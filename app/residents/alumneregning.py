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
from .statement_pdf import _kr

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
    """Everything a send needs for one alumne except actually emailing it."""

    resident: Resident
    account_number: int
    entries: list[economic.LedgerEntry]  # this period's postings only — may be empty
    balance: Decimal  # e-conomic's own current balance for the account, not derived from `entries`
    pdf: bytes
    body: str


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
    """Residents with an e-conomic account number and a Residency within [date_from, date_to]."""
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
    """Fetches the balance + this period's postings, mirrors them into the ledger, renders the PDF."""
    balance = Decimal(str(economic.get_account(account_number).balance))
    entries = economic.get_entries_in_range(account_number, date_from, date_to)
    if entries:
        ledger.sync_entries(resident, entries)

    pdf = statement_pdf.render(resident, account_number, entries, balance, date_from, date_to)
    if mail_settings.followup_pdf:
        with mail_settings.followup_pdf.open("rb") as followup:
            pdf = statement_pdf.prepend_followup(followup.read(), pdf)
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
    email.send()
    detail = (
        f"{len(prepared.entries)} posteringer, saldo {_kr(prepared.balance)} kr."
        if prepared.entries
        else f"Ingen nye posteringer, saldo (videreført) {_kr(prepared.balance)} kr."
    )
    return AlumneregningResult(resident, "sent", detail)


def send_alumneregninger(date_from: date, date_to: date) -> list[AlumneregningResult]:
    """Email every billable resident their statement. Best-effort: one failing doesn't stop the rest."""
    mail_settings = AlumneregningSettings.get_solo()
    results: list[AlumneregningResult] = []
    for resident in _residents_billable_for(date_from, date_to):
        account_number = resident.economic_account_number
        assert account_number is not None  # noqa: S101  # nosec B101 — _residents_billable_for filters on it
        try:
            results.append(_send_one(resident, account_number, date_from, date_to, mail_settings))
        except economic.EconomicAuthError as exc:
            logger.error("alumneregning: e-conomic access invalid/revoked: %s", exc)
            raise  # affects every remaining alumne equally — stop the batch, don't repeat the error
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
    """Who would be billed for [date_from, date_to] and what their statement would say — sends nothing."""
    mail_settings = AlumneregningSettings.get_solo()
    rows: list[PreviewRow] = []
    for resident in _residents_billable_for(date_from, date_to):
        account_number = resident.economic_account_number
        assert account_number is not None  # noqa: S101  # nosec B101 — _residents_billable_for filters on it
        try:
            prepared = _prepare_one(resident, account_number, date_from, date_to, mail_settings)
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
    """The statement PDF for one resident/period, or None if they have no account number."""
    if resident.economic_account_number is None:
        return None
    mail_settings = AlumneregningSettings.get_solo()
    return _prepare_one(resident, resident.economic_account_number, date_from, date_to, mail_settings).pdf
