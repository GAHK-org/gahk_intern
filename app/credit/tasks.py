from celery import shared_task
from django.utils import timezone

from . import anchors, checks, statements
from .models import Anchor


@shared_task
def check_ledger() -> dict[str, int]:
    """Nightly: recompute every chain, balance and book total, and the latest anchors (credit.checks)."""
    report = checks.run_and_report()
    return {"accounts": report.accounts, "postings": report.postings, "faults": len(report.faults)}


@shared_task
def anchor_daily() -> int:
    """Nightly: fix the whole ledger as it stands, outside the database (credit.anchors)."""
    as_of = timezone.now().replace(second=0, microsecond=0)
    anchor = anchors.make(Anchor.Kind.DAILY, as_of)
    if not anchor.deliveries.filter(ok=True).exists():
        anchors.deliver(anchor)
    return anchor.pk


@shared_task
def send_statements() -> int:
    """The 1st of each month: last month's statement to every holder (credit.statements)."""
    year, month = statements.previous_month(timezone.localdate())
    return statements.send_month(year, month)
