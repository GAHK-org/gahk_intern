from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from string import Template
from typing import Literal

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.db import IntegrityError, transaction
from django.db.models import F, QuerySet
from django.utils import timezone

from residents.models import Residency, Resident

from . import economic, ledger, statement_pdf
from .models import AlumneregningSend, AlumneregningSettings
from .statement_pdf import _kr

logger = logging.getLogger(__name__)

Outcome = Literal["sent", "failed"]
PreviewStatus = Literal["ready", "failed"]


class AccountNameMismatch(Exception):
    """The e-conomic account is named for someone other than the resident it is set on — most likely a
    mistyped kontonr., which would otherwise mail one alumne another's balance under their own name."""


class MailThrottled(Exception):
    """The mail server refuses everyone for now, as one.com does after ~25 mails in 5 minutes."""


# 421: the server is closing the connection. 451: one.com's "4.7.1 [R2] Too many mails" (and greylisting,
# which a later run gets past too). Any other 4xx — 450 mailbox busy, 452 mailbox full — is about the one
# recipient, and fails just them: treating it as a throttle would stop every run at the same alumne.
THROTTLE_CODES = frozenset({421, 451})


def _is_throttled(exc: Exception) -> bool:
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return bool(exc.recipients) and all(code in THROTTLE_CODES for code, _msg in exc.recipients.values())
    return isinstance(exc, smtplib.SMTPResponseException) and exc.smtp_code in THROTTLE_CODES


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


def _words(name: str) -> set[str]:
    return set(re.findall(r"\w+", name.casefold()))


def account_name_matches(resident: Resident, account_name: str) -> bool:
    """Every word of the resident's first and last name is in the e-conomic account's name. Lenient on
    purpose — middle names, initials and word order may differ — but a different person's account fails."""
    wanted = _words(f"{resident.first_name} {resident.last_name}")
    return bool(wanted) and wanted <= _words(account_name)


def mismatch_message(resident: Resident, account_number: int, account_name: str) -> str:
    return (
        f'Kontonr. {account_number} hedder "{account_name}" i e-conomic, ikke {resident.full_name}. '
        "Tjek nummeret, eller ret navnet i e-conomic."
    )


def residents_in_period(date_from: date, date_to: date) -> QuerySet[Resident]:
    """Residents with a Residency within [date_from, date_to], with or without an account number."""
    lower = date_from.year * 12 + date_from.month
    upper = date_to.year * 12 + date_to.month
    resident_ids = (
        Residency.objects.annotate(month_number=F("year") * 12 + F("month"))
        .filter(month_number__gte=lower, month_number__lte=upper)
        .values_list("resident_id", flat=True)
        .distinct()
    )
    return Resident.objects.filter(pk__in=resident_ids).order_by("first_name", "last_name")


def residents_billable_for(date_from: date, date_to: date) -> QuerySet[Resident]:
    """Residents with an e-conomic account number and a Residency within [date_from, date_to]."""
    return residents_in_period(date_from, date_to).filter(economic_account_number__isnull=False)


def residents_missing_account_number(date_from: date, date_to: date) -> QuerySet[Resident]:
    """Residents in the period with no e-conomic account number, who get no alumneregning. regnskab.views
    lists and warns about them, so nobody who lived here goes unbilled without anyone noticing."""
    return residents_in_period(date_from, date_to).filter(economic_account_number__isnull=True)


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
    account = economic.get_account(account_number)
    if not account_name_matches(resident, account.name):
        raise AccountNameMismatch(mismatch_message(resident, account_number, account.name))
    balance = Decimal(str(account.balance))
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
        connection=None
        if settings.ALUMNEREGNING_SEND_MAIL
        else get_connection("django.core.mail.backends.console.EmailBackend"),
    )
    email.attach(f"alumneregning-{date_from.isoformat()}.pdf", prepared.pdf, "application/pdf")
    try:
        email.send()
    except Exception as exc:  # SMTP errors, a bad address, etc. must not abort the batch
        if _is_throttled(exc):
            raise MailThrottled(str(exc)) from exc
        logger.warning("alumneregning: failed to mail resident %s: %s", resident.pk, exc)
        return AlumneregningResult(resident, "failed", f"Kunne ikke sende mail: {exc}")
    detail = (
        f"{len(prepared.entries)} posteringer, saldo {_kr(prepared.balance)} kr."
        if prepared.entries
        else f"Ingen nye posteringer, saldo (videreført) {_kr(prepared.balance)} kr."
    )
    if not settings.ALUMNEREGNING_SEND_MAIL:
        detail += " Ikke mailet (test, ALUMNEREGNING_SEND_MAIL er slået fra): kun skrevet i loggen."
    return AlumneregningResult(resident, "sent", detail)


