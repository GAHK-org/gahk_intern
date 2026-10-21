"""residents.ledger — the local, immutable mirror of e-conomic postings (F-010 regnskab role).

e-conomic itself stays the real books; EconomicLedgerEntry is GAHK's own permanent, append-only copy.
economic.get_entries is monkeypatched directly (see test_economic.py for the client against the real
e-conomic demo API).
"""

from collections.abc import Callable
from decimal import Decimal

import pytest

from residents import economic, ledger
from residents.models import EconomicLedgerEntry


def _entry(number: int, amount: float, date: str = "2026-08-01") -> economic.LedgerEntry:
    return economic.LedgerEntry(
        entry_number=number,
        date=date,
        text="x",
        amount=amount,
        entry_type="customerInvoice",
        voucher_number=1,
        due_date=None,
    )


@pytest.mark.django_db
def test_sync_inserts_new_entries(monkeypatch: pytest.MonkeyPatch, make_resident: Callable) -> None:
    resident = make_resident(email="a@gahk.dk", economic_customer_number=1)
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, 100.0), _entry(2, -40.0)])

    inserted = ledger.sync_entries(resident, 1)

    assert inserted == 2
    assert EconomicLedgerEntry.objects.filter(resident=resident).count() == 2


@pytest.mark.django_db
def test_sync_is_insert_only_and_idempotent(monkeypatch: pytest.MonkeyPatch, make_resident: Callable) -> None:
    resident = make_resident(email="a@gahk.dk", economic_customer_number=1)
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, 100.0)])
    ledger.sync_entries(resident, 1)

    # e-conomic returns the same entry again (a normal re-sync) plus one genuinely new one.
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, 100.0), _entry(2, -25.0)])
    inserted = ledger.sync_entries(resident, 1)

    assert inserted == 1  # only the new one
    rows = EconomicLedgerEntry.objects.filter(resident=resident).order_by("entry_number")
    assert [r.entry_number for r in rows] == [1, 2]
    assert rows[0].amount == Decimal("100.00")  # the original row, untouched by the re-sync


@pytest.mark.django_db
def test_balance_sums_entries_with_economics_own_sign(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    resident = make_resident(email="a@gahk.dk", economic_customer_number=1)
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, 250.0), _entry(2, -87.5)])
    ledger.sync_entries(resident, 1)

    assert ledger.balance(resident) == Decimal("162.50")


@pytest.mark.django_db
def test_balance_is_zero_with_no_entries(make_resident: Callable) -> None:
    resident = make_resident(email="a@gahk.dk", economic_customer_number=1)

    assert ledger.balance(resident) == Decimal(0)
