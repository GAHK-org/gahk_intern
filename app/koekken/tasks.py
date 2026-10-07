import logging

from celery import shared_task
from django.conf import settings
from django.core.management import call_command

logger = logging.getLogger(__name__)

# See `config/settings.py`'s `KOEKKEN_JOBS_ENABLED` comment for the full reasoning: this feature
# ships behind `koekken.access.Gate`, but that Gate reads `effective_roles(request)` and there is no
# request in a Celery task context, so these two scheduled jobs need their own settings flag while
# the feature is staged closed. Default False — P1 has no credit path (P2's self-report UDFOERT
# flow), so letting these run would post real monthly obligation debt with no way to offset it.


@shared_task
def generate_koekkenvagter() -> None:
    """Generate this month's Vagt rows (idempotent) — no-ops while KOEKKEN_JOBS_ENABLED is False."""
    if not settings.KOEKKEN_JOBS_ENABLED:
        logger.info("koekken.tasks.generate_koekkenvagter skipped: KOEKKEN_JOBS_ENABLED is False.")
        return
    call_command("generate_koekkenvagter")


@shared_task
def post_koekken_obligation() -> None:
    """Post this month's FORPLIGTELSE ledger entries (idempotent) — no-ops while
    KOEKKEN_JOBS_ENABLED is False."""
    if not settings.KOEKKEN_JOBS_ENABLED:
        logger.info("koekken.tasks.post_koekken_obligation skipped: KOEKKEN_JOBS_ENABLED is False.")
        return
    call_command("post_koekken_obligation")


@shared_task
def roll_forward_koekkenvagter() -> None:
    """Advance the tier-A allocation window one month within the active periode (idempotent —
    Amendment 1, A1.2) — no-ops while KOEKKEN_JOBS_ENABLED is False, the same gate as the two tasks
    above. Allocation itself never writes a KoekkenPost ledger entry (only VagtTildeling rows), so
    this gate isn't a debt-accrual safety requirement the way it is for post_koekken_obligation —
    it's gated anyway, for consistency with the other two koekken jobs while the feature is staged
    closed."""
    if not settings.KOEKKEN_JOBS_ENABLED:
        logger.info("koekken.tasks.roll_forward_koekkenvagter skipped: KOEKKEN_JOBS_ENABLED is False.")
        return
    call_command("roll_forward_koekkenvagter")


@shared_task
def reconcile_koekkenvagter() -> None:
    """Reconcile tier-A assignments against the real Residency list, additive only (Amendment 2/3,
    A2.3/A3.1) — no-ops while KOEKKEN_JOBS_ENABLED is False, the same gate as the other three. Like
    roll_forward_koekkenvagter, this never writes a KoekkenPost ledger entry -- only VagtTildeling
    rows -- so the gate is for consistency with the other koekken jobs while the feature is staged
    closed, not a debt-accrual safety requirement."""
    if not settings.KOEKKEN_JOBS_ENABLED:
        logger.info("koekken.tasks.reconcile_koekkenvagter skipped: KOEKKEN_JOBS_ENABLED is False.")
        return
    call_command("reconcile_koekkenvagter")
