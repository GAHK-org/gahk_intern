"""Closing a book's period: its managers sign off on how it stood at the period's end.

A period is a month, a half year or a year (Book.closing_months), one after the other from the
month the book was made. It can be closed once it has ended (and a few minutes more, so a transfer
in flight at midnight has landed), and only when:

  * every bank account's balance matches the bank statement its managers type in, and
  * every mellemregning is settled, or the closing says why not (a settlement in transit).

Closing stores every account's balance and chain head at the end, makes and delivers the book's
anchor over them, and from then on the database refuses any posting into the closed period. A
posting belongs to the period it was recorded in, so a correction found later goes into the open
one, as a new transfer.
"""

import datetime

from django.db import transaction
from django.utils import timezone

from audit import trail
from residents.models import Account

from . import anchors, holders
from .ledger import LedgerError
from .models import Anchor, Book, Closing, ClosingBalance, CreditAccount

GRACE = datetime.timedelta(minutes=10)


def next_period(book: Book) -> tuple[datetime.date, datetime.date]:
    """(first day, last day) of the book's first unclosed period."""
    last = book.closings.order_by("-period_end").first()
    if last is not None:
        start = last.period_end + datetime.timedelta(days=1)
    else:
        made = timezone.localtime(book.created_at).date()
        start = made.replace(day=1)
    end = _months_after(start, book.closing_months) - datetime.timedelta(days=1)
    return start, end


def _months_after(first: datetime.date, months: int) -> datetime.date:
    """The first day `months` months after `first` (itself a first of a month)."""
    years, index = divmod(first.month - 1 + months, 12)
    return datetime.date(first.year + years, index + 1, 1)


def ends_at(period_end: datetime.date) -> datetime.datetime:
    return timezone.make_aware(
        datetime.datetime.combine(period_end + datetime.timedelta(days=1), datetime.time())
    )


def may_close(book: Book, now: datetime.datetime | None = None) -> bool:
    _, end = next_period(book)
    return (now or timezone.now()) >= ends_at(end) + GRACE


def close(
    *,
    actor: Account,
    book: Book,
    bank_statements: dict[int, int],
    note: str = "",
    now: datetime.datetime | None = None,
) -> Closing:
    """Close the book's next period. `bank_statements`: each bank account's id → the balance its bank
    statement shows at the period's end, in øre (money in the bank is positive there)."""
    if not holders.manages(actor, book):
        raise LedgerError("Kun kontoførerens ansvarlige kan lukke en periode.")
    start, end = next_period(book)
    moment = ends_at(end)
    if (now or timezone.now()) < moment + GRACE:
        raise LedgerError(f"Perioden {start:%d.%m.%Y}–{end:%d.%m.%Y} er ikke slut endnu.")
    with transaction.atomic():
        Book.objects.select_for_update().get(pk=book.pk)  # one closing at a time
        if book.closings.filter(period_end=end).exists():
            raise LedgerError("Perioden er allerede lukket.")
        heads = {h.account_id: h for h in anchors.heads_as_of(moment, book=book)}
        accounts = {a.pk: a for a in CreditAccount.objects.filter(pk__in=heads)}
        wrong = []
        for pk, account in accounts.items():
            if account.bank_reference:
                typed = bank_statements.get(pk)
                ledger_says = -heads[pk].balance  # money in the bank is negative in the ledger
                if typed is None or typed != ledger_says:
                    wrong.append(f"{account.name}: banken {_kr(typed)}, GAHK-konto {_kr(ledger_says)}")
        if wrong:
            raise LedgerError("Bankkontoen stemmer ikke: " + "; ".join(wrong) + ".")
        unsettled = [a.name for pk, a in accounts.items() if a.is_mellemregning and heads[pk].balance != 0]
        if unsettled and not note.strip():
            raise LedgerError(f"Ikke afregnet: {', '.join(unsettled)}. Skriv hvorfor, eller afregn først.")
        with trail.event("credit.book.closed", subject=book, details={"period_end": end.isoformat()}):
            anchor = anchors.make(Anchor.Kind.CLOSING, moment, book=book)
            closing = Closing.objects.create(
                book=book,
                period_start=start,
                period_end=end,
                ends_at=moment,
                previous=book.closings.order_by("-period_end").first(),
                closed_by=actor,
                note=note.strip(),
                anchor=anchor,
            )
            for pk, head in heads.items():
                ClosingBalance.objects.create(
                    closing=closing,
                    account_id=pk,
                    balance=head.balance,
                    last_posting_id=head.posting_id,
                    chain_head=head.head,
                    bank_statement_ore=bank_statements.get(pk) if accounts[pk].bank_reference else None,
                )
        transaction.on_commit(lambda: anchors.deliver(anchor))
    return closing


def _kr(ore: int | None) -> str:
    from .templatetags.credit_extras import kr

    return "intet tal" if ore is None else kr(ore)
