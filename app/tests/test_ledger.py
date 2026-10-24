"""regnskab.ledger — the local, immutable mirror of e-conomic postings (F-010 regnskab role)."""

from collections.abc import Callable
from decimal import Decimal

import pytest

from regnskab import economic, ledger
from regnskab.models import EconomicLedgerEntry


def _entry(
    number: int, amount: float, date: str = "2026-08-01", is_draft: bool = False
) -> economic.LedgerEntry:
    return economic.LedgerEntry(
        entry_number=number,
        is_draft=is_draft,
        date=date,
        text="x",
        amount=amount,
        entry_type="financeVoucher",
        voucher_number=1,
        due_date=None,
    )


@pytest.mark.django_db
def test_sync_inserts_new_entries(make_resident: Callable) -> None:
    resident = make_resident(email="a@gahk.dk", economic_account_number=1101)

    inserted = ledger.sync_entries(resident, [_entry(1, 100.0), _entry(2, -40.0)])

    assert inserted == 2
    assert EconomicLedgerEntry.objects.filter(resident=resident).count() == 2


@pytest.mark.django_db
def test_sync_is_insert_only_and_idempotent_for_posted_entries(make_resident: Callable) -> None:
    resident = make_resident(email="a@gahk.dk", economic_account_number=1101)
    ledger.sync_entries(resident, [_entry(1, 100.0)])

    # e-conomic returns the same entry again (a normal re-sync) plus one genuinely new one.
    inserted = ledger.sync_entries(resident, [_entry(1, 100.0), _entry(2, -25.0)])

    assert inserted == 1  # only the new one
    rows = EconomicLedgerEntry.objects.filter(resident=resident).order_by("entry_number")
    assert [r.entry_number for r in rows] == [1, 2]
    assert rows[0].amount == Decimal("100.00")  # the original row, untouched by the re-sync


@pytest.mark.django_db
def test_posted_and_draft_entries_with_the_same_number_do_not_collide(make_resident: Callable) -> None:
    """entryNumber (posted) and journalEntryNumber (draft) are independent e-conomic counters and can
    coincide numerically — both must be kept, not deduplicated against each other."""
    resident = make_resident(email="a@gahk.dk", economic_account_number=1101)

    inserted = ledger.sync_entries(
        resident, [_entry(1, 100.0, is_draft=False), _entry(1, 50.0, is_draft=True)]
    )

    assert inserted == 2
    assert EconomicLedgerEntry.objects.filter(resident=resident, entry_number=1).count() == 2


@pytest.mark.django_db
def test_same_entry_number_for_two_different_residents_does_not_collide(make_resident: Callable) -> None:
    """entry_number is not assumed unique company-wide — the uniqueness constraint is per-resident,
    matching sync_entries's own dedup scope, so two residents never collide on the same number."""
    a = make_resident(email="a@gahk.dk", economic_account_number=1101)
    b = make_resident(email="b@gahk.dk", economic_account_number=1102)
    ledger.sync_entries(a, [_entry(1, 100.0)])

    inserted = ledger.sync_entries(b, [_entry(1, 50.0)])

    assert inserted == 1
    assert EconomicLedgerEntry.objects.filter(entry_number=1).count() == 2


@pytest.mark.django_db
def test_a_draft_no_longer_present_is_removed_on_the_next_sync(make_resident: Callable) -> None:
    """Drafts are mutable in e-conomic (edited, deleted, or formally posted under a new entryNumber) —
    a sync replaces this resident's mirrored drafts wholesale, so a stale one never lingers."""
    resident = make_resident(email="a@gahk.dk", economic_account_number=1101)
    ledger.sync_entries(resident, [_entry(10, 5.0, is_draft=True), _entry(11, 7.0, is_draft=True)])

    # e-conomic now only reports draft 10 — 11 was withdrawn/posted/deleted.
    ledger.sync_entries(resident, [_entry(10, 5.0, is_draft=True)])

    drafts = EconomicLedgerEntry.objects.filter(resident=resident, is_draft=True)
    assert [d.entry_number for d in drafts] == [10]


@pytest.mark.django_db
def test_a_posted_draft_leaves_both_the_new_posted_row_and_no_stale_draft(make_resident: Callable) -> None:
    resident = make_resident(email="a@gahk.dk", economic_account_number=1101)
    ledger.sync_entries(resident, [_entry(10, 5.0, is_draft=True)])

    # Formally posted: a new, independent entryNumber, is_draft=False — draft 10 no longer exists.
    ledger.sync_entries(resident, [_entry(99, 5.0, is_draft=False)])

    assert not EconomicLedgerEntry.objects.filter(resident=resident, is_draft=True).exists()
    assert EconomicLedgerEntry.objects.filter(resident=resident, entry_number=99, is_draft=False).exists()
