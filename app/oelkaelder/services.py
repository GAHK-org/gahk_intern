"""Ølkælder's money operations (F-003): each one a transfer in GAHK-konto, with Ølkælder's own record.

  purchase      one basket = one Transaction with its items, and one transfer per sharer (their
                account → Salg), all or nothing. The executor (the first one picked) pays the remainder.
                The ledger's limit decides: a sharer the purchase would take below their account's
                limit refuses the whole basket, by name.
  deposit       Ølkælder's bank → the shopper, recorded by a manager.
  adjustment    a manual correction: a charge into Justeringer, or money from it.
  interest      a charge per account into Renter, once a month, skipping exempt accounts.
  void          the transfer(s) reversed. Charges and reversals are never stopped by a limit.

Prices are recomputed from the Product, never trusted from the client (a betalingshop step must be
one of the product's own).
"""

import logging
import uuid
from collections.abc import Iterable
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone
from django.utils.formats import date_format

from audit import trail
from credit import ledger as credit_ledger
from credit.models import Posting, Transfer
from residents.models import Account

from . import ledger
from .models import (
    Adjustment,
    Deposit,
    InterestExemption,
    InterestPolicy,
    LogEntry,
    Product,
    PurchaseShare,
    Shopper,
    Transaction,
    TransactionItem,
    Warning,
)

logger = logging.getLogger(__name__)

# The identity of the sends Ølkælder makes on its own (a basket's shares, voids, interest): the same
# record and action always give the same token, so a retry or a double press writes nothing twice.
NAMESPACE = uuid.UUID("0b8f3f5e-8d0c-4a43-9f3e-4f5a1c2b7d61")


def _token(*parts: object) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, ":".join(str(p) for p in parts))


def split(total: int, n: int) -> list[int]:
    """Each sharer's part of `total`, the executor (first) first: everyone pays total // n, and the
    executor also pays what is left over."""
    base, rest = divmod(total, n)
    return [base + rest] + [base] * (n - 1)


def _kr(ore: int) -> str:
    return f"{ore / 100:.2f}".replace(".", ",")


def _who(actor: Account | None) -> str:
    return actor.email if actor is not None else "systemet"


def is_live(transfer: Transfer | None) -> bool:
    """Moved money that still stands: written, and not reversed."""
    return transfer is not None and not hasattr(transfer, "reversed_by")


def _live(field: str) -> Q:
    return Q(**{f"{field}__isnull": False, f"{field}__reversed_by__isnull": True})


def counted_sales() -> QuerySet[Transaction]:
    """The sales that stand: some of their money still moved, or none ever moved (old baskets whose
    buyers were not migrated). A sale whose every transfer is reversed is voided."""
    shares = PurchaseShare.objects.filter(transaction=OuterRef("pk"))
    return Transaction.objects.filter(
        Q(Exists(shares.filter(_live("transfer") | _live("moved_transfer"))))
        | ~Q(Exists(shares.filter(transfer__isnull=False)))
    )


def not_reversed(field: str = "transfer") -> Q:
    """A record whose transfer stands (or that never moved money: a legacy 0 kr row)."""
    return Q(**{f"{field}__reversed_by__isnull": True})


# ---- debt warnings --------------------------------------------------------------------------------


def send_debt_warnings(shopper: Shopper, old_ore: int, new_ore: int) -> None:
    """Mirror the legacy edge-trigger: e-mail the shopper when a purchase pushes their balance *down*
    through an active warning threshold (old above, new below). Best-effort — a mail failure never
    affects the purchase."""
    for warning in Warning.objects.filter(active=True):
        if old_ore > warning.threshold_ore and new_ore < warning.threshold_ore:
            body = warning.message.replace("SALDOSALDOSALDO", _kr(new_ore)).replace("{saldo}", _kr(new_ore))
            try:
                send_mail(
                    "Ølkælder saldo",
                    body,
                    settings.OELKAELDER_FROM_EMAIL,
                    [shopper.resident.email],
                    fail_silently=True,
                )
            except Exception:
                logger.exception("Failed to send ølkælder warning to shopper#%s", shopper.pk)


