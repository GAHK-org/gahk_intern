"""Alumneregninger: monthly billing mails to former residents (F-010 regnskab role).

Replaces the old manual flow (alumneregning/send_mail.py in a separate repo): download a combined
PDF of every debtor's account card from e-conomic, OCR-match pages to names, split, email. Here each
alumne's e-conomic customerNumber is already on their Resident (residents.forms.EconomicCustomerNumberForm),
so their statement is fetched directly and deterministically via residents.economic instead.

What gets sent: the alumne's *booked invoice falling within a caller-chosen date range* (one PDF,
e-conomic's own rendering) plus the regnskab-edited følgebrev PDF (AlumneregningSettings, if one is
set) as attachments, and their balance in the mail body — but the balance comes from GAHK's own local
ledger mirror (residents.ledger), not a live e-conomic customer.balance call. Each send first syncs
that resident's entries (residents.ledger.sync_entries), so the figure quoted in the mail is also the
one now sitting in gahk-intern's own permanent record, not just e-conomic's.

The mail body itself is AlumneregningSettings.message_template (edited in the UI by regnskab,
defaults to models.DEFAULT_ALUMNEREGNING_MESSAGE), a string.Template filled in with $FORNAVN,
$SALDO_LINE (the balance sentence, sign-aware — computed here, not editable, since it is derived
data rather than wording), $REG_NR and $KONTO_NR. safe_substitute(), not substitute(): a typo'd
placeholder in a regnskab-edited template should render literally, not break every send in the batch.

The date range (e.g. "August 2026", or any custom from/to) picks WHICH invoice gets attached — the
balance itself is never period-scoped, it is always the running total, same as e-conomic's own
customer.balance. One invoice attached per send, not every open entry in the range like the old
combined PDF — e-conomic books one invoice per billing run, so a resident billed once in the chosen
range gets that run's bill. If more than one invoice falls in range, the latest wins. If that
assumption turns out wrong in practice (multiple invoices per month, or postings that never become an
"invoice"), residents.economic.get_booked_invoices already returns the full list to build a richer
statement from.
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

from . import economic, ledger
from .models import AlumneregningSettings, Resident

logger = logging.getLogger(__name__)

Outcome = Literal["sent", "no_invoices", "failed"]


@dataclass(frozen=True)
class AlumneregningResult:
    resident: Resident
    outcome: Outcome
    detail: str = ""


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


def _send_one(
    resident: Resident,
    customer_number: int,
    date_from: date,
    date_to: date,
    mail_settings: AlumneregningSettings,
) -> AlumneregningResult:
    ledger.sync_entries(resident, customer_number)
    balance = ledger.balance(resident)

    invoices = [
        inv
        for inv in economic.get_booked_invoices(customer_number)
        if date_from <= date.fromisoformat(inv.date) <= date_to
    ]
    if not invoices:
        return AlumneregningResult(
            resident, "no_invoices", f"Ingen bogført faktura mellem {date_from} og {date_to}."
        )

    latest = max(invoices, key=lambda inv: inv.date)
    pdf = economic.get_invoice_pdf(latest.pdf_url)

    email = EmailMessage(
        subject="Alumneregning",
        body=_body(resident, balance, mail_settings.message_template),
        from_email=f"Regnskabsgruppen <{settings.ALUMNEREGNING_FROM_EMAIL}>",
        to=[resident.email],
    )
    email.attach(f"alumneregning-{latest.date}.pdf", pdf, "application/pdf")
    if mail_settings.followup_pdf:
        with mail_settings.followup_pdf.open("rb") as followup:
            email.attach(
                mail_settings.followup_pdf.name.rsplit("/", 1)[-1], followup.read(), "application/pdf"
            )
    email.send()
    return AlumneregningResult(resident, "sent", f"Faktura {latest.booked_invoice_number} ({latest.date}).")


def send_alumneregninger(date_from: date, date_to: date) -> list[AlumneregningResult]:
    """Email every resident with an e-conomic customerNumber the booked invoice falling in
    [date_from, date_to] (inclusive). Best-effort per resident: one failing (e-conomic error, bad
    data) does not stop the rest — reported in the results instead, same as the old script's per-row
    error printing."""
    mail_settings = AlumneregningSettings.get_solo()
    results: list[AlumneregningResult] = []
    residents = Resident.objects.filter(economic_customer_number__isnull=False).order_by(
        "first_name", "last_name"
    )
    for resident in residents:
        try:
            results.append(
                _send_one(resident, resident.economic_customer_number, date_from, date_to, mail_settings)
            )
        except economic.EconomicApiError as exc:
            logger.warning("alumneregning failed for resident %s: %s", resident.pk, exc)
            results.append(AlumneregningResult(resident, "failed", str(exc)))
    return results
