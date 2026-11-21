from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from string import Template
from typing import Literal

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.db import IntegrityError, transaction
from django.db.models import F, Q, QuerySet
from django.utils import timezone

from core import mail as outbox
from core.models import OutgoingMail
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


@dataclass(frozen=True)
class AlumneregningResult:
    resident: Resident
    outcome: Outcome
    detail: str = ""
    retryable: bool = False  # a failure that may pass if tried again shortly (e-conomic's rate limit)


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


def _email(prepared: PreparedStatement, date_from: date) -> EmailMessage:
    email = EmailMessage(
        subject="Alumneregning",
        body=prepared.body,
        from_email=f"Regnskabsgruppen <{settings.ALUMNEREGNING_FROM_EMAIL}>",
        to=[prepared.resident.email],
        connection=None
        if settings.ALUMNEREGNING_SEND_MAIL
        else get_connection("django.core.mail.backends.console.EmailBackend"),
    )
    email.attach(f"alumneregning-{date_from.isoformat()}.pdf", prepared.pdf, "application/pdf")
    return email


def _sent_detail(prepared: PreparedStatement) -> str:
    detail = (
        f"{len(prepared.entries)} posteringer, saldo {_kr(prepared.balance)} kr."
        if prepared.entries
        else f"Ingen nye posteringer, saldo (videreført) {_kr(prepared.balance)} kr."
    )
    if not settings.ALUMNEREGNING_SEND_MAIL:
        detail += " Ikke mailet (test, ALUMNEREGNING_SEND_MAIL er slået fra): kun skrevet i loggen."
    return detail


# A statement mailed through the outbox counts as sent unless its mail is known not to have arrived.
# A mail cut off mid-send (UNKNOWN) may well have, so it is not sent again by a rerun on its own —
# only once regnskab says it did not arrive (mark_not_received).
_NOT_DELIVERED = (OutgoingMail.Status.FAILED, OutgoingMail.Status.EXPIRED)


def _already_sent(date_from: date, date_to: date) -> QuerySet[AlumneregningSend]:
    """Sends of the period that count as done: made and mailed, or with their mail still on its way.
    A failure — of the statement or, in the end, of its mail — is tried again by the next run."""
    return AlumneregningSend.objects.filter(date_from=date_from, date_to=date_to, outcome="sent").exclude(
        mail__status__in=_NOT_DELIVERED
    )


def _retryable(resident: Resident, date_from: date, date_to: date) -> QuerySet[AlumneregningSend]:
    return AlumneregningSend.objects.filter(resident=resident, date_from=date_from, date_to=date_to).filter(
        Q(outcome="failed") | Q(mail__status__in=_NOT_DELIVERED)
    )


def _record(resident: Resident, date_from: date, date_to: date, outcome: Outcome, detail: str) -> bool:
    """Write the alumne's outcome for the period, unless another run has sent them their statement
    meanwhile. Either way it is one statement only one of two concurrent runs can win: the update of a
    retryable row, or the insert the unique constraint guards. Returns whether this run's outcome was
    written."""
    retryable = _retryable(resident, date_from, date_to)
    replaced = list(retryable.exclude(mail=None).values_list("mail_id", flat=True))
    if retryable.update(outcome=outcome, detail=detail, mail=None, sent_at=timezone.now()):
        # The mail that did not arrive is replaced by this run's: emptied, so it cannot also be sent
        # again by hand (core.mail.send_again) and reach the alumne beside the new one.
        OutgoingMail.objects.filter(pk__in=replaced).update(payload={})
        return True
    try:
        with transaction.atomic():
            AlumneregningSend.objects.create(
                resident=resident, date_from=date_from, date_to=date_to, outcome=outcome, detail=detail
            )
    except IntegrityError:  # sent by another run
        return False
    return True


class _NotMailed(Exception):
    pass