# ---- the till -------------------------------------------------------------------------------------


def _line_ore(product: Product, line: dict[str, Any]) -> tuple[int, int]:
    """Return (quantity_stored, price_ore) for one basket line, priced authoritatively from the product.

    Mirrors the legacy Oelkaelder_model::addItem. `mode`:
      * fixed  → price_ore x qty
      * step   → the tapped step (must be one of the product's price_steps) x qty
      * weight → weight_price_ore x grams / 100 (per-100g rate; no weight products in current data)
    Returns (0, 0) for a non-positive line so the caller skips it.
    """
    mode = line.get("mode", "fixed")
    if mode == "weight":
        grams = int(line.get("grams", 0))
        if grams <= 0:
            return 0, 0
        if not product.weight_price_ore:
            raise ValueError(f"{product.name} sælges ikke efter vægt.")
        return grams, round(product.weight_price_ore * grams / 100)
    if mode == "step":
        step_ore = int(line.get("step_ore", 0))
        if step_ore not in product.price_steps_ore():
            raise ValueError(f"Ugyldig pris for {product.name}.")
        qty = int(line.get("qty", 1))
        return (qty, step_ore * qty) if qty > 0 else (0, 0)
    qty = int(line.get("qty", 0))  # fixed
    return (qty, product.price_ore * qty) if qty > 0 else (0, 0)


def _ordered_shoppers(shopper_ids: Iterable[int]) -> list[Shopper]:
    """The active shoppers in the order the till posted them (the order they were picked): the first
    is the executor. A repeated id counts once."""
    ids = list(dict.fromkeys(int(i) for i in shopper_ids))
    found = Shopper.objects.filter(id__in=ids, active=True).select_related(
        "resident", "credit_account__holder__person"
    )
    by_id = {s.pk: s for s in found}
    return [by_id[i] for i in ids if i in by_id]


def record_purchase(
    shopper_ids: list[int], lines: list[dict[str, Any]], *, token: uuid.UUID | None = None
) -> Transaction:
    """lines: [{"product": id, "mode": "fixed|step|weight", "qty"/"grams"/"step_ore": ...}].
    `shopper_ids` in the order they were picked: the first is the executor and pays the remainder.
    `token`: the till's send; the same basket posted twice is one sale. Raises ValueError (Danish,
    for the till) on bad input or a sharer the limit refuses; nothing is written then."""
    if token is not None:
        done = Transaction.objects.filter(dedupe_token=token).first()
        if done is not None:
            return done
    shoppers = _ordered_shoppers(shopper_ids)
    if not shoppers:
        raise ValueError("Vælg mindst én aktiv shopper.")
    own = ledger.accounts()
    try:
        with transaction.atomic():
            return _purchase(shoppers, lines, token, own)
    except IntegrityError:
        # The same token at the same moment (a double tap): the other request made the sale.
        if token is not None and (done := Transaction.objects.filter(dedupe_token=token).first()):
            return done
        raise


