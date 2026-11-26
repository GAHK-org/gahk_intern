"""Ølkælder's history into GAHK-konto: every record from before the ledger as the transfer it stood for.

Oldest first, each dated when it happened (credit.ledger.record_history), never stopped by a limit:

  * a deposit: Ølkælder's bank → the shopper ("Indbetaling");
  * a basket: one transfer per stored share, the shopper → Salg, as stored (never split again);
  * interest: a charge into Renter; a manual adjustment: a charge into Justeringer, or money from it;
  * a voided one: the same, and its reversal right after it.

Each record is linked to its transfer as it is written. It is idempotent: a record already linked is
skipped, and each transfer's dedupe token is made from the record's own id, so a run cut off half way
picks up where it stopped without writing anything twice. Afterwards every shopper's balance is what
the old records added up to.

It runs once, from the migration that adds the links (before the old `is_valid` flags are dropped:
they say what was voided), and for the demo seed's and the ETL's records, which pass what was voided
themselves. It refuses a book that has closed a period, or that an anchor already fixed: dating
transfers into the past would change what those already signed.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Min

from audit import trail
from credit import ledger as credit_ledger
from credit.models import Anchor, Closing, CreditAccount, Transfer
from residents.models import Account

from . import ledger

NAMESPACE = uuid.UUID("5f0f7a52-3c1e-4bb4-9a51-0e1d2a6f7c41")
CHUNK = 500
SOURCE = "oelkaelder.history"


class HistoryError(Exception):
    """The history cannot be imported as things stand. The message says why (English: for whoever
    runs the migration)."""


def token(*parts: object) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, ":".join(str(p) for p in parts))


@dataclass(frozen=True)
class Models:
    """The record models to read: the migration's historical ones, or the app's own."""

    Shopper: Any
    Deposit: Any
    Transaction: Any
    TransactionItem: Any
    PurchaseShare: Any
    Adjustment: Any


def live_models() -> Models:
    from . import models

    return Models(
        Shopper=models.Shopper,
        Deposit=models.Deposit,
        Transaction=models.Transaction,
        TransactionItem=models.TransactionItem,
        PurchaseShare=models.PurchaseShare,
        Adjustment=models.Adjustment,
    )


def _voided_by_flag(model: Any) -> set[int]:  # noqa: ANN401
    """The ids an old `is_valid` column marks as voided; none once the column is gone."""
    if not any(f.name == "is_valid" for f in model._meta.get_fields()):
        return set()
    return set(model.objects.filter(is_valid=False).values_list("pk", flat=True))


def import_history(
    models: Models | None = None, *, voided: dict[str, set[int]] | None = None
) -> dict[str, int]:
    """Write every record not yet in the ledger. `voided`: {"deposit"|"transaction"|"adjustment": ids}
    voided in the old system; by default read from the models' `is_valid` columns. Returns counts."""
    m = models or live_models()
    if voided is None:
        voided = {
            "deposit": _voided_by_flag(m.Deposit),
            "transaction": _voided_by_flag(m.Transaction),
            "adjustment": _voided_by_flag(m.Adjustment),
        }
    with trail.unaudited():
        counts = _import(m, voided)
    if any(counts.values()):
        trail.record("oelkaelder.history.imported", actor=None, details=counts)
    return counts


def _import(m: Models, voided: dict[str, set[int]]) -> dict[str, int]:
    keys = _pending(m, voided)
    counts = {"deposits": 0, "purchases": 0, "adjustments": 0, "reversals": 0}
    if not keys:
        _open_missing_accounts(m)  # a shopper without history still gets their account
        return counts
    own = ledger.accounts()
    _refuse_fixed_book(own)
    accounts, names = _open_missing_accounts(m)
    keys.sort()
    for start in range(0, len(keys), CHUNK):
        chunk = keys[start : start + CHUNK]
        rows = _load(m, chunk)
        for _, _, kind, pk in chunk:
            with transaction.atomic():
                if kind == "deposit":
                    made, undone = _deposit(m, rows.deposits[pk], own, accounts, names, voided)
                    counts["deposits"] += made
                elif kind == "transaction":
                    made, undone = _basket(m, rows, pk, own, accounts, names, voided)
                    counts["purchases"] += made
                else:
                    made, undone = _adjustment(m, rows.adjustments[pk], own, accounts, names, voided)
                    counts["adjustments"] += made
                counts["reversals"] += undone
    return counts


def _refuse_fixed_book(own: ledger.Accounts) -> None:
    if Closing.objects.filter(book=own.book).exists():
        raise HistoryError(
            f"The {own.book.name} book has closed a period: its history can no longer be imported into it."
        )
    first = CreditAccount.objects.filter(book=own.book).aggregate(first=Min("created_at"))["first"]
    if first is not None and Anchor.objects.filter(as_of__gt=first).exists():
        raise HistoryError(
            f"An anchor already fixes the {own.book.name} book's accounts; transfers dated before it "
            "would no longer match it. Import the ølkælder history before GAHK-konto anchors the book."
        )


