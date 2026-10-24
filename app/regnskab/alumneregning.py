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

from residents.models import Residency, Resident

from . import economic, ledger, statement_pdf
from .models import AlumneregningSend, AlumneregningSettings
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
    entries: list[economic.LedgerEntry]  # this period's postings, for display — may be empty
    balance: Decimal  # e-conomic's posted balance plus every currently outstanding draft
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


def residents_billable_for(date_from: date, date_to: date) -> QuerySet[Resident]:
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
    *,
    persist: bool,
) -> PreparedStatement:
    """Fetches the balance + this period's postings and renders the PDF. Only mirrors into the local
    ledger when persist=True — a preview or a PDF view is a read, and should stay one (regnskab.views
    pass persist=True only from the actual send)."""
    # get_account first: the cheapest single call, and the one most likely to surface an auth problem
    # — callers (send_alumneregninger) rely on that to fail fast before any of the heavier calls below.
    balance = Decimal(str(economic.get_account(account_number).balance))
    posted = economic.get_posted_entries_in_range(account_number, date_from, date_to)
    # Unscoped: a draft dated before date_from is still real, uncounted debt until it is posted or
    # withdrawn, so the balance (and the local draft mirror) must see every current draft, not just
    # ones dated in the billed period. Confirmed against the real account 4207: get_account().balance
    # was 132.81 while the true current balance, drafts included, was 214.01.
    drafts = economic.get_draft_entries_for_account(account_number)
    balance += sum((Decimal(str(d.amount)) for d in drafts), Decimal(0))
    entries = posted + [d for d in drafts if date_from <= date.fromisoformat(d.date) <= date_to]
    if persist:
        ledger.sync_entries(resident, posted + drafts)

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
    prepared = _prepare_one(resident, account_number, date_from, date_to, mail_settings, persist=True)

    email = EmailMessage(
        subject="Alumneregning",
        body=prepared.body,
        from_email=f"Regnskabsgruppen <{settings.ALUMNEREGNING_FROM_EMAIL}>",
        to=[resident.email],
    )
    email.attach(f"alumneregning-{date_from.isoformat()}.pdf", prepared.pdf, "application/pdf")
    try:
        email.send()
    except Exception as exc:  # SMTP errors, a bad address, etc. must not abort the batch
        logger.warning("alumneregning: failed to mail resident %s: %s", resident.pk, exc)
        return AlumneregningResult(resident, "failed", f"Kunne ikke sende mail: {exc}")
    detail = (
        f"{len(prepared.entries)} posteringer, saldo {_kr(prepared.balance)} kr."
        if prepared.entries
        else f"Ingen nye posteringer, saldo (videreført) {_kr(prepared.balance)} kr."
    )
    return AlumneregningResult(resident, "sent", detail)


def send_alumneregninger(date_from: date, date_to: date) -> list[AlumneregningResult]:
    """Email every billable resident their statement, skipping anyone already billed for this exact
    period (AlumneregningSend) — so a retried/duplicate send (the task re-running, the button pressed
    twice) never bills someone twice. Best-effort: one failing doesn't stop the rest."""
    mail_settings = AlumneregningSettings.get_solo()
    already_sent = set(
        AlumneregningSend.objects.filter(date_from=date_from, date_to=date_to, outcome="sent").values_list(
            "resident_id", flat=True
        )
    )
    results: list[AlumneregningResult] = []
    for resident in residents_billable_for(date_from, date_to):
        if resident.pk in already_sent:
            continue
        account_number = resident.economic_account_number
        assert account_number is not None  # noqa: S101  # nosec B101 — residents_billable_for filters on it
        try:
            result = _send_one(resident, account_number, date_from, date_to, mail_settings)
        except economic.EconomicAuthError as exc:
            logger.error("alumneregning: e-conomic access invalid/revoked: %s", exc)
            raise  # affects every remaining alumne equally — stop the batch, don't repeat the error
        except economic.EconomicRateLimitError as exc:
            logger.warning("alumneregning: e-conomic rate limit for resident %s: %s", resident.pk, exc)
            result = AlumneregningResult(
                resident, "failed", "E-conomic rate-limit ramt — prøv at sende igen om lidt."
            )
        except economic.EconomicApiError as exc:
            logger.warning("alumneregning failed for resident %s: %s", resident.pk, exc)
            result = AlumneregningResult(resident, "failed", str(exc))
        AlumneregningSend.objects.update_or_create(
            resident=resident,
            date_from=date_from,
            date_to=date_to,
            defaults={"outcome": result.outcome, "detail": result.detail},
        )
        results.append(result)
    return results


def preview_alumneregninger(date_from: date, date_to: date) -> list[PreviewRow]:
    """Who would be billed for [date_from, date_to] and what their statement would say — sends nothing."""
    mail_settings = AlumneregningSettings.get_solo()
    rows: list[PreviewRow] = []
    for resident in residents_billable_for(date_from, date_to):
        account_number = resident.economic_account_number
        assert account_number is not None  # noqa: S101  # nosec B101 — residents_billable_for filters on it
        try:
            prepared = _prepare_one(
                resident, account_number, date_from, date_to, mail_settings, persist=False
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
    """The statement PDF for one resident/period, or None if they have no account number."""
    if resident.economic_account_number is None:
        return None
    mail_settings = AlumneregningSettings.get_solo()
    prepared = _prepare_one(
        resident, resident.economic_account_number, date_from, date_to, mail_settings, persist=False
    )
    return prepared.pdf