def _purchase(
    shoppers: list[Shopper], lines: list[dict[str, Any]], token: uuid.UUID | None, own: ledger.Accounts
) -> Transaction:
    txn = Transaction.objects.create(created_at=timezone.now(), dedupe_token=token)
    total = 0
    items = []
    for line in lines:
        product = Product.objects.get(id=int(line["product"]), active=True)  # priced from DB, not client
        quantity, price = _line_ore(product, line)
        if price <= 0:
            continue
        TransactionItem.objects.create(transaction=txn, product=product, quantity=quantity, price_ore=price)
        items.append(f"{quantity}× {product.name}")
        total += price
    if total <= 0:
        raise ValueError("Tom kurv.")  # rolls back the transaction

    what = ", ".join(items)
    crossings: list[tuple[Shopper, int, int]] = []
    for index, (shopper, share) in enumerate(zip(shoppers, split(total, len(shoppers)), strict=True)):
        name = shopper.resident.full_name
        others = [s.resident.full_name for s in shoppers if s.pk != shopper.pk]
        shared = f", delt med {', '.join(others)}" if others else ""
        transfer = None
        if share > 0:  # a basket of fewer øre than sharers leaves some with nothing to pay
            try:
                transfer = credit_ledger.send(
                    actor=None,
                    sender=shopper.credit_account,
                    receiver=own.sales,
                    amount_ore=share,
                    sender_text=f"Køb #{txn.pk}{shared}: {what}",
                    receiver_text=f"Køb #{txn.pk}: {name}"
                    + (" (betaler resten)" if index == 0 and others else ""),
                    dedupe_token=_token("basket", txn.pk, shopper.pk),
                    source=f"oelkaelder.basket:{txn.pk}",
                )
            except credit_ledger.LedgerError as exc:
                raise ValueError(f"{name} kan ikke handle: {exc}") from exc
            after = Posting.objects.get(transfer=transfer, account=shopper.credit_account).balance_after
            crossings.append((shopper, after + share, after))
        PurchaseShare.objects.create(transaction=txn, shopper=shopper, share_ore=share, transfer=transfer)

    LogEntry.objects.create(message=f"Køb txn#{txn.id}: {total} øre delt på {len(shoppers)} shopper(e).")

    # Debt-warning mails only after the purchase commits (never inside the transaction).
    def _fire_warnings() -> None:
        for s, old_ore, new_ore in crossings:
            send_debt_warnings(s, old_ore, new_ore)

    transaction.on_commit(_fire_warnings)
    return txn


# ---- voiding a sale -------------------------------------------------------------------------------


def void_purchase(txn_id: int, actor: Account | None) -> bool:
    """Annullér a mistaken sale: every transfer of it that stands is reversed (a moved share's charge
    too), in one go. Returns False when nothing stood, so a double submit writes nothing twice.

    Not undone: a debt-warning mail already sent, and interest charged while the debt included this
    sale (a charge of its own, reversed alone if it should go)."""
    with transaction.atomic():
        txn = Transaction.objects.select_for_update().get(pk=txn_id)  # one void at a time
        shares = list(
            PurchaseShare.objects.filter(transaction=txn)
            .select_related("shopper__resident", "transfer__reversed_by", "moved_transfer__reversed_by")
            .order_by("pk")
        )
        undone = 0
        try:
            for share in shares:
                for field in ("transfer", "moved_transfer"):
                    transfer = getattr(share, field)
                    if is_live(transfer):
                        credit_ledger.reverse(
                            actor=actor,
                            transfer=transfer,
                            dedupe_token=_token(f"void-{field}", share.pk),
                            source=f"oelkaelder.void:{txn.pk}",
                        )
                        undone += 1
        except credit_ledger.LedgerError as exc:
            raise ValueError(str(exc)) from exc
        if not undone:
            return False
        total = sum(TransactionItem.objects.filter(transaction=txn).values_list("price_ore", flat=True))
        buyers = ", ".join(s.shopper.resident.full_name for s in shares)
        LogEntry.objects.create(
            message=(
                f"Køb txn#{txn.pk} ({total} øre, {buyers or 'ingen registrerede købere'}) "
                f"annulleret af {_who(actor)}."
            )
        )
    return True


def _possessive(name: str) -> str:
    return f"{name}'" if name[-1:].lower() in ("s", "x", "z") else f"{name}s"


def executor_share(txn_id: int) -> PurchaseShare | None:
    """The share of whoever made the basket: the first picked, so the first share written."""
    return PurchaseShare.objects.filter(transaction_id=txn_id).order_by("pk").first()


