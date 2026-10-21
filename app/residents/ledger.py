"""Syncs each alumne's e-conomic postings into the local, immutable EconomicLedgerEntry mirror
(F-010 regnskab, alumneregning automation). See EconomicLedgerEntry's docstring for why this mirror
exists: e-conomic stays the real books, this is GAHK's own append-only copy of it.

Insert-only and idempotent: entry_number is e-conomic's own id for a posting and is unique here, so
re-syncing a resident already covered by a previous sync just finds nothing new — existing rows are
never updated or deleted, which is what "immutable" means for a mirror of someone else's ledger.
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum

from . import economic
from .models import EconomicLedgerEntry, Resident


def sync_entries(resident: Resident, customer_number: int) -> int:
    """Pull every e-conomic posting for one resident and store the ones not already mirrored locally.
    Returns how many new rows were inserted."""
    known = set(EconomicLedgerEntry.objects.filter(resident=resident).values_list("entry_number", flat=True))
    new_entries = [
        EconomicLedgerEntry(
            resident=resident,
            entry_number=entry.entry_number,
            date=entry.date,
            text=entry.text,
            amount=Decimal(str(entry.amount)),
            entry_type=entry.entry_type,
            voucher_number=entry.voucher_number or None,
            due_date=entry.due_date or None,
        )
        for entry in economic.get_entries(customer_number)
        if entry.entry_number not in known
    ]
    EconomicLedgerEntry.objects.bulk_create(new_entries)
    return len(new_entries)


def balance(resident: Resident) -> Decimal:
    """The resident's balance as GAHK's own ledger mirror currently has it — positive = they owe
    GAHK, matching e-conomic's own sign convention (EconomicLedgerEntry.amount)."""
    total = resident.economic_entries.aggregate(total=Sum("amount"))["total"]
    return total or Decimal(0)