def _open_missing_accounts(m: Models) -> tuple[dict[int, CreditAccount], dict[int, str]]:
    """Every shopper's ølkælder account (opened where missing) and name, by shopper id."""
    shoppers = list(m.Shopper.objects.order_by("pk").values("pk", "resident_id", "credit_account_id"))
    logins = Account.objects.select_related("person").in_bulk({s["resident_id"] for s in shoppers})
    nobody = [
        f"shopper {s['pk']} ({logins[s['resident_id']].email})"
        for s in shoppers
        if s["credit_account_id"] is None and logins[s["resident_id"]].person_id is None
    ]
    if nobody:
        raise HistoryError(
            "These shoppers' logins have no person behind them, so they cannot hold an account: "
            + ", ".join(nobody)
        )
    for s in shoppers:
        if s["credit_account_id"] is None:
            opened = ledger.open_shopper_account(logins[s["resident_id"]])
            m.Shopper.objects.filter(pk=s["pk"]).update(credit_account_id=opened.pk)
            s["credit_account_id"] = opened.pk
    by_id = CreditAccount.objects.in_bulk([s["credit_account_id"] for s in shoppers])
    accounts = {s["pk"]: by_id[s["credit_account_id"]] for s in shoppers}
    names = {s["pk"]: logins[s["resident_id"]].full_name for s in shoppers}
    return accounts, names


def _reversed(transfer_ids: Iterable[int]) -> set[int]:
    return set(
        Transfer.objects.filter(reverses_id__in=list(transfer_ids)).values_list("reverses_id", flat=True)
    )


def _pending(m: Models, voided: dict[str, set[int]]) -> list[tuple[Any, int, str, int]]:
    """(when, order, kind, id) of every record with something left to write."""
    keys: list[tuple[Any, int, str, int]] = []
    linked_voided: dict[int, tuple[str, int]] = {}
    for row in m.Deposit.objects.filter(amount_ore__gt=0).values("pk", "created_at", "transfer_id"):
        if row["transfer_id"] is None:
            keys.append((row["created_at"], 0, "deposit", row["pk"]))
        elif row["pk"] in voided["deposit"]:
            linked_voided[row["transfer_id"]] = ("deposit", row["pk"])
    shares = m.PurchaseShare.objects.filter(share_ore__gt=0).values(
        "transaction_id", "transaction__created_at", "transfer_id"
    )
    baskets: dict[int, Any] = {}
    for row in shares:
        if row["transfer_id"] is None:
            baskets[row["transaction_id"]] = row["transaction__created_at"]
        elif row["transaction_id"] in voided["transaction"]:
            linked_voided[row["transfer_id"]] = ("transaction", row["transaction_id"])
    for row in m.Adjustment.objects.exclude(amount_ore=0).values("pk", "created_at", "transfer_id"):
        if row["transfer_id"] is None:
            keys.append((row["created_at"], 2, "adjustment", row["pk"]))
        elif row["pk"] in voided["adjustment"]:
            linked_voided[row["transfer_id"]] = ("adjustment", row["pk"])
    # Linked and voided, but the run stopped before the reversal: finish it.
    done = _reversed(linked_voided)
    for transfer_id, (kind, pk) in linked_voided.items():
        if transfer_id in done:
            continue
        if kind == "transaction":
            baskets.setdefault(pk, m.Transaction.objects.get(pk=pk).created_at)
        else:
            model = m.Deposit if kind == "deposit" else m.Adjustment
            keys.append((model.objects.get(pk=pk).created_at, 0 if kind == "deposit" else 2, kind, pk))
    keys += [(when, 1, "transaction", pk) for pk, when in baskets.items()]
    return list(dict.fromkeys(keys))


@dataclass
class _Rows:
    deposits: dict[int, Any]
    adjustments: dict[int, Any]
    baskets: dict[int, Any]
    shares: dict[int, list[Any]]
    items: dict[int, str]


def _load(m: Models, chunk: list[tuple[Any, int, str, int]]) -> _Rows:
    ids: dict[str, list[int]] = {"deposit": [], "transaction": [], "adjustment": []}
    for _, _, kind, pk in chunk:
        ids[kind].append(pk)
    shares: dict[int, list[Any]] = {}
    for share in m.PurchaseShare.objects.filter(
        transaction_id__in=ids["transaction"], share_ore__gt=0
    ).order_by("pk"):
        shares.setdefault(share.transaction_id, []).append(share)
    items: dict[int, list[str]] = {}
    for row in (
        m.TransactionItem.objects.filter(transaction_id__in=ids["transaction"])
        .order_by("pk")
        .values("transaction_id", "quantity", "product__name")
    ):
        items.setdefault(row["transaction_id"], []).append(f"{row['quantity']}× {row['product__name']}")
    return _Rows(
        deposits=m.Deposit.objects.in_bulk(ids["deposit"]),
        adjustments=m.Adjustment.objects.in_bulk(ids["adjustment"]),
        baskets=m.Transaction.objects.in_bulk(ids["transaction"]),
        shares=shares,
        items={pk: ", ".join(lines) for pk, lines in items.items()},
    )


