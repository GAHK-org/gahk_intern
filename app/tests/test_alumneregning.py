"""residents.alumneregning — the "one button" monthly alumni billing mail (F-010 regnskab role).

economic.get_entries/get_booked_invoices/get_invoice_pdf are monkeypatched directly: these tests
cover alumneregning's own logic (who gets mailed, which invoice, what happens on failure, that a send
mirrors entries into the local ledger first, and the date-range filter on which invoice gets picked),
not the e-conomic client itself (see test_economic.py) or the real API (verified manually during
development).
"""

from collections.abc import Callable
from datetime import date

import pytest
from django.core import mail
from django.core.files.base import ContentFile

from residents import alumneregning, economic
from residents.models import AlumneregningSettings, EconomicLedgerEntry

AUGUST = (date(2026, 8, 1), date(2026, 8, 31))


def _invoice(number: int, date_str: str, pdf_url: str = "https://example/pdf") -> economic.BookedInvoice:
    return economic.BookedInvoice(
        booked_invoice_number=number, date=date_str, due_date=None, remainder=0.0, pdf_url=pdf_url
    )


def _entry(number: int, date_str: str, amount: float) -> economic.LedgerEntry:
    return economic.LedgerEntry(
        entry_number=number,
        date=date_str,
        text="Invoice",
        amount=amount,
        entry_type="customerInvoice",
        voucher_number=1,
        due_date=None,
    )


@pytest.mark.django_db
def test_sends_mail_with_invoice_from_the_chosen_range(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_customer_number=42)
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, "2026-08-01", 250.0)])
    monkeypatch.setattr(
        economic,
        "get_booked_invoices",
        lambda n: [_invoice(1, "2026-07-01"), _invoice(2, "2026-08-01")],  # July is out of range
    )
    monkeypatch.setattr(economic, "get_invoice_pdf", lambda url: b"%PDF-fake")

    results = alumneregning.send_alumneregninger(*AUGUST)

    assert len(results) == 1
    assert results[0].resident == resident
    assert results[0].outcome == "sent"
    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == ["alumne@gahk.dk"]
    assert sent.subject == "Alumneregning"
    assert "250,00 kr" in sent.body
    assert "din gæld til kollegiet" in sent.body
    filename, content, content_type = sent.attachments[0]
    assert filename == "alumneregning-2026-08-01.pdf"  # the August invoice, not the July one
    assert content == b"%PDF-fake"
    assert content_type == "application/pdf"
    assert EconomicLedgerEntry.objects.filter(resident=resident, entry_number=1).exists()


@pytest.mark.django_db
def test_no_invoice_in_range_is_reported_without_sending(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    make_resident(email="alumne@gahk.dk", economic_customer_number=42)
    monkeypatch.setattr(economic, "get_entries", lambda n: [])
    monkeypatch.setattr(
        economic, "get_booked_invoices", lambda n: [_invoice(1, "2026-07-01")]
    )  # out of range

    results = alumneregning.send_alumneregninger(*AUGUST)

    assert results[0].outcome == "no_invoices"
    assert "2026-08-01" in results[0].detail
    assert "2026-08-31" in results[0].detail
    assert mail.outbox == []


@pytest.mark.django_db
def test_picks_latest_invoice_within_range(monkeypatch: pytest.MonkeyPatch, make_resident: Callable) -> None:
    make_resident(email="alumne@gahk.dk", economic_customer_number=42)
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, "2026-08-01", 0.0)])
    monkeypatch.setattr(
        economic,
        "get_booked_invoices",
        lambda n: [_invoice(1, "2026-08-05"), _invoice(2, "2026-08-20")],  # both in range
    )
    monkeypatch.setattr(economic, "get_invoice_pdf", lambda url: b"%PDF-fake")

    alumneregning.send_alumneregninger(*AUGUST)

    filename, _content, _content_type = mail.outbox[0].attachments[0]
    assert filename == "alumneregning-2026-08-20.pdf"