def _claim(resident: Resident, date_from: date, date_to: date) -> bool | None:
    """Mark the alumne "sending" for this period, unless another run has them or they are already sent.
    None: not ours. Otherwise whether the row is new — what _release needs to put it back. Both ways
    are a single statement that only one of two concurrent runs can win."""
    period = {"resident": resident, "date_from": date_from, "date_to": date_to}
    if AlumneregningSend.objects.filter(**period, outcome="failed").update(outcome="sending"):
        return False
    try:
        with transaction.atomic():
            AlumneregningSend.objects.create(**period, outcome="sending")
    except IntegrityError:  # a row already there is "sent" or "sending"
        return None
    return True


def _release(resident: Resident, date_from: date, date_to: date, *, created: bool) -> None:
    """Undo _claim for an alumne this run is leaving to a later one."""
    rows = AlumneregningSend.objects.filter(resident=resident, date_from=date_from, date_to=date_to)
    if created:
        rows.delete()
    else:
        rows.update(outcome="failed")  # detail was left as the earlier failure's


def send_alumneregninger(date_from: date, date_to: date) -> list[AlumneregningResult]:
    """Email every billable resident their statement, skipping anyone already billed for this exact
    period, or being billed by another run of it (_claim) — so a retried/duplicate send (the task
    re-running, the button pressed while a retry is pending) never bills someone twice. Best-effort:
    one failing doesn't stop the rest. Stops with MailThrottled, leaving the rest waiting rather than
    failed, for regnskab.tasks to run again."""
    mail_settings = AlumneregningSettings.get_solo()
    results: list[AlumneregningResult] = []
    for resident in residents_billable_for(date_from, date_to):
        created = _claim(resident, date_from, date_to)
        if created is None:
            continue
        account_number = resident.economic_account_number
        assert account_number is not None  # noqa: S101  # nosec B101 — residents_billable_for filters on it
        try:
            result = _send_one(resident, account_number, date_from, date_to, mail_settings)
        except economic.EconomicAuthError as exc:
            logger.error("alumneregning: e-conomic access invalid/revoked: %s", exc)
            _release(resident, date_from, date_to, created=created)
            raise  # affects every remaining alumne equally — stop the batch, don't repeat the error
        except MailThrottled:
            _release(resident, date_from, date_to, created=created)
            raise  # before the catch-all below: the rest wait for the retry rather than fail
        except AccountNameMismatch as exc:
            logger.warning("alumneregning: account name mismatch for resident %s: %s", resident.pk, exc)
            result = AlumneregningResult(resident, "failed", str(exc))
        except economic.EconomicRateLimitError as exc:
            logger.warning("alumneregning: e-conomic rate limit for resident %s: %s", resident.pk, exc)
            result = AlumneregningResult(
                resident, "failed", "E-conomic rate-limit ramt — prøv at sende igen om lidt."
            )
        except economic.EconomicApiError as exc:
            logger.warning("alumneregning failed for resident %s: %s", resident.pk, exc)
            detail = (
                f"Kontonr. {account_number} findes ikke i e-conomic." if exc.status_code == 404 else str(exc)
            )
            result = AlumneregningResult(resident, "failed", detail)
        except Exception as exc:  # anything unforeseen for one alumne must not strand everyone after them
            logger.exception("alumneregning: unexpected error for resident %s", resident.pk)
            result = AlumneregningResult(resident, "failed", f"Uventet fejl: {exc}")
        AlumneregningSend.objects.filter(resident=resident, date_from=date_from, date_to=date_to).update(
            outcome=result.outcome, detail=result.detail, sent_at=timezone.now()
        )
        results.append(result)
    return results


def fail_the_rest(date_from: date, date_to: date, detail: str) -> int:
    """Give up on everyone billable for the period who has no outcome yet: they are marked failed, so
    the status page stops saying they will be sent. Returns how many."""
    handled = AlumneregningSend.objects.filter(date_from=date_from, date_to=date_to).values("resident_id")
    waiting = residents_billable_for(date_from, date_to).exclude(pk__in=handled)
    rows = [
        AlumneregningSend(resident=r, date_from=date_from, date_to=date_to, outcome="failed", detail=detail)
        for r in waiting
    ]
    AlumneregningSend.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


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
        except AccountNameMismatch as exc:
            rows.append(PreviewRow(resident, "failed", str(exc)))
            continue
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
    """The statement PDF for one resident/period, or None if they have no account number. Raises
    AccountNameMismatch if the account is named for someone else."""
    if resident.economic_account_number is None:
        return None
    mail_settings = AlumneregningSettings.get_solo()
    prepared = _prepare_one(
        resident, resident.economic_account_number, date_from, date_to, mail_settings, persist=False
    )
    return prepared.pdf
