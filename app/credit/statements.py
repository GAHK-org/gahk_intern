"""Monthly statements: on the 1st, every holder gets one signed PDF of their accounts for the month
that ended, by e-mail.

  * The month anchor (credit.anchors, every account as of the 1st at 00:00) is made and delivered
    first: every statement prints it.
  * A holder's accounts are in it when they moved that month or end it with a balance; a holder with
    none gets nothing.
  * A person gets it at their login's address; an embedsgruppe's goes to its current members. A
    person without a login gets none by mail; their book's managers fetch it on the account page.
  * Each holder's statement is sent once (Statement, unique per month): a rerun skips them.
"""

import datetime
import hashlib
import logging

from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.utils import timezone

from core import mail as outbox

from . import anchors, holders
from .models import Anchor, CreditAccount, Holder, Posting, Statement
from .statement_pdf import MONTHS, AccountMonth, render

logger = logging.getLogger(__name__)


def bounds(year: int, month: int) -> tuple[datetime.datetime, datetime.datetime]:
    start = anchors.month_start(year, month)
    end = anchors.month_start(year + month // 12, month % 12 + 1)
    return start, end


def previous_month(today: datetime.date) -> tuple[int, int]:
    first = today.replace(day=1) - datetime.timedelta(days=1)
    return first.year, first.month


def month_anchor(year: int, month: int) -> Anchor:
    """The anchor every statement for the month prints, made (and delivered) the first time."""
    _, end = bounds(year, month)
    if end > timezone.now():
        raise ValueError("The month has not ended")
    anchor = anchors.make(Anchor.Kind.MONTH, end)
    if not anchor.deliveries.filter(ok=True).exists():
        anchors.deliver(anchor)
    return anchor


def account_month(account: CreditAccount, year: int, month: int) -> AccountMonth:
    start, end = bounds(year, month)
    before = Posting.objects.filter(account=account, transfer__created_at__lt=start).order_by("-id").first()
    until = Posting.objects.filter(account=account, transfer__created_at__lt=end).order_by("-id").first()
    postings = list(
        Posting.objects.filter(account=account, transfer__created_at__gte=start, transfer__created_at__lt=end)
        .select_related("transfer")
        .order_by("id")
    )
    others = {
        p.transfer_id: p.account
        for p in Posting.objects.filter(transfer_id__in=[p.transfer_id for p in postings])
        .exclude(account=account)
        .select_related("account__holder__person", "account__holder__workgroup")
    }
    return AccountMonth(
        account=account,
        opening=before.balance_after if before else 0,
        closing=until.balance_after if until else 0,
        lines=[(p, others[p.transfer_id]) for p in postings],
        head=bytes(until.row_hash) if until else bytes(32),
        previous_head=bytes(before.row_hash) if before else bytes(32),
    )


def months_for(holder: Holder, year: int, month: int) -> list[AccountMonth]:
    """The holder's accounts worth a statement this month: moved, or ending with a balance. Their
    bank and mellemregning accounts are a book's, shown on its page, not on a statement."""
    _, end = bounds(year, month)
    accounts = (
        CreditAccount.objects.filter(
            holder=holder, created_at__lt=end, counterpart_book__isnull=True, bank_reference=""
        )
        .select_related("book")
        .order_by("book__name", "name")
    )
    out = []
    for account in accounts:
        item = account_month(account, year, month)
        if item.lines or item.closing != 0:
            out.append(item)
    return out


def pdf_for(holder_name: str, items: list[AccountMonth], year: int, month: int) -> bytes:
    return render(
        holder_name=holder_name,
        year=year,
        month=month,
        months=items,
        anchor=month_anchor(year, month),
        made=timezone.now(),
    )


def recipients(holder: Holder) -> list[str]:
    return sorted({a.email for a in holders.members(holder) if a.email})


def send_month(year: int, month: int) -> int:
    """Send every holder's statement for the month once. Returns how many went out."""
    anchor = month_anchor(year, month)
    sent = 0
    for holder in Holder.objects.select_related("person", "workgroup").order_by("pk"):
        if Statement.objects.filter(holder=holder, year=year, month=month).exists():
            continue
        items = months_for(holder, year, month)
        to = recipients(holder)
        if not items or not to:
            continue
        pdf = pdf_for(holder.name, items, year, month)
        try:
            with transaction.atomic():
                Statement.objects.create(
                    holder=holder,
                    year=year,
                    month=month,
                    anchor=anchor,
                    recipients="\n".join(to),
                    pdf_sha256=hashlib.sha256(pdf).hexdigest(),
                )
                message = EmailMessage(
                    subject=f"GAHK-konto: kontoudtog for {MONTHS[month - 1]} {year}",
                    body=(
                        f"Hej {holder.name}\n\nHer er kontoudtoget for dine GAHK-konti for {MONTHS[month - 1]} {year}. "
                        "Gem det: kontrolnumrene på det bruges til at tjekke, at ingen har ændret i historikken.\n"
                    ),
                    to=to,
                )
                message.attach(f"gahk-konto-{year}-{month:02d}.pdf", pdf, "application/pdf")
                with outbox.kind(outbox.Kind.BULK):
                    message.send()
        except IntegrityError:  # sent meanwhile by another run
            continue
        sent += 1
    return sent