def move_share_to_executor(share_id: int, actor: Account | None) -> PurchaseShare:
    """A sharer who was not part of the basket: their transfer is reversed, and the basket's executor
    (who picked them) is charged the same amount instead, in one go. Raises ValueError (Danish)."""
    with transaction.atomic():
        share = (
            PurchaseShare.objects.select_for_update(of=("self",))
            .select_related("shopper__resident", "transfer__reversed_by")
            .get(pk=share_id)
        )
        executor = executor_share(share.transaction_id)
        if executor is None or executor.pk == share.pk:
            raise ValueError("Den, der lavede købet, kan ikke flyttes over på sig selv.")
        if share.moved_transfer_id is not None:
            raise ValueError("Andelen er allerede flyttet.")
        if not is_live(share.transfer):
            raise ValueError("Andelen er ikke betalt eller er tilbageført; der er intet at flytte.")
        assert share.transfer is not None  # noqa: S101  # nosec B101 - is_live said so
        own = ledger.accounts()
        name = share.shopper.resident.full_name
        payer = executor.shopper.resident.full_name
        txn = share.transaction_id
        try:
            credit_ledger.reverse(
                actor=actor,
                transfer=share.transfer,
                dedupe_token=_token("void-transfer", share.pk),
                source=f"oelkaelder.moved:{txn}",
            )
            share.moved_transfer = credit_ledger.charge(
                actor=actor,
                account=executor.shopper.credit_account,
                into=own.sales,
                amount_ore=share.share_ore,
                text=f"Køb #{txn}: {_possessive(name)} andel flyttet til dig",
                into_text=f"Køb #{txn}: {_possessive(name)} andel betalt af {payer}",
                dedupe_token=_token("moved", share.pk),
                source=f"oelkaelder.moved:{txn}",
            )
        except credit_ledger.LedgerError as exc:
            raise ValueError(str(exc)) from exc
        share.save(update_fields=["moved_transfer"])
        LogEntry.objects.create(
            message=(
                f"Køb txn#{txn}: {_possessive(name)} andel ({share.share_ore} øre) flyttet til {payer} "
                f"af {_who(actor)}."
            )
        )
    return share


# ---- deposits -------------------------------------------------------------------------------------


def record_deposit(
    shopper: Shopper, amount_ore: int, *, actor: Account | None, token: uuid.UUID | None = None
) -> Deposit:
    """Money paid into Ølkælder's bank, to the shopper's account. `token`: the form's send."""
    if amount_ore <= 0:
        raise ValueError("Beløb skal være positivt.")
    own = ledger.accounts()
    name = shopper.resident.full_name
    with transaction.atomic():
        try:
            transfer = credit_ledger.send(
                actor=actor,
                sender=own.bank,
                receiver=shopper.credit_account,
                amount_ore=amount_ore,
                sender_text=f"Indbetaling fra {name}",
                receiver_text="Indbetaling",
                dedupe_token=token or uuid.uuid4(),
                source="oelkaelder.deposit",
            )
        except credit_ledger.LedgerError as exc:
            raise ValueError(str(exc)) from exc
        deposit, made = Deposit.objects.get_or_create(
            transfer=transfer,
            defaults={"shopper": shopper, "amount_ore": amount_ore, "created_at": transfer.created_at},
        )
        if made:
            LogEntry.objects.create(
                message=f"Indbetaling {amount_ore} øre til shopper#{shopper.id} af {_who(actor)}."
            )
    return deposit


def void_deposit(deposit: Deposit, actor: Account | None) -> bool:
    """Reverse a mistaken deposit. False when it was reversed already."""
    if deposit.transfer_id is None:  # a legacy 0 kr deposit: nothing moved
        return False
    with transaction.atomic():
        transfer = Transfer.objects.select_related("reversed_by").filter(pk=deposit.transfer_id).first()
        if not is_live(transfer):
            return False
        assert transfer is not None  # noqa: S101  # nosec B101 - is_live said so
        try:
            credit_ledger.reverse(
                actor=actor,
                transfer=transfer,
                dedupe_token=_token("void-deposit", deposit.pk),
                source="oelkaelder.deposit",
            )
        except credit_ledger.LedgerError as exc:
            raise ValueError(str(exc)) from exc
        LogEntry.objects.create(
            message=f"Indbetaling #{deposit.pk} ({deposit.amount_ore} øre) annulleret af {_who(actor)}."
        )
    return True


# ---- adjustments ----------------------------------------------------------------------------------


