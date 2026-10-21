"""residents.alumneregning — the "one button" billing mail for a chosen period (F-010 regnskab role).

economic.get_account/get_entries_in_range are monkeypatched directly: these tests cover
alumneregning's own logic (who gets mailed, the generated statement PDF, what happens on failure,
that a send mirrors entries into the local ledger, the Residency-based eligibility filter, and that
an alumne with a balance but no new postings still gets billed), not the e-conomic client itself (see
test_economic.py) or the real API (verified manually during development).
"""

import io
from collections.abc import Callable
from datetime import date

import pytest
from django.core import mail
from django.core.files.base import ContentFile
from fpdf import FPDF
from pypdf import PdfReader

from core.models import Room
from residents import alumneregning, economic
from residents.models import AlumneregningSettings, EconomicLedgerEntry, Residency, Resident

AUGUST = (date(2026, 8, 1), date(2026, 8, 31))


def _account(balance: float, name: str = "Alumne") -> economic.EconomicAccount:
    return economic.EconomicAccount(account_number=1101, name=name, balance=balance)


def _entry(number: int, date_str: str, amount: float, is_draft: bool = False) -> economic.LedgerEntry:
    return economic.LedgerEntry(
        entry_number=number,
        is_draft=is_draft,
        date=date_str,
        text="Husleje",
        amount=amount,
        entry_type="financeVoucher",
        voucher_number=1,
        due_date=None,
    )


