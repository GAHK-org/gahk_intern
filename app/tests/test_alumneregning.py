"""residents.alumneregning — the "one button" monthly alumni billing mail (F-010 regnskab role).

economic.get_entries/get_booked_invoices/get_invoice_pdf are monkeypatched directly: these tests
cover alumneregning's own logic (who gets mailed, which invoice, what happens on failure, and that a
send mirrors entries into the local ledger first), not the e-conomic client itself (see
test_economic.py) or the real API (verified manually during development).
"""

from collections.abc import Callable

import pytest
from django.core import mail

from residents import alumneregning, economic
from residents.models import EconomicLedgerEntry


def _invoice(number: int, date: str, pdf_url: str = "https://example/pdf") -> economic.BookedInvoice:
    return economic.BookedInvoice(
        booked_invoice_number=number, date=date, due_date=None, remainder=0.0, pdf_url=pdf_url
    )


def _entry(number: int, date: str, amount: float) -> economic.LedgerEntry:
    return economic.LedgerEntry(
        entry_number=number,
        date=date,
        text="Invoice",
        amount=amount,
        entry_type="customerInvoice",
        voucher_number=1,
        due_date=None,
    )


@pytest.mark.django_db
def test_sends_mail_with_latest_invoice_attached(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_customer_number=42)
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, "2026-08-01", 250.0)])
    monkeypatch.setattr(
        economic,
        "get_booked_invoices",
        lambda n: [_invoice(1, "2026-07-01"), _invoice(2, "2026-08-01")],  # newer should win
    )
    monkeypatch.setattr(economic, "get_invoice_pdf", lambda url: b"%PDF-fake")

    results = alumneregning.send_alumneregninger()

    assert len(results) == 1
    assert results[0].resident == resident
    assert results[0].outcome == "sent"
    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == ["alumne@gahk.dk"]
    assert sent.subject == "Alumneregning"
    assert "250,00 kr" in sent.body
    assert "din gæld til kollegiet" in sent.body
    assert len(sent.attachments) == 1
    filename, content, content_type = sent.attachments[0]
    assert filename == "alumneregning-2026-08-01.pdf"  # the NEWER invoice, not the first one returned
    assert content == b"%PDF-fake"
    assert content_type == "application/pdf"
    # The balance quoted in the mail came from a row actually mirrored into the local ledger.
    assert EconomicLedgerEntry.objects.filter(resident=resident, entry_number=1).exists()


@pytest.mark.django_db
def test_sync_is_idempotent_across_sends(monkeypatch: pytest.MonkeyPatch, make_resident: Callable) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_customer_number=42)
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, "2026-08-01", 250.0)])
    monkeypatch.setattr(economic, "get_booked_invoices", lambda n: [_invoice(1, "2026-08-01")])
    monkeypatch.setattr(economic, "get_invoice_pdf", lambda url: b"%PDF-fake")

    alumneregning.send_alumneregninger()
    alumneregning.send_alumneregninger()  # e-conomic returns the same entry again on re-sync

    assert EconomicLedgerEntry.objects.filter(resident=resident).count() == 1  # not duplicated
    assert "250,00 kr" in mail.outbox[1].body  # balance still correct, not doubled to 500


@pytest.mark.django_db
def test_credit_balance_gets_the_other_wording(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    make_resident(email="alumne@gahk.dk", economic_customer_number=42)
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, "2026-08-01", -100.0)])
    monkeypatch.setattr(economic, "get_booked_invoices", lambda n: [_invoice(1, "2026-08-01")])
    monkeypatch.setattr(economic, "get_invoice_pdf", lambda url: b"%PDF-fake")

    alumneregning.send_alumneregninger()

    assert "kollegiet skylder dig penge" in mail.outbox[0].body


@pytest.mark.django_db
def test_skips_residents_without_a_customer_number(make_resident: Callable) -> None:
    make_resident(email="no-number@gahk.dk")

    results = alumneregning.send_alumneregninger()

    assert results == []
    assert mail.outbox == []


@pytest.mark.django_db
def test_reports_no_invoices_without_sending(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    make_resident(email="alumne@gahk.dk", economic_customer_number=42)
    monkeypatch.setattr(economic, "get_entries", lambda n: [])
    monkeypatch.setattr(economic, "get_booked_invoices", lambda n: [])

    results = alumneregning.send_alumneregninger()

    assert results[0].outcome == "no_invoices"
    assert mail.outbox == []


@pytest.mark.django_db
def test_one_failure_does_not_stop_the_rest(monkeypatch: pytest.MonkeyPatch, make_resident: Callable) -> None:
    failing = make_resident(email="fails@gahk.dk", economic_customer_number=1, first_name="Fail")
    ok = make_resident(email="ok@gahk.dk", economic_customer_number=2, first_name="Ok")

    def get_entries(customer_number: int) -> list[economic.LedgerEntry]:
        if customer_number == 1:
            raise economic.EconomicApiError("e-conomic 500")
        return [_entry(1, "2026-08-01", 0.0)]

    monkeypatch.setattr(economic, "get_entries", get_entries)
    monkeypatch.setattr(economic, "get_booked_invoices", lambda n: [_invoice(1, "2026-08-01")])
    monkeypatch.setattr(economic, "get_invoice_pdf", lambda url: b"%PDF-fake")

    results = alumneregning.send_alumneregninger()

    by_resident = {r.resident.pk: r for r in results}
    assert by_resident[failing.pk].outcome == "failed"
    assert by_resident[ok.pk].outcome == "sent"
    assert len(mail.outbox) == 1  # only the one that succeeded
    assert not EconomicLedgerEntry.objects.filter(resident=failing).exists()  # failed sync wrote nothing