def record_adjustment(
    shopper: Shopper,
    amount_ore: int,
    reason: str,
    actor: Account | None,
    *,
    token: uuid.UUID | None = None,
) -> Adjustment:
    """A manual signed correction to one shopper's balance (F-003). Negative = a charge into
    Justeringer (more debt); positive = money from Justeringer to the shopper.

    This is the repair tool for the ølkælder data the 2026-08 migration could not reconstruct: sales
    whose buyers were never migrated left those people under-charged, and baskets where only some
    buyers survived charged the whole total to whoever remained, over-charging them. Corrections
    therefore go both ways, which is why the amount is signed.

    A reason is required, not optional: an unexplained balance change is indistinguishable from a bug
    to the resident it happens to.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Angiv en begrundelse for justeringen.")
    if amount_ore == 0:
        raise ValueError("Beløbet må ikke være 0.")
    own = ledger.accounts()
    name = shopper.resident.full_name
    with transaction.atomic():
        try:
            if amount_ore < 0:
                transfer = credit_ledger.charge(
                    actor=actor,
                    account=shopper.credit_account,
                    into=own.adjustments,
                    amount_ore=-amount_ore,
                    text=reason,
                    into_text=f"{name}: {reason}",
                    dedupe_token=token or uuid.uuid4(),
                    source="oelkaelder.adjustment",
                )
            else:
                transfer = credit_ledger.send(
                    actor=actor,
                    sender=own.adjustments,
                    receiver=shopper.credit_account,
                    amount_ore=amount_ore,
                    sender_text=f"{name}: {reason}",
                    receiver_text=reason,
                    dedupe_token=token or uuid.uuid4(),
                    source="oelkaelder.adjustment",
                )
        except credit_ledger.LedgerError as exc:
            raise ValueError(str(exc)) from exc
        adjustment, made = Adjustment.objects.get_or_create(
            transfer=transfer,
            defaults={
                "shopper": shopper,
                "amount_ore": amount_ore,
                "kind": Adjustment.Kind.MANUAL,
                "reason": reason,
                "created_at": transfer.created_at,
            },
        )
        if made:
            LogEntry.objects.create(
                message=(
                    f"Manuel justering #{adjustment.pk} på {amount_ore} øre for {name} "
                    f"(shopper#{shopper.pk}) af {_who(actor)}: {reason}"
                )
            )
    return adjustment


def void_adjustment(adjustment_id: int, actor: Account | None) -> bool:
    """Reverse a correction or an interest charge entered wrongly; each is reversed alone. False when
    it was reversed already, so a double submit writes nothing twice."""
    with transaction.atomic():
        adjustment = Adjustment.objects.select_related("transfer__reversed_by").get(pk=adjustment_id)
        if not is_live(adjustment.transfer):
            return False
        assert adjustment.transfer is not None  # noqa: S101  # nosec B101 - is_live said so
        try:
            credit_ledger.reverse(
                actor=actor,
                transfer=adjustment.transfer,
                dedupe_token=_token("void-adjustment", adjustment.pk),
                source="oelkaelder.adjustment",
            )
        except credit_ledger.LedgerError as exc:
            raise ValueError(str(exc)) from exc
        what = "Rente" if adjustment.kind == Adjustment.Kind.INTEREST else "Manuel justering"
        LogEntry.objects.create(message=f"{what} #{adjustment_id} annulleret af {_who(actor)}.")
    return True


# ---- interest -------------------------------------------------------------------------------------


def exempt_account_ids(day: date) -> set[int]:
    """The accounts the interest skips on `day`."""
    return set(
        InterestExemption.objects.filter(
            Q(ends_on__isnull=True) | Q(ends_on__gte=day), starts_on__lte=day
        ).values_list("credit_account_id", flat=True)
    )


def interest_token(credit_account_id: int, day: date) -> uuid.UUID:
    """One charge per account per month: the month is part of the charge's identity."""
    return _token("interest", credit_account_id, f"{day:%Y-%m}")


