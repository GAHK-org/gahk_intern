"""Alumneregninger: monthly billing mails to former residents (F-010 regnskab role).

Replaces the old manual flow (alumneregning/send_mail.py in a separate repo): download a combined
PDF of every debtor's account card from e-conomic, OCR-match pages to names, split, email. Here each
alumne's e-conomic customerNumber is already on their Resident (residents.forms.EconomicCustomerNumberForm),
so their statement is fetched directly and deterministically via residents.economic instead.

What gets sent: the alumne's most recent *booked invoice* from e-conomic (one PDF, e-conomic's own
rendering) as the attachment, and their balance in the mail body — but the balance comes from GAHK's
own local ledger mirror (residents.ledger), not a live e-conomic customer.balance call. Each send
first syncs that resident's entries (residents.ledger.sync_entries), so the figure quoted in the mail
is also the one now sitting in gahk-intern's own permanent record, not just e-conomic's.

One invoice attached, not every open entry since the last accounting meeting like the old combined
PDF — e-conomic books one invoice per billing run, so "most recent" is that run's bill. If that
assumption turns out wrong in practice (multiple invoices per month, or postings that never become an
"invoice"), residents.economic.get_booked_invoices already returns the full list to build a richer
statement from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from django.conf import settings
from django.core.mail import EmailMessage

from . import economic, ledger
from .models import Resident

logger = logging.getLogger(__name__)

Outcome = Literal["sent", "no_invoices", "failed"]


@dataclass(frozen=True)
class AlumneregningResult:
    resident: Resident
    outcome: Outcome
    detail: str = ""


def _kr(amount: Decimal) -> str:
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _body(resident: Resident, balance: Decimal) -> str:
    saldo_line = (
        f"Din saldo er nu {_kr(balance)} kr. — det er din gæld til kollegiet."
        if balance >= 0
        else f"Din saldo er nu {_kr(balance)} kr. — kollegiet skylder dig penge."
    )
    return "\n".join(
        [
            f"Hej {resident.first_name},",
            "",
            "Os i Regnskabsgruppen sender dig hermed (vedhæftet) din alumneregning.",
            "",
            saldo_line,
            "",
            "Vi beder om at du generelt forsøger at holde din gæld så lav som muligt.",
            "",
            (
                "Hvis du skylder mere end 600 kr. beder vi om at du indbetaler inden for udgangen af "
                "indeværende uge. Skulle du have brug for en speciel aftale om afbetaling, fordi din "
                "gæld er for stor til, at du kan betale den af på én gang, så bedes du kontakte det "
                "siddende inspektionsmedlem i Regnskabsgruppen."
            ),
            "",
            "UDFLYTTER-INFO: Denne regning skal ikke betales.",
            (
                "I tilfælde af at du skylder alumnekassen penge bliver dette fratrukket dit depositum "
                "som PKS tilbagebetaler snarest."
            ),
            "",
            "-" * 72,
            "",
            "På vegne af Regnskabsgruppen på G. A. Hagemanns Kollegium",
            "",
            f"Reg. nr. {settings.ALUMNEREGNING_BANK_REG} Konto nr. {settings.ALUMNEREGNING_BANK_ACCOUNT}",
            "",
            "-" * 72,
        ]
    )


def _send_one(resident: Resident, customer_number: int) -> AlumneregningResult:
    ledger.sync_entries(resident, customer_number)
    balance = ledger.balance(resident)

    invoices = economic.get_booked_invoices(customer_number)
    if not invoices:
        return AlumneregningResult(resident, "no_invoices", "Ingen bogførte fakturaer i e-conomic.")

    latest = max(invoices, key=lambda inv: inv.date)
    pdf = economic.get_invoice_pdf(latest.pdf_url)

    email = EmailMessage(
        subject="Alumneregning",
        body=_body(resident, balance),
        from_email=f"Regnskabsgruppen <{settings.ALUMNEREGNING_FROM_EMAIL}>",
        to=[resident.email],
    )
    email.attach(f"alumneregning-{latest.date}.pdf", pdf, "application/pdf")
    email.send()
    return AlumneregningResult(resident, "sent", f"Faktura {latest.booked_invoice_number} ({latest.date}).")


def send_alumneregninger() -> list[AlumneregningResult]:
    """Email every resident with an e-conomic customerNumber their latest statement. Best-effort per
    resident: one failing (e-conomic error, bad data) does not stop the rest — reported in the
    results instead, same as the old script's per-row error printing."""
    results: list[AlumneregningResult] = []
    residents = Resident.objects.filter(economic_customer_number__isnull=False).order_by(
        "first_name", "last_name"
    )
    for resident in residents:
        try:
            results.append(_send_one(resident, resident.economic_customer_number))
        except economic.EconomicApiError as exc:
            logger.warning("alumneregning failed for resident %s: %s", resident.pk, exc)
            results.append(AlumneregningResult(resident, "failed", str(exc)))
    return results
