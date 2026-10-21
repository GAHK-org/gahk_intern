"""Alumneregninger: billing mails to former residents for a chosen period (F-010 regnskab role).

Replaces the old manual flow (alumneregning/send_mail.py in a separate repo): download a combined PDF
of every debtor's account card from e-conomic, OCR-match pages to names, split, email. GAHK does not
track alumni as e-conomic Customers — each alumne has their own chart-of-accounts GL account instead
(residents.forms.EconomicAccountNumberForm sets Resident.economic_account_number), so their statement
is read directly and deterministically via residents.economic instead of matched by OCR.

What gets sent: a PDF statement generated locally (residents.statement_pdf — NOT fetched from
e-conomic; GAHK's account-based setup has nothing like a rendered invoice to fetch, see
economic.get_account's docstring) showing the period's postings (economic.get_entries_in_range) plus
the regnskab-edited følgebrev PDF (AlumneregningSettings) if one is set. The balance quoted in the
mail is e-conomic's own current balance for the account (economic.get_account) — NOT derived from the
period's postings: GAHK's real alumni accounts are type "status" (Danish e-conomic UI: "Balance") and
carry a continuous running balance, so an alumne with nothing posted in the chosen period can still
have (and should still be told about) an outstanding balance carried forward from before. Everyone
billable therefore gets mailed, whether or not anything new was posted in the period.

Every fetched entry is still mirrored into the local immutable ledger (residents.ledger) regardless —
including during a preview (preview_alumneregninger/render_preview_pdf): that is GAHK's own permanent
audit record of what was read from e-conomic, and a preview reads real data just as a send does, so
there is no reason for it to skip the mirror.

Who gets mailed: residents with an economic_account_number set AND a Residency falling within
[date_from, date_to] — i.e. people who actually lived at GAHK during the period being billed, not
every resident who has ever been given an account number.

The mail body is AlumneregningSettings.message_template (edited in the UI by regnskab, defaults to
models.DEFAULT_ALUMNEREGNING_MESSAGE), a string.Template filled in with $FORNAVN, $SALDO_LINE (the
balance sentence, sign-aware — computed here, not editable, since it is derived data rather than
wording), $REG_NR and $KONTO_NR. safe_substitute(), not substitute(): a typo'd placeholder in a
regnskab-edited template should render literally, not break every send in the batch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from string import Template
from typing import Literal

from django.conf import settings
from django.core.mail import EmailMessage
from django.db.models import F, QuerySet

from . import economic, ledger, statement_pdf
from .models import AlumneregningSettings, Residency, Resident

logger = logging.getLogger(__name__)

Outcome = Literal["sent", "failed"]
PreviewStatus = Literal["ready", "failed"]


@dataclass(frozen=True)
class AlumneregningResult:
    resident: Resident
    outcome: Outcome
    detail: str = ""


@dataclass(frozen=True)
class PreviewRow:
    resident: Resident
    status: PreviewStatus
    detail: str = ""


@dataclass(frozen=True)
class PreparedStatement:
    """Everything a send needs for one alumne except actually emailing it — shared by
    send_alumneregninger (which mails it) and the regnskab preview (which just shows it)."""

    resident: Resident
    account_number: int
    entries: list[economic.LedgerEntry]  # this period's postings only — may be empty
    balance: Decimal  # e-conomic's own current balance for the account, not derived from `entries`
    pdf: bytes
    body: str


def _kr(amount: Decimal) -> str:
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _body(resident: Resident, balance: Decimal, message_template: str) -> str:
    saldo_line = (
        f"Din saldo er nu {_kr(balance)} kr. — det er din gæld til kollegiet."
        if balance >= 0
        else f"Din saldo er nu {_kr(balance)} kr. — kollegiet skylder dig penge."
    )
    return Template(message_template).safe_substitute(
        FORNAVN=resident.first_name,
        SALDO_LINE=saldo_line,
        REG_NR=settings.ALUMNEREGNING_BANK_REG,
        KONTO_NR=settings.ALUMNEREGNING_BANK_ACCOUNT,
    )


def _residents_billable_for(date_from: date, date_to: date) -> QuerySet[Resident]:
    """Residents eligible for this send: an e-conomic account number set, and a Residency row whose
    (year, month) falls within [date_from, date_to] — i.e. they actually lived at GAHK at some point
    during the period being billed."""
    lower = date_from.year * 12 + date_from.month
    upper = date_to.year * 12 + date_to.month
    resident_ids = (
        Residency.objects.annotate(month_number=F("year") * 12 + F("month"))
        .filter(month_number__gte=lower, month_number__lte=upper)
        .values_list("resident_id", flat=True)
        .distinct()
    )
    return Resident.objects.filter(pk__in=resident_ids, economic_account_number__isnull=False).order_by(
        "first_name", "last_name"
    )


def _prepare_one(
    resident: Resident,
    account_number: int,
    date_from: date,
    date_to: date,
    mail_settings: AlumneregningSettings,
) -> PreparedStatement:
    """Fetches the account's current balance and this period's postings, mirrors the postings into
    the local ledger, and renders the statement PDF + mail body. Always produces something to send —
    see the module docstring on why a period with nothing posted still gets mailed."""
    balance = Decimal(str(economic.get_account(account_number).balance))
    entries = economic.get_entries_in_range(account_number, date_from, date_to)
    if entries:
        ledger.sync_entries(resident, entries)

    pdf = statement_pdf.render(resident, account_number, entries, balance, date_from, date_to)
    body = _body(resident, balance, mail_settings.message_template)
    return PreparedStatement(resident, account_number, entries, balance, pdf, body)


def _send_one(
    resident: Resident,
    account_number: int,
    date_from: date,
    date_to: date,
    mail_settings: AlumneregningSettings,
) -> AlumneregningResult:
    prepared = _prepare_one(resident, account_number, date_from, date_to, mail_settings)

    email = EmailMessage(
        subject="Alumneregning",
        body=prepared.body,
        from_email=f"Regnskabsgruppen <{settings.ALUMNEREGNING_FROM_EMAIL}>",
        to=[resident.email],
    )
    email.attach(f"alumneregning-{date_from.isoformat()}.pdf", prepared.pdf, "application/pdf")
    if mail_settings.followup_pdf:
        with mail_settings.followup_pdf.open("rb") as followup:
            email.attach(
                mail_settings.followup_pdf.name.rsplit("/", 1)[-1], followup.read(), "application/pdf"
            )
    email.send()
    detail = (
        f"{len(prepared.entries)} posteringer, saldo {_kr(prepared.balance)} kr."
        if prepared.entries
        else f"Ingen nye posteringer, saldo (videreført) {_kr(prepared.balance)} kr."
    )
    return AlumneregningResult(resident, "sent", detail)


def send_alumneregninger(date_from: date, date_to: date) -> list[AlumneregningResult]:
    """Email every billable resident (see _residents_billable_for) their statement for
    [date_from, date_to]. Best-effort per resident: one failing (e-conomic error, bad account number)
    does not stop the rest — reported in the results instead, same as the old script's per-row error
    printing."""
    mail_settings = AlumneregningSettings.get_solo()
    results: list[AlumneregningResult] = []
    for resident in _residents_billable_for(date_from, date_to):
        try:
            results.append(
                _send_one(resident, resident.economic_account_number, date_from, date_to, mail_settings)
            )
        except economic.EconomicAuthError as exc:
            # No silent refresh is possible (see EconomicAuthError's docstring) — stop the whole
            # batch rather than report the same "reconnect e-conomic" failure once per remaining
            # alumne, and surface it as an exception so the view renders a clear error, not a
            # results table full of identical rows.
            logger.error("alumneregning: e-conomic access invalid/revoked: %s", exc)
            raise
        except economic.EconomicRateLimitError as exc:
            logger.warning("alumneregning: e-conomic rate limit for resident %s: %s", resident.pk, exc)
            results.append(
                AlumneregningResult(
                    resident, "failed", "E-conomic rate-limit ramt — prøv at sende igen om lidt."
                )
            )
        except economic.EconomicApiError as exc:
            logger.warning("alumneregning failed for resident %s: %s", resident.pk, exc)
            results.append(AlumneregningResult(resident, "failed", str(exc)))
    return results


def preview_alumneregninger(date_from: date, date_to: date) -> list[PreviewRow]:
    """Who WOULD be billed for [date_from, date_to], and what their statement would say — without
    sending anything. Same error handling as send_alumneregninger: an auth error aborts immediately
    (raised, not reported per-row — it affects every alumne equally), a rate limit or other API error
    is reported per-resident so the rest of the preview still renders."""
    mail_settings = AlumneregningSettings.get_solo()
    rows: list[PreviewRow] = []
    for resident in _residents_billable_for(date_from, date_to):
        try:
            prepared = _prepare_one(
                resident, resident.economic_account_number, date_from, date_to, mail_settings
            )
        except economic.EconomicAuthError as exc:
            logger.error("alumneregning preview: e-conomic access invalid/revoked: %s", exc)
            raise
        except economic.EconomicRateLimitError:
            rows.append(PreviewRow(resident, "failed", "E-conomic rate-limit ramt — prøv igen om lidt."))
            continue
        except economic.EconomicApiError as exc:
            rows.append(PreviewRow(resident, "failed", str(exc)))
            continue

        detail = (
            f"{len(prepared.entries)} posteringer, saldo {_kr(prepared.balance)} kr."
            if prepared.entries
            else f"Ingen nye posteringer, saldo (videreført) {_kr(prepared.balance)} kr."
        )
        rows.append(PreviewRow(resident, "ready", detail))
    return rows


def render_preview_pdf(resident: Resident, date_from: date, date_to: date) -> bytes | None:
    """The statement PDF for one resident/period — what the regnskab preview's "Se PDF" link shows.
    None only if the resident has no account number set."""
    if resident.economic_account_number is None:
        return None
    mail_settings = AlumneregningSettings.get_solo()
    return _prepare_one(resident, resident.economic_account_number, date_from, date_to, mail_settings).pdf
