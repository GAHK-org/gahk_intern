"""The nightly check: recompute the whole ledger from its rows and compare it with what is stored.

What it checks, for every account and every book:

  * the chain: each posting's prev_hash is the previous posting's row_hash (zeros for the first),
    and its row_hash is what its row and transfer hash to (credit.chain);
  * the balance: each posting's balance_after is the previous one's plus its amount;
  * each transfer: exactly two postings, one out and one in, summing to 0;
  * each book: its balances add up to 0;
  * each pair of mellemregning accounts: what one book owes the other mirrors the other's. A pair
    out of step is a settlement half recorded: reported, but as something to finish, not a fault.

A fault is never fixed here: the ledger is append-only and the stored rows are the evidence. The
check reports, and a person decides (a correction is a new transfer).
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from django.db.models import Count, OuterRef, Q, Subquery, Sum

from audit import trail

from . import anchors, chain, ledger
from .models import Anchor, Book, CreditAccount, Posting, Transfer

logger = logging.getLogger(__name__)


@dataclass
class Report:
    faults: list[str] = field(default_factory=list)
    in_transit: list[str] = field(default_factory=list)
    accounts: int = 0
    postings: int = 0
    heads: dict[int, bytes] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.faults


def run(*, batch: int = 2000) -> Report:
    report = Report()
    other_side = (
        Posting.objects.filter(transfer=OuterRef("transfer"))
        .exclude(pk=OuterRef("pk"))
        .values("account_id")[:1]
    )
    for account in CreditAccount.objects.order_by("pk").iterator():
        report.accounts += 1
        prev, balance = chain.GENESIS, 0
        postings = (
            Posting.objects.filter(account=account)
            .select_related("transfer")
            .annotate(other=Subquery(other_side))
            .order_by("id")
        )
        for posting in postings.iterator(chunk_size=batch):
            report.postings += 1
            other = posting.other
            if bytes(posting.prev_hash) != prev:
                report.faults.append(f"Konto {account.pk}: postering {posting.pk} følger ikke kæden.")
            if posting.balance_after != balance + posting.amount_ore:
                report.faults.append(f"Konto {account.pk}: postering {posting.pk} har forkert saldo.")
            if other is None or ledger.posting_hash(posting, other) != bytes(posting.row_hash):
                report.faults.append(f"Konto {account.pk}: postering {posting.pk} passer ikke med sin hash.")
            prev, balance = bytes(posting.row_hash), posting.balance_after
        report.heads[account.pk] = prev
    unbalanced = (
        Transfer.objects.annotate(
            n=Count("postings"),
            total=Sum("postings__amount_ore"),
            out=Count("postings", filter=Q(postings__amount_ore__lt=0)),
        )
        .exclude(n=2, total=0, out=1)
        .values_list("pk", flat=True)
    )
    for pk in unbalanced:
        report.faults.append(f"Overførsel {pk} har ikke præcis to posteringer, der går i nul.")
    for book in Book.objects.all():
        total = ledger.book_total(book)
        if total != 0:
            report.faults.append(f"{book.name} går ikke i nul ({total} øre).")
    owed: dict[tuple[int, int], int] = defaultdict(int)
    for account in ledger.with_balances(CreditAccount.objects.filter(counterpart_book__isnull=False)):
        if account.counterpart_book_id is not None:
            owed[(account.book_id, account.counterpart_book_id)] = ledger.annotated_balance(account)
    for anchor in _recent_anchors():
        report.faults += anchors.verify(anchor)
    names = dict(Book.objects.values_list("pk", "name"))
    for (one, other), amount in owed.items():
        if one < other and amount + owed.get((other, one), 0) != 0:
            report.in_transit.append(
                f"Mellemregningen mellem {names[one]} og {names[other]} er ikke afstemt."
            )
    return report


def _recent_anchors() -> list[Anchor]:
    """What the nightly check re-proves: the last three daily anchors, the last two month anchors and
    each book's latest closing. All of them: manage.py credit_verify."""
    picked = list(Anchor.objects.filter(kind=Anchor.Kind.DAILY).order_by("-as_of")[:3])
    picked += list(Anchor.objects.filter(kind=Anchor.Kind.MONTH).order_by("-as_of")[:2])
    for book in Book.objects.all():
        latest = Anchor.objects.filter(kind=Anchor.Kind.CLOSING, book=book).order_by("-as_of").first()
        if latest is not None:
            picked.append(latest)
    return picked


def run_and_report() -> Report:
    """The nightly job: run the check, and on a fault write it to the log and the audit trail."""
    report = run()
    if report.faults:
        for fault in report.faults:
            logger.error("GAHK-konto: %s", fault)
        trail.record(
            "credit.check.failed",
            actor=None,
            details={"faults": report.faults[:50], "fault_count": len(report.faults)},
        )
    for note in report.in_transit:
        logger.warning("GAHK-konto: %s", note)
    return report
