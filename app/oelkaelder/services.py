"""Ølkælder money operations (F-003).

The purchase path is the security-critical one. Fixes vs legacy:
  * **server-side pricing** — line prices are recomputed from the Product, never trusted from the client
    (for betalingshop the chosen step must be one of the product's own price_steps);
  * **atomic** — the whole transaction (txn + items + shares) is one DB transaction;
  * **exact split** — largest-remainder so the per-shopper shares sum to the item total (in øre).
Balances are derived (see Shopper.balance_ore), so there is no mutable saldo to corrupt.
"""

import logging
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings
from django.core.mail import send_mail
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    Adjustment,
    Deposit,
    InterestPolicy,
    LogEntry,
    Product,
    PurchasePolicy,
    PurchaseShare,
    Shopper,
    Transaction,
    TransactionItem,
    Warning,
)

logger = logging.getLogger(__name__)


def largest_remainder(total: int, n: int) -> list[int]:
    base, rem = divmod(total, n)
    return [base + 1 if i < rem else base for i in range(n)]


def _kr(ore: int) -> str:
    return f"{ore / 100:.2f}".replace(".", ",")


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


def apply_interest() -> int:
    """Charge configured monthly interest to every active shopper whose debt is past the threshold.
    A real ledger charge (negative Adjustment). Idempotent per calendar month. Returns the count."""
    policy = InterestPolicy.get()
    if not policy.active:
        return 0
    now = timezone.now()
    charged = 0
    with transaction.atomic():
        for shopper in Shopper.objects.filter(active=True).select_related("resident"):
            bal = shopper.balance_ore
            if bal >= policy.threshold_ore:
                continue  # not in debt beyond the threshold
            if shopper.adjustments.filter(
                kind=Adjustment.Kind.INTEREST, created_at__year=now.year, created_at__month=now.month
            ).exists():
                continue  # already charged this month
            charge = int((Decimal(-bal) * policy.rate_percent / 100).quantize(Decimal(1), ROUND_HALF_UP))
            if charge <= 0:
                continue
            Adjustment.objects.create(
                shopper=shopper,
                amount_ore=-charge,
                kind=Adjustment.Kind.INTEREST,
                reason=f"Rente {policy.rate_percent}%",
            )
            charged += 1
        if charged:
            LogEntry.objects.create(
                message=f"Rente anvendt på {charged} shopper(e) ({policy.rate_percent}%)."
            )
    return charged


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


def bulk_balances(shoppers: Sequence[Shopper]) -> dict[int, int]:
    """Balances for many shoppers in three queries instead of three *per shopper*.

    `Shopper.balance_ore` runs three aggregates every time it is read, and the till lists every
    active shopper and reads the balance more than once per row — so the kiosk page was issuing
    hundreds of queries on an iPad. Same arithmetic as the property; a shopper with no rows at all
    correctly comes out as 0.
    """
    ids = [s.pk for s in shoppers]
    if not ids:
        return {}

    def totals(manager: models.Manager[Any], field: str, **extra: object) -> dict[int, int]:
        rows = manager.filter(shopper_id__in=ids, **extra).values("shopper_id").annotate(total=Sum(field))
        return {r["shopper_id"]: r["total"] or 0 for r in rows}

    deposits = totals(Deposit.objects, "amount_ore", is_valid=True)
    spent = totals(PurchaseShare.objects, "share_ore", transaction__is_valid=True)
    adjustments = totals(Adjustment.objects, "amount_ore", is_valid=True)
    return {i: deposits.get(i, 0) - spent.get(i, 0) + adjustments.get(i, 0) for i in ids}


