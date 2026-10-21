"""Generates the alumneregning statement PDF attached to each mail (F-010 regnskab role).

Replaces fetching a rendered invoice/kontokort PDF from e-conomic: GAHK tracks alumni via per-person
chart-of-accounts entries, not e-conomic Customers/Invoices, so there is no e-conomic-rendered PDF to
fetch for an alumne's statement (see residents.economic.get_period_entries's docstring). This builds
the same "kontokort" shape the old manual process downloaded — date, description, amount, running
balance — from the entries already fetched for this send.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from . import economic
from .models import Resident


def _kr(amount: Decimal) -> str:
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def render(
    resident: Resident,
    account_number: int,
    entries: list[economic.LedgerEntry],
    date_from: date,
    date_to: date,
) -> bytes:
    """A one-page account statement: every entry in [date_from, date_to] with a running balance."""
    pdf = FPDF()
    pdf.add_page()

    pdf.set_font("helvetica", "B", 16)
    pdf.cell(0, 10, "Alumneregning", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("helvetica", "", 11)
    pdf.cell(0, 7, "G. A. Hagemanns Kollegium", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(
        0, 7, f"{resident.full_name} (e-conomic konto {account_number})", new_x=XPos.LMARGIN, new_y=YPos.NEXT
    )
    pdf.cell(
        0, 7, f"Periode: {date_from.isoformat()} - {date_to.isoformat()}", new_x=XPos.LMARGIN, new_y=YPos.NEXT
    )
    pdf.ln(5)

    pdf.set_font("helvetica", "B", 10)
    pdf.cell(28, 8, "Dato", border="B")
    pdf.cell(104, 8, "Tekst", border="B")
    pdf.cell(28, 8, "Beløb", border="B", align="R")
    pdf.cell(28, 8, "Saldo", border="B", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("helvetica", "", 10)
    running = Decimal(0)
    for entry in sorted(entries, key=lambda e: e.date):
        amount = Decimal(str(entry.amount))
        running += amount
        pdf.cell(28, 7, entry.date)
        pdf.cell(104, 7, (entry.text or "")[:58])
        pdf.cell(28, 7, _kr(amount), align="R")
        pdf.cell(28, 7, _kr(running), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(5)
    pdf.set_font("helvetica", "B", 11)
    pdf.cell(0, 8, f"Saldo i alt: {_kr(running)} kr", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    return bytes(pdf.output())