def apply_interest(actor: Account | None = None) -> int:
    """This month's interest: one charge per account whose debt is past the threshold, into Renter,
    each with its own Adjustment record. Exempt accounts are skipped. Once per account per month (the
    charge's token is the account and the month), all of the run or none of it. Returns how many
    were charged now."""
    policy = InterestPolicy.get()
    if not policy.active:
        return 0
    today = timezone.localdate()
    exempt = exempt_account_ids(today)
    own = ledger.accounts()
    shoppers = list(
        ledger.with_balance(Shopper.objects.filter(active=True))
        .select_related("resident", "credit_account__book")
        .order_by("pk")
    )
    text = f"Rente {date_format(today, 'F Y')} ({policy.rate_percent} %)"
    charged = 0
    with transaction.atomic():
        for shopper in shoppers:
            balance = ledger.annotated(shopper)
            if balance >= policy.threshold_ore or shopper.credit_account_id in exempt:
                continue
            token = interest_token(shopper.credit_account_id, today)
            if Transfer.objects.filter(dedupe_token=token).exists():
                continue  # charged this month already (and perhaps reversed: that stands too)
            amount = int((Decimal(-balance) * policy.rate_percent / 100).quantize(Decimal(1), ROUND_HALF_UP))
            if amount <= 0:
                continue
            try:
                transfer = credit_ledger.charge(
                    actor=actor,
                    account=shopper.credit_account,
                    into=own.interest,
                    amount_ore=amount,
                    text=text,
                    into_text=f"{shopper.resident.full_name}: {text}",
                    dedupe_token=token,
                    source="oelkaelder.interest",
                )
            except credit_ledger.LedgerError as exc:
                raise ValueError(str(exc)) from exc
            Adjustment.objects.create(
                shopper=shopper,
                amount_ore=-amount,
                kind=Adjustment.Kind.INTEREST,
                reason=text,
                created_at=transfer.created_at,
                transfer=transfer,
            )
            charged += 1
        if charged:
            LogEntry.objects.create(
                message=f"Rente anvendt på {charged} shopper(e) ({policy.rate_percent}%) af {_who(actor)}."
            )
    return charged


def add_exemption(
    shopper: Shopper, reason: str, starts_on: date, ends_on: date | None, actor: Account | None
) -> InterestExemption:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Skriv hvorfor kontoen er fritaget for rente.")
    if ends_on is not None and ends_on < starts_on:
        raise ValueError("Slutdatoen ligger før startdatoen.")
    with trail.event("oelkaelder.exemption.created", actor=actor):
        return InterestExemption.objects.create(
            credit_account=shopper.credit_account,
            reason=reason,
            starts_on=starts_on,
            ends_on=ends_on,
            created_by=actor,
        )


def end_exemption(exemption: InterestExemption, actor: Account | None) -> None:
    """From the next run on, the account pays interest again. One that has not begun yet is removed:
    it never exempted anything."""
    yesterday = timezone.localdate() - timedelta(days=1)
    with trail.event("oelkaelder.exemption.ended", subject=exemption, actor=actor):
        if exemption.starts_on > yesterday:
            exemption.delete()
        elif exemption.ends_on is None or exemption.ends_on > yesterday:
            exemption.ends_on = yesterday
            exemption.save(update_fields=["ends_on"])


# ---- reading --------------------------------------------------------------------------------------


def statement(shoppers: Iterable[Shopper], *, limit: int) -> list[credit_ledger.Line]:
    """The shoppers' ølkælder account lines from the ledger, newest first."""
    lines: list[credit_ledger.Line] = []
    for shopper in shoppers:
        lines += credit_ledger.statement(shopper.credit_account, limit=limit)
    lines.sort(key=lambda line: line.posting.pk, reverse=True)
    return lines[:limit]


def balances(shoppers: Iterable[Shopper]) -> dict[int, int]:
    """Each shopper's balance, by shopper id, in one query."""
    listed = list(shoppers)
    found = ledger.balances([s.credit_account_id for s in listed])
    return {s.pk: found.get(s.credit_account_id, 0) for s in listed}


def blocked(balance: int, credit_limit_ore: int | None) -> bool:
    """Whether the till can sell nothing at all: the balance is at or past the account's limit."""
    return credit_limit_ore is not None and balance <= credit_limit_ore