@transaction.atomic
def record_purchase(shopper_ids: list[int], lines: list[dict[str, Any]]) -> Transaction:
    """lines: [{"product": id, "mode": "fixed|step|weight", "qty"/"grams"/"step_ore": ...}].
    Returns the created Transaction. Raises ValueError on bad input."""
    shoppers = list(Shopper.objects.filter(id__in=shopper_ids, active=True).select_related("resident"))
    if not shoppers:
        raise ValueError("Vælg mindst én aktiv shopper.")

    # Snapshot balances before the purchase so we can detect a downward threshold crossing afterwards.
    old_balances = bulk_balances(shoppers)

    # Credit limit (PurchasePolicy, off by default). Enforced here rather than only in the till so the
    # rule cannot be sidestepped by posting to this endpoint directly, and so a split purchase cannot
    # smuggle a blocked shopper in alongside solvent ones — the whole sale is refused, by name, since
    # a partial charge would leave the basket half-paid.
    policy = PurchasePolicy.get()
    blocked = sorted(s.resident.full_name for s in shoppers if policy.blocks(old_balances[s.pk]))
    if blocked:
        limit_kr = f"{policy.block_below_ore / 100:.0f}"
        raise ValueError(
            f"{', '.join(blocked)} skylder mere end {limit_kr} kr og kan ikke handle. "
            "Indbetal til ølkælderkontoen først."
        )

    txn = Transaction.objects.create(created_at=timezone.now())
    total = 0
    for line in lines:
        product = Product.objects.get(id=int(line["product"]), active=True)  # priced from DB, not client
        quantity, price = _line_ore(product, line)
        if price <= 0:
            continue
        TransactionItem.objects.create(transaction=txn, product=product, quantity=quantity, price_ore=price)
        total += price

    if total <= 0:
        raise ValueError("Tom kurv.")  # rolls back the transaction

    crossings: list[tuple[Shopper, int, int]] = []
    for shopper, share in zip(
        sorted(shoppers, key=lambda s: s.id), largest_remainder(total, len(shoppers)), strict=False
    ):
        PurchaseShare.objects.create(transaction=txn, shopper=shopper, share_ore=share)
        old_ore = old_balances[shopper.id]
        crossings.append((shopper, old_ore, old_ore - share))

    LogEntry.objects.create(message=f"Køb txn#{txn.id}: {total} øre delt på {len(shoppers)} shopper(e).")

    # Send debt-warning mails only after the purchase commits (never inside the transaction).
    def _fire_warnings() -> None:
        for s, old_ore, new_ore in crossings:
            send_debt_warnings(s, old_ore, new_ore)

    transaction.on_commit(_fire_warnings)
    return txn


@transaction.atomic
def record_deposit(shopper: Shopper, amount_ore: int) -> None:
    from .models import Deposit

    if amount_ore <= 0:
        raise ValueError("Beløb skal være positivt.")
    Deposit.objects.create(shopper=shopper, amount_ore=amount_ore, created_at=timezone.now())
    LogEntry.objects.create(message=f"Indbetaling {amount_ore} øre til shopper#{shopper.id}.")


@transaction.atomic
def record_adjustment(shopper: Shopper, amount_ore: int, reason: str, actor: str) -> Adjustment:
    """A manual signed correction to one shopper's balance (F-003). Negative = a charge that increases
    their debt; positive = a credit.

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
    adj = Adjustment.objects.create(
        shopper=shopper, amount_ore=amount_ore, kind=Adjustment.Kind.MANUAL, reason=reason
    )
    LogEntry.objects.create(
        message=(
            f"Manuel justering #{adj.pk} på {amount_ore} øre for {shopper.resident.full_name} "
            f"(shopper#{shopper.pk}) af {actor}: {reason}"
        )
    )
    return adj


@transaction.atomic
def void_adjustment(adjustment_id: int, actor: str) -> bool:
    """Soft-delete a manual correction that was entered wrongly. Returns False if already voided, so a
    double-submit cannot write a second log line. Interest adjustments are deliberately out of scope —
    those are generated by apply_interest and voiding one by hand would silently desynchronise the
    monthly idempotency guard."""
    updated = Adjustment.objects.filter(pk=adjustment_id, is_valid=True, kind=Adjustment.Kind.MANUAL).update(
        is_valid=False
    )
    if not updated:
        return False
    LogEntry.objects.create(message=f"Manuel justering #{adjustment_id} annulleret af {actor}.")
    return True


@transaction.atomic
def void_purchase(txn_id: int, actor: str) -> bool:
    """Soft-delete a mistaken sale (F-003, legacy `deleteTransaction`). Returns False if it was already
    voided, so a double-submit cannot write a second log line.

    No compensating ledger write is needed: balances are *derived*, and Shopper.balance_ore filters
    `transaction__is_valid=True`, so clearing the flag refunds every buyer of the basket at once. The
    legacy code had to read-modify-write each saldo and accumulated fractional-øre drift doing it.

    Two things this deliberately does NOT undo:
      * a debt-warning mail already sent by the purchase (send_debt_warnings is edge-triggered and
        keeps no state, so voiding also re-arms the same warning for the next crossing);
      * an interest Adjustment charged while the debt included this sale. apply_interest materialises
        the charge from the balance at the time and is idempotent per month, so it will not
        self-correct — the shopper keeps interest on a debt that no longer exists.
    """
    if not Transaction.objects.filter(pk=txn_id, is_valid=True).update(is_valid=False):
        return False
    total = TransactionItem.objects.filter(transaction_id=txn_id).aggregate(s=Sum("price_ore"))["s"] or 0
    buyers = ", ".join(
        s.shopper.resident.full_name
        for s in PurchaseShare.objects.filter(transaction_id=txn_id).select_related("shopper__resident")
    )
    LogEntry.objects.create(
        message=f"Køb txn#{txn_id} ({total} øre, {buyers or 'ingen registrerede købere'}) annulleret af {actor}."
    )
    return True