@pytest.mark.django_db
def test_sync_is_idempotent_across_sends(monkeypatch: pytest.MonkeyPatch, make_resident: Callable) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_customer_number=42)
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, "2026-08-01", 250.0)])
    monkeypatch.setattr(economic, "get_booked_invoices", lambda n: [_invoice(1, "2026-08-01")])
    monkeypatch.setattr(economic, "get_invoice_pdf", lambda url: b"%PDF-fake")

    alumneregning.send_alumneregninger(*AUGUST)
    alumneregning.send_alumneregninger(*AUGUST)  # e-conomic returns the same entry again on re-sync

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

    alumneregning.send_alumneregninger(*AUGUST)

    assert "kollegiet skylder dig penge" in mail.outbox[0].body


@pytest.mark.django_db
def test_skips_residents_without_a_customer_number(make_resident: Callable) -> None:
    make_resident(email="no-number@gahk.dk")

    results = alumneregning.send_alumneregninger(*AUGUST)

    assert results == []
    assert mail.outbox == []


@pytest.mark.django_db
def test_uses_the_regnskab_edited_message_template(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    make_resident(email="alumne@gahk.dk", economic_customer_number=42, first_name="Tove")
    AlumneregningSettings.objects.create(
        pk=1, message_template="Kære $FORNAVN. $SALDO_LINE Konto: $KONTO_NR."
    )
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, "2026-08-01", 100.0)])
    monkeypatch.setattr(economic, "get_booked_invoices", lambda n: [_invoice(1, "2026-08-01")])
    monkeypatch.setattr(economic, "get_invoice_pdf", lambda url: b"%PDF-fake")

    alumneregning.send_alumneregninger(*AUGUST)

    assert (
        mail.outbox[0].body
        == "Kære Tove. Din saldo er nu 100,00 kr. — det er din gæld til kollegiet. Konto: 1642410853."
    )


@pytest.mark.django_db
def test_unknown_placeholder_in_edited_template_renders_literally(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    """A regnskab typo ($SALDOO) must not break the send for everyone — safe_substitute leaves it as-is."""
    make_resident(email="alumne@gahk.dk", economic_customer_number=42)
    AlumneregningSettings.objects.create(pk=1, message_template="Saldo: $SALDOO")
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, "2026-08-01", 0.0)])
    monkeypatch.setattr(economic, "get_booked_invoices", lambda n: [_invoice(1, "2026-08-01")])
    monkeypatch.setattr(economic, "get_invoice_pdf", lambda url: b"%PDF-fake")

    results = alumneregning.send_alumneregninger(*AUGUST)

    assert results[0].outcome == "sent"
    assert mail.outbox[0].body == "Saldo: $SALDOO"


@pytest.mark.django_db
def test_attaches_the_followup_pdf_when_one_is_set(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    make_resident(email="alumne@gahk.dk", economic_customer_number=42)
    mail_settings = AlumneregningSettings.objects.create(pk=1)
    mail_settings.followup_pdf.save("folgebrev.pdf", ContentFile(b"%PDF-followup"), save=True)
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, "2026-08-01", 0.0)])
    monkeypatch.setattr(economic, "get_booked_invoices", lambda n: [_invoice(1, "2026-08-01")])
    monkeypatch.setattr(economic, "get_invoice_pdf", lambda url: b"%PDF-invoice")

    alumneregning.send_alumneregninger(*AUGUST)

    assert len(mail.outbox[0].attachments) == 2
    names = {a[0] for a in mail.outbox[0].attachments}
    assert names == {"alumneregning-2026-08-01.pdf", "folgebrev.pdf"}


@pytest.mark.django_db
def test_no_followup_pdf_means_one_attachment(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    make_resident(email="alumne@gahk.dk", economic_customer_number=42)
    monkeypatch.setattr(economic, "get_entries", lambda n: [_entry(1, "2026-08-01", 0.0)])
    monkeypatch.setattr(economic, "get_booked_invoices", lambda n: [_invoice(1, "2026-08-01")])
    monkeypatch.setattr(economic, "get_invoice_pdf", lambda url: b"%PDF-invoice")

    alumneregning.send_alumneregninger(*AUGUST)

    assert len(mail.outbox[0].attachments) == 1


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

    results = alumneregning.send_alumneregninger(*AUGUST)

    by_resident = {r.resident.pk: r for r in results}
    assert by_resident[failing.pk].outcome == "failed"
    assert by_resident[ok.pk].outcome == "sent"
    assert len(mail.outbox) == 1  # only the one that succeeded
    assert not EconomicLedgerEntry.objects.filter(resident=failing).exists()  # failed sync wrote nothing
