"""GAHK-konto's monthly statement as a PDF, signed. Its own code, apart from Regnskab's alumneregning.

For each account: the opening and closing balance, the month's lines, and two control numbers:
this month's chain head and last month's. Last month's number must be the one printed on last
month's statement; and the shared number at the bottom (the month anchor over every account) is the
same on every holder's statement that month, so each PDF witnesses the whole ledger. A QR code
carries the same numbers for a machine to read.
"""

import datetime
import io
import json
from dataclasses import dataclass

import segno
from django.utils import timezone
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from . import signing
from .models import Anchor, CreditAccount, Posting

MONTHS = [
    "januar",
    "februar",
    "marts",
    "april",
    "maj",
    "juni",
    "juli",
    "august",
    "september",
    "oktober",
    "november",
    "december",
]


@dataclass(frozen=True)
class AccountMonth:
    account: CreditAccount
    opening: int
    closing: int
    lines: list[tuple[Posting, CreditAccount]]  # each posting with the account on the other side
    head: bytes  # the chain head at the month's end
    previous_head: bytes  # at its start: last month's number


class _PDF(FPDF):
    """The built-in fonts are latin-1: anything else is drawn as "?" rather than stopping the send."""

    def normalize_text(self, text: str) -> str:
        return super().normalize_text(text.encode("latin-1", "replace").decode("latin-1"))


def _kr(ore: int) -> str:
    sign = "-" if ore < 0 else ""
    whole, cents = divmod(abs(ore), 100)
    return f"{sign}{whole:,}".replace(",", ".") + f",{cents:02d}"


def _grouped(digest: bytes) -> str:
    text = digest.hex()
    return " ".join(text[i : i + 8] for i in range(0, len(text), 8))


def render(
    *,
    holder_name: str,
    year: int,
    month: int,
    months: list[AccountMonth],
    anchor: Anchor,
    made: datetime.datetime,
) -> bytes:
    pdf = _PDF()
    pdf.set_title(f"GAHK-konto {MONTHS[month - 1]} {year}")
    pdf.set_author("GAHK Intern")
    pdf.add_page()
    pdf.set_font("helvetica", "B", 16)
    pdf.cell(0, 9, f"GAHK-konto - kontoudtog {MONTHS[month - 1]} {year}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 6, "G. A. Hagemanns Kollegium", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, holder_name, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)
    for item in months:
        account = item.account
        pdf.set_font("helvetica", "B", 12)
        pdf.cell(0, 8, f"{account.name} ({account.book.name})", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("helvetica", "", 9)
        pdf.cell(0, 5, f"Primosaldo: {_kr(item.opening)} kr", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        widths = (24, 52, 64, 25, 25)
        pdf.set_font("helvetica", "B", 9)
        for width, title in zip(widths, ("Dato", "Til / fra", "Tekst", "Beløb", "Saldo"), strict=True):
            pdf.cell(width, 6, title, border="B", align="R" if title in ("Beløb", "Saldo") else "L")
        pdf.ln()
        pdf.set_font("helvetica", "", 9)
        for posting, other in item.lines:
            when = timezone.localtime(posting.transfer.created_at)
            direction = "Til" if posting.amount_ore < 0 else "Fra"
            cells = (
                f"{when:%d.%m.%Y}",
                f"{direction} {other.holder.name}"[:34],
                posting.text[:44],
                _kr(posting.amount_ore),
                _kr(posting.balance_after),
            )
            for width, text, right in zip(widths, cells, (False, False, False, True, True), strict=True):
                pdf.cell(width, 5, text, align="R" if right else "L")
            pdf.ln()
        if not item.lines:
            pdf.cell(0, 5, "Ingen bevægelser i måneden.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("helvetica", "B", 9)
        pdf.cell(0, 6, f"Ultimosaldo: {_kr(item.closing)} kr", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("courier", "", 7)
        pdf.cell(
            0, 4, f"Kontrolnummer denne måned:  {_grouped(item.head)}", new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )
        pdf.cell(
            0,
            4,
            f"Kontrolnummer sidste måned: {_grouped(item.previous_head)}",
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )
        qr = {
            "account": account.pk,
            "month": f"{year}-{month:02d}",
            "head": item.head.hex(),
            "previous": item.previous_head.hex(),
            "anchor": bytes(anchor.anchor_hash).hex(),
        }
        image = io.BytesIO()
        segno.make(json.dumps(qr, separators=(",", ":")), error="m").save(
            image, kind="png", scale=3, border=2
        )
        image.seek(0)
        pdf.image(image, w=32)
        pdf.ln(2)
    pdf.ln(2)
    pdf.set_font("helvetica", "B", 9)
    pdf.cell(
        0,
        5,
        "Fælles kontrolnummer for alle GAHK-konti ved månedens udgang",
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.set_font("courier", "", 7)
    pdf.cell(0, 4, _grouped(bytes(anchor.anchor_hash)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("helvetica", "", 8)
    pdf.multi_cell(
        0,
        4,
        'Kontrolnummeret for sidste måned skal være det samme som "denne måned" på sidste måneds udtog. '
        "Det fælles kontrolnummer står på alle udtog for måneden; afviger to udtog, er der ændret i "
        "historikken. Udtoget er signeret digitalt af GAHK Intern "
        f"(certifikat {signing.pdf_certificate_fingerprint()[:32]}...). Lavet {timezone.localtime(made):%d.%m.%Y %H:%M}.",
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    raw = bytes(pdf.output())
    return signing.sign_pdf(raw, reason=f"GAHK-konto kontoudtog {MONTHS[month - 1]} {year}")