def _one_page_pdf(text: str) -> bytes:
    """A minimal real PDF (not just PDF-looking bytes) — pypdf.PdfReader needs an actual parseable
    document, unlike the old single-attachment test which could get away with arbitrary bytes."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", "", 12)
    pdf.cell(0, 10, text)
    return bytes(pdf.output())


def _give_residency(resident: Resident, year: int, month: int, room_index: int) -> None:
    room = Room.objects.create(legacy_index=room_index, number=room_index, floor="stuen", side="mod gaden")
    Residency.objects.create(resident=resident, room=room, year=year, month=month)


@pytest.mark.django_db
def test_sends_mail_with_generated_pdf_for_the_period(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    _give_residency(resident, 2026, 8, room_index=1)
    monkeypatch.setattr(economic, "get_account", lambda n: _account(250.0))
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [_entry(1, "2026-08-01", 250.0)])

    results = alumneregning.send_alumneregninger(*AUGUST)

    assert len(results) == 1
    assert results[0].resident == resident
    assert results[0].outcome == "sent"
    assert "250,00 kr" in results[0].detail
    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == ["alumne@gahk.dk"]
    assert sent.subject == "Alumneregning"
    assert "250,00 kr" in sent.body
    assert "din gæld til kollegiet" in sent.body
    filename, content, content_type = sent.attachments[0]
    assert filename == "alumneregning-2026-08-01.pdf"
    assert content.startswith(b"%PDF-")
    assert content_type == "application/pdf"
    # The balance quoted in the mail came from a row actually mirrored into the local ledger.
    assert EconomicLedgerEntry.objects.filter(resident=resident, entry_number=1).exists()


@pytest.mark.django_db
def test_sends_mail_even_with_no_new_entries_when_a_balance_carries_forward(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    """GAHK's real alumni accounts are balance-sheet ("status") accounts — a nonzero balance with
    nothing posted this period is still real debt, so the alumne still gets a reminder mail."""
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    _give_residency(resident, 2026, 8, room_index=1)
    monkeypatch.setattr(economic, "get_account", lambda n: _account(600.0))
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [])  # nothing posted in August

    results = alumneregning.send_alumneregninger(*AUGUST)

    assert results[0].outcome == "sent"
    assert "videreført" in results[0].detail
    assert "600,00 kr" in results[0].detail
    assert len(mail.outbox) == 1
    assert "600,00 kr" in mail.outbox[0].body
    assert not EconomicLedgerEntry.objects.filter(resident=resident).exists()  # nothing to mirror


@pytest.mark.django_db
def test_skips_residents_without_a_residency_in_the_billed_period(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    _give_residency(resident, 2025, 1, room_index=1)  # lived there, but not in August 2026
    monkeypatch.setattr(economic, "get_account", lambda n: _account(100.0))
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [_entry(1, "2026-08-01", 100.0)])

    results = alumneregning.send_alumneregninger(*AUGUST)

    assert results == []
    assert mail.outbox == []


@pytest.mark.django_db
def test_skips_residents_without_a_customer_number(make_resident: Callable) -> None:
    resident = make_resident(email="no-number@gahk.dk")
    _give_residency(resident, 2026, 8, room_index=1)

    results = alumneregning.send_alumneregninger(*AUGUST)

    assert results == []
    assert mail.outbox == []


@pytest.mark.django_db
def test_sync_is_idempotent_across_sends(monkeypatch: pytest.MonkeyPatch, make_resident: Callable) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    _give_residency(resident, 2026, 8, room_index=1)
    monkeypatch.setattr(economic, "get_account", lambda n: _account(250.0))
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [_entry(1, "2026-08-01", 250.0)])

    alumneregning.send_alumneregninger(*AUGUST)
    alumneregning.send_alumneregninger(*AUGUST)  # e-conomic returns the same entry again on re-sync

    assert EconomicLedgerEntry.objects.filter(resident=resident).count() == 1  # not duplicated
    assert "250,00 kr" in mail.outbox[1].body  # balance still correct, not doubled to 500


@pytest.mark.django_db
def test_credit_balance_gets_the_other_wording(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    _give_residency(resident, 2026, 8, room_index=1)
    monkeypatch.setattr(economic, "get_account", lambda n: _account(-100.0))
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [_entry(1, "2026-08-01", -100.0)])

    alumneregning.send_alumneregninger(*AUGUST)

    assert "kollegiet skylder dig penge" in mail.outbox[0].body


@pytest.mark.django_db
def test_uses_the_regnskab_edited_message_template(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101, first_name="Tove")
    _give_residency(resident, 2026, 8, room_index=1)
    AlumneregningSettings.objects.create(
        pk=1, message_template="Kære $FORNAVN. $SALDO_LINE Konto: $KONTO_NR."
    )
    monkeypatch.setattr(economic, "get_account", lambda n: _account(100.0))
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [_entry(1, "2026-08-01", 100.0)])

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
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    _give_residency(resident, 2026, 8, room_index=1)
    AlumneregningSettings.objects.create(pk=1, message_template="Saldo: $SALDOO")
    monkeypatch.setattr(economic, "get_account", lambda n: _account(0.0))
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [_entry(1, "2026-08-01", 0.0)])

    results = alumneregning.send_alumneregninger(*AUGUST)

    assert results[0].outcome == "sent"
    assert mail.outbox[0].body == "Saldo: $SALDOO"


@pytest.mark.django_db
def test_followup_pdf_is_merged_in_as_the_first_page(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    _give_residency(resident, 2026, 8, room_index=1)
    mail_settings = AlumneregningSettings.objects.create(pk=1)
    mail_settings.followup_pdf.save("folgebrev.pdf", ContentFile(_one_page_pdf("Kaere alumner")), save=True)
    monkeypatch.setattr(economic, "get_account", lambda n: _account(0.0))
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [_entry(1, "2026-08-01", 0.0)])

    alumneregning.send_alumneregninger(*AUGUST)

    # One PDF, not a separate attachment — the følgebrev's page(s) come first.
    assert len(mail.outbox[0].attachments) == 1
    name, content, _mimetype = mail.outbox[0].attachments[0]
    assert name == "alumneregning-2026-08-01.pdf"
    merged = PdfReader(io.BytesIO(content))
    assert len(merged.pages) == 2
    assert "Kaere alumner" in merged.pages[0].extract_text()
    assert "Alumneregning" in merged.pages[1].extract_text()


@pytest.mark.django_db
def test_no_followup_pdf_means_statement_only(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    _give_residency(resident, 2026, 8, room_index=1)
    monkeypatch.setattr(economic, "get_account", lambda n: _account(0.0))
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [_entry(1, "2026-08-01", 0.0)])

    alumneregning.send_alumneregninger(*AUGUST)

    assert len(mail.outbox[0].attachments) == 1
    _name, content, _mimetype = mail.outbox[0].attachments[0]
    assert len(PdfReader(io.BytesIO(content)).pages) == 1


@pytest.mark.django_db
def test_one_failure_does_not_stop_the_rest(monkeypatch: pytest.MonkeyPatch, make_resident: Callable) -> None:
    failing = make_resident(email="fails@gahk.dk", economic_account_number=1, first_name="Fail")
    _give_residency(failing, 2026, 8, room_index=1)
    ok = make_resident(email="ok@gahk.dk", economic_account_number=2, first_name="Ok")
    _give_residency(ok, 2026, 8, room_index=2)

    def get_account(account_number: int) -> economic.EconomicAccount:
        if account_number == 1:
            raise economic.EconomicApiError("e-conomic 500")
        return _account(0.0)

    monkeypatch.setattr(economic, "get_account", get_account)
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [_entry(1, "2026-08-01", 0.0)])

    results = alumneregning.send_alumneregninger(*AUGUST)

    by_resident = {r.resident.pk: r for r in results}
    assert by_resident[failing.pk].outcome == "failed"
    assert by_resident[ok.pk].outcome == "sent"
    assert len(mail.outbox) == 1  # only the one that succeeded


@pytest.mark.django_db
def test_auth_error_aborts_the_whole_batch(monkeypatch: pytest.MonkeyPatch, make_resident: Callable) -> None:
    """An invalid/revoked e-conomic connection affects every alumne equally — stop immediately
    rather than report the same "reconnect e-conomic" failure once per remaining alumne."""
    alice = make_resident(email="alice@gahk.dk", economic_account_number=1, first_name="Alice")
    _give_residency(alice, 2026, 8, room_index=1)  # processed first (alphabetical)
    bob = make_resident(email="bob@gahk.dk", economic_account_number=2, first_name="Bob")
    _give_residency(bob, 2026, 8, room_index=2)

    def get_account(account_number: int) -> economic.EconomicAccount:
        raise economic.EconomicAuthError("token revoked")

    monkeypatch.setattr(economic, "get_account", get_account)

    with pytest.raises(economic.EconomicAuthError):
        alumneregning.send_alumneregninger(*AUGUST)

    assert mail.outbox == []  # Bob was never even attempted


@pytest.mark.django_db
def test_rate_limit_error_is_reported_and_batch_continues(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    alice = make_resident(email="alice@gahk.dk", economic_account_number=1, first_name="Alice")
    _give_residency(alice, 2026, 8, room_index=1)  # processed first
    bob = make_resident(email="bob@gahk.dk", economic_account_number=2, first_name="Bob")
    _give_residency(bob, 2026, 8, room_index=2)

    def get_account(account_number: int) -> economic.EconomicAccount:
        if account_number == 1:
            raise economic.EconomicRateLimitError("still 429 after retries")
        return _account(0.0)

    monkeypatch.setattr(economic, "get_account", get_account)
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [_entry(1, "2026-08-01", 0.0)])

    results = alumneregning.send_alumneregninger(*AUGUST)

    by_email = {r.resident.email: r for r in results}
    assert by_email["alice@gahk.dk"].outcome == "failed"
    assert "rate-limit" in by_email["alice@gahk.dk"].detail
    assert by_email["bob@gahk.dk"].outcome == "sent"
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_preview_does_not_send_but_mirrors_entries(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    _give_residency(resident, 2026, 8, room_index=1)
    monkeypatch.setattr(economic, "get_account", lambda n: _account(250.0))
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [_entry(1, "2026-08-01", 250.0)])

    rows = alumneregning.preview_alumneregninger(*AUGUST)

    assert len(rows) == 1
    assert rows[0].resident == resident
    assert rows[0].status == "ready"
    assert "250,00 kr" in rows[0].detail
    assert mail.outbox == []  # nothing was sent
    # A preview still reads real data, so it mirrors into the local ledger same as a send does.
    assert EconomicLedgerEntry.objects.filter(resident=resident, entry_number=1).exists()


@pytest.mark.django_db
def test_preview_reports_balance_even_with_no_entries(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    _give_residency(resident, 2026, 8, room_index=1)
    monkeypatch.setattr(economic, "get_account", lambda n: _account(600.0))
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [])

    rows = alumneregning.preview_alumneregninger(*AUGUST)

    assert rows[0].status == "ready"
    assert "videreført" in rows[0].detail


@pytest.mark.django_db
def test_preview_auth_error_aborts(monkeypatch: pytest.MonkeyPatch, make_resident: Callable) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    _give_residency(resident, 2026, 8, room_index=1)

    def get_account(account_number: int) -> economic.EconomicAccount:
        raise economic.EconomicAuthError("token revoked")

    monkeypatch.setattr(economic, "get_account", get_account)

    with pytest.raises(economic.EconomicAuthError):
        alumneregning.preview_alumneregninger(*AUGUST)


@pytest.mark.django_db
def test_render_preview_pdf_returns_bytes(monkeypatch: pytest.MonkeyPatch, make_resident: Callable) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    monkeypatch.setattr(economic, "get_account", lambda n: _account(250.0))
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [_entry(1, "2026-08-01", 250.0)])

    pdf = alumneregning.render_preview_pdf(resident, *AUGUST)

    assert pdf is not None
    assert pdf.startswith(b"%PDF-")


@pytest.mark.django_db
def test_render_preview_pdf_works_with_no_entries(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    resident = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    monkeypatch.setattr(economic, "get_account", lambda n: _account(600.0))
    monkeypatch.setattr(economic, "get_entries_in_range", lambda n, f, t: [])

    pdf = alumneregning.render_preview_pdf(resident, *AUGUST)

    assert pdf is not None
    assert pdf.startswith(b"%PDF-")


@pytest.mark.django_db
def test_render_preview_pdf_none_without_account_number(make_resident: Callable) -> None:
    resident = make_resident(email="alumne@gahk.dk")

    assert alumneregning.render_preview_pdf(resident, *AUGUST) is None