def _queue_statement(
    prepared: PreparedStatement, date_from: date, date_to: date
) -> AlumneregningResult | None:
    """Record the alumne's statement as sent and queue its mail, in one transaction: a mail is only
    queued with its row, and a row only written with its mail. None if another run sent it first."""
    resident = prepared.resident
    detail = _sent_detail(prepared)
    email = _email(prepared, date_from)
    with transaction.atomic():
        if not _record(resident, date_from, date_to, "sent", detail):
            return None
        try:
            with outbox.kind(OutgoingMail.Kind.BULK):
                email.send()  # through core.mail's outbox: stored here, sent after commit
        except Exception as exc:  # only a backend that sends there and then (dev's console) gets here
            raise _NotMailed(str(exc)) from exc  # rolls the "sent" row back with it
        mail = getattr(email, "outgoing_mail", None)
        if mail is not None:
            AlumneregningSend.objects.filter(resident=resident, date_from=date_from, date_to=date_to).update(
                mail=mail
            )
    return AlumneregningResult(resident, "sent", detail)


def mark_not_received(send: AlumneregningSend, by: str) -> bool:
    """Regnskab's answer to a statement whose mail was cut off mid-send (UNKNOWN): it did not arrive.
    The mail then counts as failed, so the next run of the period sends the statement again. Returns
    whether there was such a mail."""
    if send.mail_id is None:
        return False
    return bool(
        OutgoingMail.objects.filter(pk=send.mail_id, status=OutgoingMail.Status.UNKNOWN).update(
            status=OutgoingMail.Status.FAILED, error=f"Kom ikke frem, ifølge {by}."
        )
    )


def send_alumneregninger(date_from: date, date_to: date) -> list[AlumneregningResult]:
    """Make every billable resident's statement and queue it to them, skipping anyone already sent
    theirs for this exact period — so a retried/duplicate send (the task re-running, the button pressed
    twice) never mails someone twice. Best-effort: one failing doesn't stop the rest. The mails go out
    from core.mail's outbox within the mail server's rate limit; the status page follows them there."""
    mail_settings = AlumneregningSettings.get_solo()
    results: list[AlumneregningResult] = []
    for resident in residents_billable_for(date_from, date_to):
        if _already_sent(date_from, date_to).filter(resident=resident).exists():
            continue
        account_number = resident.economic_account_number
        assert account_number is not None  # noqa: S101  # nosec B101 — residents_billable_for filters on it
        failure: AlumneregningResult | None = None
        try:
            prepared = _prepare_one(resident, account_number, date_from, date_to, mail_settings, persist=True)
            result = _queue_statement(prepared, date_from, date_to)
        except economic.EconomicAuthError as exc:
            logger.error("alumneregning: e-conomic access invalid/revoked: %s", exc)
            raise  # affects every remaining alumne equally — stop the batch, don't repeat the error
        except _NotMailed as exc:
            logger.warning("alumneregning: failed to mail resident %s: %s", resident.pk, exc)
            failure = AlumneregningResult(resident, "failed", f"Kunne ikke sende mail: {exc}")
        except AccountNameMismatch as exc:
            logger.warning("alumneregning: account name mismatch for resident %s: %s", resident.pk, exc)
            failure = AlumneregningResult(resident, "failed", str(exc))
        except economic.EconomicRateLimitError as exc:
            logger.warning("alumneregning: e-conomic rate limit for resident %s: %s", resident.pk, exc)
            failure = AlumneregningResult(
                resident, "failed", "E-conomic rate-limit ramt — prøves igen automatisk.", retryable=True
            )
        except economic.EconomicApiError as exc:
            logger.warning("alumneregning failed for resident %s: %s", resident.pk, exc)
            detail = (
                f"Kontonr. {account_number} findes ikke i e-conomic." if exc.status_code == 404 else str(exc)
            )
            failure = AlumneregningResult(
                resident, "failed", detail, retryable=exc.status_code is None or exc.status_code >= 500
            )
        except Exception as exc:  # anything unforeseen for one alumne must not strand everyone after them
            logger.exception("alumneregning: unexpected error for resident %s", resident.pk)
            failure = AlumneregningResult(resident, "failed", f"Uventet fejl: {exc}")
        if failure is not None:
            if _record(resident, date_from, date_to, "failed", failure.detail):
                results.append(failure)
        elif result is not None:
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