def _write(
    model: Any,  # noqa: ANN401
    row: Any,  # noqa: ANN401
    field: str,
    *,
    kind: str,
    sender: CreditAccount,
    receiver: CreditAccount,
    amount_ore: int,
    sender_text: str,
    receiver_text: str,
    at: Any,  # noqa: ANN401 - a datetime
    label: str,
) -> tuple[Transfer, int]:
    """The record's transfer, written and linked if it is not yet; (the transfer, 1 if written now)."""
    linked = getattr(row, f"{field}_id")
    if linked is not None:
        return Transfer.objects.get(pk=linked), 0
    made = credit_ledger.record_history(
        kind=kind,
        sender=sender,
        receiver=receiver,
        amount_ore=amount_ore,
        sender_text=sender_text,
        receiver_text=receiver_text,
        at=at,
        dedupe_token=token(label, row.pk),
        source=f"{SOURCE}:{label}:{row.pk}",
    )
    model.objects.filter(pk=row.pk).update(**{f"{field}_id": made.pk})
    return made, 1


def _undo(transfer: Transfer, at: Any, label: str, pk: int) -> int:  # noqa: ANN401
    """The reversal of a voided record's transfer, right after it; 1 if written now."""
    if Transfer.objects.filter(reverses=transfer).exists():
        return 0
    out, back = credit_ledger._sides(transfer)
    credit_ledger.record_history(
        kind=Transfer.Kind.REVERSAL,
        sender=back.account,
        receiver=out.account,
        amount_ore=back.amount_ore,
        sender_text=f"Tilbageført: {back.text}",
        receiver_text=f"Tilbageført: {out.text}",
        at=at,
        dedupe_token=token(f"{label}-void", pk),
        source=f"{SOURCE}:{label}-void:{pk}",
        reverses=transfer,
    )
    return 1


def _deposit(
    m: Models,
    row: Any,  # noqa: ANN401
    own: ledger.Accounts,
    accounts: dict[int, CreditAccount],
    names: dict[int, str],
    voided: dict[str, set[int]],
) -> tuple[int, int]:
    made, written = _write(
        m.Deposit,
        row,
        "transfer",
        kind=Transfer.Kind.TRANSFER,
        sender=own.bank,
        receiver=accounts[row.shopper_id],
        amount_ore=row.amount_ore,
        sender_text=f"Indbetaling fra {names[row.shopper_id]}",
        receiver_text="Indbetaling",
        at=row.created_at,
        label="deposit",
    )
    undone = _undo(made, row.created_at, "deposit", row.pk) if row.pk in voided["deposit"] else 0
    return written, undone


def _basket(
    m: Models,
    rows: _Rows,
    pk: int,
    own: ledger.Accounts,
    accounts: dict[int, CreditAccount],
    names: dict[int, str],
    voided: dict[str, set[int]],
) -> tuple[int, int]:
    basket = rows.baskets[pk]
    what = rows.items.get(pk, "") or "Køb"
    written = undone = 0
    for share in rows.shares.get(pk, []):
        made, now = _write(
            m.PurchaseShare,
            share,
            "transfer",
            kind=Transfer.Kind.TRANSFER,
            sender=accounts[share.shopper_id],
            receiver=own.sales,
            amount_ore=share.share_ore,
            sender_text=f"Køb #{pk}: {what}",
            receiver_text=f"Køb #{pk}: {names[share.shopper_id]}",
            at=basket.created_at,
            label="purchase",
        )
        written += now
        if pk in voided["transaction"]:
            undone += _undo(made, basket.created_at, "purchase", share.pk)
    return min(written, 1), undone


def _adjustment(
    m: Models,
    row: Any,  # noqa: ANN401
    own: ledger.Accounts,
    accounts: dict[int, CreditAccount],
    names: dict[int, str],
    voided: dict[str, set[int]],
) -> tuple[int, int]:
    shopper = accounts[row.shopper_id]
    into = own.interest if row.kind == "interest" else own.adjustments
    text = row.reason or ("Rente" if row.kind == "interest" else "Justering")
    if row.amount_ore < 0:  # more debt: taken from the shopper
        sender, receiver, kind = shopper, into, Transfer.Kind.CHARGE
        sender_text, receiver_text = text, f"{names[row.shopper_id]}: {text}"
    else:  # a credit: given to the shopper
        sender, receiver, kind = into, shopper, Transfer.Kind.TRANSFER
        sender_text, receiver_text = f"{names[row.shopper_id]}: {text}", text
    made, written = _write(
        m.Adjustment,
        row,
        "transfer",
        kind=kind,
        sender=sender,
        receiver=receiver,
        amount_ore=abs(row.amount_ore),
        sender_text=sender_text,
        receiver_text=receiver_text,
        at=row.created_at,
        label="adjustment",
    )
    undone = _undo(made, row.created_at, "adjustment", row.pk) if row.pk in voided["adjustment"] else 0
    return written, undone
