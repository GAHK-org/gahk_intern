from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from . import economic
from .models import EconomicLedgerEntry, Resident


def sync_entries(resident: Resident, entries: Iterable[economic.LedgerEntry]) -> int:
    """Store the given e-conomic entries for `resident`, skipping any already mirrored locally.
    Returns how many new rows were inserted."""
    known = set(EconomicLedgerEntry.objects.filter(resident=resident).values_list("entry_number", "is_draft"))
    new_entries = [
        EconomicLedgerEntry(
            resident=resident,
            entry_number=entry.entry_number,
            is_draft=entry.is_draft,
            date=entry.date,
            text=entry.text,
            amount=Decimal(str(entry.amount)),
            entry_type=entry.entry_type,
            voucher_number=entry.voucher_number or None,
            due_date=entry.due_date or None,
        )
        for entry in entries
        if (entry.entry_number, entry.is_draft) not in known
    ]
    EconomicLedgerEntry.objects.bulk_create(new_entries)
    return len(new_entries)
