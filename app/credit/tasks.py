from celery import shared_task

from . import checks


@shared_task
def check_ledger() -> dict[str, int]:
    """Nightly: recompute every chain, balance and book total (credit.checks)."""
    report = checks.run_and_report()
    return {"accounts": report.accounts, "postings": report.postings, "faults": len(report.faults)}
