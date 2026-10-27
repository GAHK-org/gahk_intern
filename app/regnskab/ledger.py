from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from residents.models import Resident

from . import economic
from .models import EconomicLedgerEntry


def _row(resident: Resident, entry: economic.LedgerEntry) -> EconomicLedgerEntry:
    return EconomicLedgerEntry(
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


def sync_entries(resident: Resident, entries: Iterable[economic.LedgerEntry]) -> int:
    """Mirrors the given e-conomic entries for `resident`. Posted entries are insert-only (e-conomic
    never changes a posted entry once it exists). Drafts are different: e-conomic's own draft can be
    edited or deleted, and gets a brand new entryNumber once formally posted — so a stale draft row
    would otherwise linger locally forever, double-counting anything that later sums this table. Every
    sync therefore replaces this resident's mirrored drafts wholesale with the current set, rather
    than inserting and leaving old ones behind.

    Returns how many rows were newly inserted (posted) or (re)written (drafts)."""
    entries = list(entries)
    posted = [e for e in entries if not e.is_draft]
    drafts = [e for e in entries if e.is_draft]

    known_posted = set(
        EconomicLedgerEntry.objects.filter(resident=resident, is_draft=False).values_list(
            "entry_number", flat=True
        )
    )
    new_posted = [_row(resident, e) for e in posted if e.entry_number not in known_posted]
    EconomicLedgerEntry.objects.bulk_create(new_posted)

    EconomicLedgerEntry.objects.filter(resident=resident, is_draft=True).delete()
    EconomicLedgerEntry.objects.bulk_create([_row(resident, e) for e in drafts])

    return len(new_posted) + len(drafts)
