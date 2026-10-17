"""ETL: ølkælder POS (F-003).

Money is already in øre in the legacy data, imported as-is into the *_ore fields. Shoppers attach to a
migrated resident (former-resident shoppers are skipped). The legacy `intern_oelkaelder_purchase` stored
no per-shopper amount — we reconstruct each transaction's split here with **largest-remainder** rounding
so the shares sum exactly to the item total, and the new balance is derived from this ledger.

High-volume tables (transactions, items, shares, deposits, the ~100k-row log) are bulk-created.
"""

import json

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.etl import fetch_all, resident_id_remap
from oelkaelder.models import (
    Deposit,
    LogEntry,
    Product,
    PurchaseShare,
    Shopper,
    Transaction,
    TransactionItem,
    Warning,
)


def largest_remainder(total: int, n: int) -> list[int]:
    """Split `total` into n integer parts summing exactly to total (first parts get the +1s)."""
    base, rem = divmod(total, n)
    return [base + 1 if i < rem else base for i in range(n)]


class Command(BaseCommand):
    help = "Migrate the ølkælder POS data from the legacy DB."

    @transaction.atomic
    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        from residents.api import Resident

        remap = resident_id_remap()
        resident_ids = set(Resident.objects.values_list("id", flat=True))

        # ---- products ----
        for p in fetch_all("SELECT * FROM intern_oelkaelder_product"):
            steps = None
            if p["price_steps"]:
                try:
                    steps = json.loads(p["price_steps"])
                except (ValueError, TypeError):
                    steps = None
            Product.objects.update_or_create(
                id=p["productId"],
                defaults={
                    "name": (p["name"] or "").strip(),
                    "price_ore": max(p["current_price"] or 0, 0),
                    "weight_price_ore": p["weight_price"] or None,
                    "price_steps": steps,
                    "image": (p["imageurl"] or "").strip().lstrip("/"),
                    "active": bool(p["active"]),
                    "highlighted": bool(p["highlighted"]),
                },
            )

        # ---- shoppers (join saldo for active flag); skip shoppers without a migrated resident ----
        saldo_active = {
            r["shopperId"]: bool(r["active"]) for r in fetch_all("SELECT * FROM intern_oelkaelder_saldo")
        }
        shopper_ok = set()
        shopper_skipped = 0
        for s in fetch_all("SELECT * FROM intern_shopper"):
            rid = remap.get(s["alumnumId"])
            if rid not in resident_ids:
                shopper_skipped += 1
                continue
            Shopper.objects.update_or_create(
                id=s["shopperId"],
                defaults={"resident_id": rid, "active": saldo_active.get(s["shopperId"], True)},
            )
            shopper_ok.add(s["shopperId"])

        # ---- transactions (delete cascades to items/shares) ----
        Transaction.objects.all().delete()
        Transaction.objects.bulk_create(
            [
                Transaction(id=t["ID"], created_at=t["time"] or timezone.now(), is_valid=bool(t["valid"]))
                for t in fetch_all("SELECT * FROM intern_oelkaelder_transaction")
            ],
            batch_size=2000,
        )
        txn_ids = set(Transaction.objects.values_list("id", flat=True))

        # ---- deposits ----
        Deposit.objects.all().delete()
        deps, dep_skipped = [], 0
        for d in fetch_all("SELECT * FROM intern_oelkaelder_deposit"):
            if d["shopperId"] not in shopper_ok:
                dep_skipped += 1
                continue
            deps.append(
                Deposit(
                    id=d["ID"],
                    shopper_id=d["shopperId"],
                    amount_ore=max(d["amount"] or 0, 0),
                    created_at=d["time"] or timezone.now(),
                    is_valid=bool(d["valid"]),
                )
            )
        Deposit.objects.bulk_create(deps, batch_size=2000)

        # ---- items (+ accumulate per-transaction totals) ----
        items: list[TransactionItem] = []
        item_total: dict[int, int] = {}
        for it in fetch_all("SELECT * FROM intern_oelkaelder_transaction_item"):
            if it["transactionId"] not in txn_ids:
                continue
            price = max(it["price"] or 0, 0)
            items.append(
                TransactionItem(
                    transaction_id=it["transactionId"],
                    product_id=it["productId"],
                    quantity=it["quantity"] or 1,
                    price_ore=price,
                )
            )
            item_total[it["transactionId"]] = item_total.get(it["transactionId"], 0) + price
        TransactionItem.objects.bulk_create(items, batch_size=2000)

        # ---- reconstructed purchase shares (largest-remainder) ----
        buyers: dict[int, list[int]] = {}
        for pr in fetch_all("SELECT * FROM intern_oelkaelder_purchase"):
            if pr["transactionId"] in txn_ids and pr["shopperId"] in shopper_ok:
                buyers.setdefault(pr["transactionId"], []).append(pr["shopperId"])
        shares = []
        for tid, sids in buyers.items():
            sids = sorted(set(sids))
            for sid, amount in zip(sids, largest_remainder(item_total.get(tid, 0), len(sids)), strict=False):
                shares.append(PurchaseShare(transaction_id=tid, shopper_id=sid, share_ore=amount))
        PurchaseShare.objects.bulk_create(shares, batch_size=2000)

        # ---- warnings + log ----
        for w in fetch_all("SELECT * FROM intern_oelkaelder_warnings"):
            Warning.objects.update_or_create(
                id=w["id"],
                defaults={
                    "message": w["message"] or "",
                    "threshold_ore": w["amount"],
                    "active": bool(w["active"]),
                },
            )
        LogEntry.objects.all().delete()
        LogEntry.objects.bulk_create(
            [
                LogEntry(id=lg["ID"], created_at=lg["time"] or timezone.now(), message=lg["log"] or "")
                for lg in fetch_all("SELECT * FROM intern_oelkaelder_log")
            ],
            batch_size=5000,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Ølkælder: {Product.objects.count()} products, {Shopper.objects.count()} shoppers "
                f"(skipped {shopper_skipped} w/o migrated resident), {Deposit.objects.count()} deposits "
                f"(skipped {dep_skipped}), {Transaction.objects.count()} transactions, "
                f"{TransactionItem.objects.count()} items, {len(shares)} purchase shares, "
                f"{Warning.objects.count()} warnings, {LogEntry.objects.count()} log rows."
            )
        )
