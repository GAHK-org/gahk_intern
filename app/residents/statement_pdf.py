"""Generates the alumneregning statement PDF attached to each mail (F-010 regnskab role).

Replaces fetching a rendered invoice/kontokort PDF from e-conomic: GAHK tracks alumni via per-person
chart-of-accounts entries, not e-conomic Customers/Invoices, so there is no e-conomic-rendered PDF to
fetch for an alumne's statement (see residents.economic.get_account's docstring). This builds the same
"kontokort" shape the old manual process downloaded — the period's postings (both posted and draft —
regnskab wants drafts included, not just formally booked entries, see economic.get_entries_in_range),
each with a running subtotal, plus the account's actual current balance (not derived from the
postings shown: an alumne can have a nonzero balance with nothing posted in the given period, carried
forward from before). Draft lines are marked "(kladde)" — unlike a posted entry, e-conomic's own
draft can still be edited or deleted before it is formally booked.

prepend_followup() then merges in the regnskab-uploaded følgebrev (AlumneregningSettings), if any, as a
permanent first page ahead of the rendered statement — see residents.alumneregning for where that's
called from.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from django.conf import settings
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from pypdf import PdfReader, PdfWriter

from . import economic
from .models import Resident


def _kr(amount: Decimal) -> str:
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def prepend_followup(followup_pdf: bytes, statement_pdf: bytes) -> bytes:
    """Merges the regnskab-uploaded følgebrev in front of the generated statement, producing one PDF —
    the følgebrev is a permanent first page (updated roughly every 6 months by regnskab), not a second
    mail attachment, so a recipient sees it before the figures without having to open a second file."""
    writer = PdfWriter()
    for source in (followup_pdf, statement_pdf):
        for page in PdfReader(io.BytesIO(source)).pages:
            writer.add_page(page)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def render(
    resident: Resident,
    account_number: int,
    entries: list[economic.LedgerEntry],
    balance: Decimal,
    date_from: date,
    date_to: date,
) -> bytes:
    """A one-page account statement: the period's postings (if any) with a running subtotal, plus the
    account's actual current balance from e-conomic."""
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

    if entries:
        pdf.set_font("helvetica", "B", 10)
        pdf.cell(28, 8, "Dato", border="B")
        pdf.cell(104, 8, "Tekst", border="B")
        pdf.cell(28, 8, "Beløb", border="B", align="R")
        pdf.cell(28, 8, "Subtotal", border="B", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.set_font("helvetica", "", 10)
        running = Decimal(0)
        for entry in sorted(entries, key=lambda e: e.date):
            amount = Decimal(str(entry.amount))
            running += amount
            text = (entry.text or "") + (" (kladde)" if entry.is_draft else "")
            pdf.cell(28, 7, entry.date)
            pdf.cell(104, 7, text[:58])
            pdf.cell(28, 7, _kr(amount), align="R")
            pdf.cell(28, 7, _kr(running), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(5)
    else:
        pdf.set_font("helvetica", "", 10)
        pdf.cell(0, 8, "Ingen nye posteringer i perioden.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(5)

    pdf.set_font("helvetica", "B", 11)
    pdf.cell(0, 8, f"Saldo (aktuel kontosaldo): {_kr(balance)} kr", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    _render_how_to_read(pdf)

    return bytes(pdf.output())


def _render_how_to_read(pdf: FPDF) -> None:
    """The permanent "how to read your alumneregning" explainer (carried over verbatim from the old
    følgebrev.pdf's page 3). Baked into every statement rather than left to the optional, freely-edited
    følgebrev upload, so it's never missing just because nobody attached a følgebrev for a given send."""
    pdf.ln(10)
    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 8, "HOW TO AFLÆSE DIN ALUMNEREGNING", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("helvetica", "", 10)
    pdf.multi_cell(
        0,
        6,
        'Det nederste tal i søjlen til højre under "saldo" er din gæld til kollegiet, aka hvor '
        "meget du skylder fællesskabet. Hvis beløbet er negativt skylder kollegiet dig penge - "
        "GAHK står i gæld til dig.",
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.ln(3)
    pdf.cell(0, 6, "Betalingen sendes til:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Reg nr: {settings.ALUMNEREGNING_BANK_REG}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Konto nr: {settings.ALUMNEREGNING_BANK_ACCOUNT}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)
    pdf.cell(0, 6, "Ved overførsel, husk:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, "- Skriv dit navn", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, "- Overfør i hele kroner", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, "- Din gæld må ikke overstige 600 kroner.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)
    pdf.cell(
        0,
        6,
        f"Hvis du har spørgsmål, så kontakt din søde regnskabsgruppe på mail: {settings.ALUMNEREGNING_FROM_EMAIL}",
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
