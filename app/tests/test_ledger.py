"""residents.ledger — the local, immutable mirror of e-conomic postings (F-010 regnskab role).

e-conomic itself stays the real books; EconomicLedgerEntry is GAHK's own permanent, append-only copy.
sync_entries is handed economic.LedgerEntry objects directly (it does not fetch them itself — see
residents.alumneregning for the actual e-conomic calls, residents.economic for the client against the
real e-conomic demo API).
"""

from collections.abc import Callable
from decimal import Decimal

import pytest

from residents import economic, ledger
from residents.models import EconomicLedgerEntry


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
def test_sync_is_insert_only_and_idempotent(make_resident: Callable) -> None:
    resident = make_resident(email="a@gahk.dk", economic_account_number=1101)
    ledger.sync_entries(resident, [_entry(1, 100.0)])

    # e-conomic returns the same entry again (a normal re-sync) plus one genuinely new one.
    inserted = ledger.sync_entries(resident, [_entry(1, 100.0), _entry(2, -25.0)])

    assert inserted == 1  # only the new one
    rows = EconomicLedgerEntry.objects.filter(resident=resident).order_by("entry_number")
    assert [r.entry_number for r in rows] == [1, 2]
    assert rows[0].amount == Decimal("100.00")  # the original row, untouched by the re-sync


@pytest.mark.django_db
def test_balance_sums_entries_with_economics_own_sign(make_resident: Callable) -> None:
    resident = make_resident(email="a@gahk.dk", economic_account_number=1101)
    ledger.sync_entries(resident, [_entry(1, 250.0), _entry(2, -87.5)])

    assert ledger.balance(resident) == Decimal("162.50")


@pytest.mark.django_db
def test_balance_is_zero_with_no_entries(make_resident: Callable) -> None:
    resident = make_resident(email="a@gahk.dk", economic_account_number=1101)

    assert ledger.balance(resident) == Decimal(0)


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
    assert ledger.balance(resident) == Decimal("150.00")
