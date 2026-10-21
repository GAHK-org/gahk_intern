"""Mirrors e-conomic postings into the local, immutable EconomicLedgerEntry table (F-010 regnskab,
alumneregning automation). See EconomicLedgerEntry's docstring for why this mirror exists: e-conomic
stays the real books, this is GAHK's own append-only copy of it.

Insert-only and idempotent: (entry_number, is_draft) is e-conomic's own id for a posting (entryNumber
for a posted entry, journalEntryNumber for a draft — two independent counters that can collide
numerically, hence the pair) and is unique here, so re-syncing a resident already covered by a
previous sync just finds nothing new — existing rows are never updated or deleted, which is what
"immutable" means for a mirror of someone else's ledger. One real consequence of that for drafts
specifically: e-conomic drafts are themselves mutable (editable/deletable there) and get a brand new
entryNumber once formally posted, so a draft mirrored here can end up double-counted against its own
later posted version if both get synced — this module does not attempt to detect or merge that; it is
why alumneregning's actual quoted balance comes from economic.get_account() instead of summing what
is mirrored here (see balance()'s docstring).

Deliberately decoupled from residents.economic: this module only persists economic.LedgerEntry
objects it is handed, it does not fetch them itself. The caller (residents.alumneregning) decides
which e-conomic period's entries are relevant to a given send; this module's job is just the
immutable local copy, not that business decision.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from django.db.models import Sum

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


def balance(resident: Resident) -> Decimal:
    """The sum of everything currently mirrored locally for this resident — positive = they owe GAHK,
    matching e-conomic's own sign convention (EconomicLedgerEntry.amount). Not what alumneregning
    mails quote: a send only ever mirrors one chosen date range's entries at a time (see
    residents.alumneregning), so this all-time local total can be incomplete if e-conomic has postings
    from periods never synced. alumneregning mails use economic.get_account's own balance instead,
    which is always authoritative regardless of what has been synced locally. This is a
    general-purpose inspection helper over whatever has been mirrored so far."""
    total = resident.economic_entries.aggregate(total=Sum("amount"))["total"]
    return total or Decimal(0)
