"""AK monthly-charge reconciliation.

The AK calendar (per-month `AkMonthlyCharge` config) is the source of truth for the monthly kryds
deduction. `apply_monthly_charge` makes the append-only `AkEntry` ledger match it for one period:
each resident on that month's alumneliste gets exactly one MONTHLY entry of `-krydser`; residents not
on the list are never charged (and any stale charge is removed). Idempotent — safe to re-run.

A month is charged once it is OVER, on the night of the 1st of the next month, from that month's
final alumneliste. Charging it as the month started read the list copied forward from the month
before, so a resident who moved out and was removed afterwards kept a −2 for a month they did not
live here.
"""

import logging

from django.db import transaction

from residents.models import Residency, Resident, prev_period

from .models import AkAutoApply, AkEntry, AkMonthlyCharge

logger = logging.getLogger(__name__)


def _tag(year: int, month: int) -> str:
    """Human-readable reason, kept for continuity with the legacy/monthly convention."""
    return f"Månedlig vurdering {year}-{month:02d}"


@transaction.atomic
def apply_monthly_charge(year: int, month: int, *, officer: Resident | None = None) -> tuple[int, int]:
    """Reconcile the MONTHLY ledger for (year, month) against the calendar-month schedule and current
    membership. The amount comes from the `AkMonthlyCharge` row for that *calendar* month (same every
    year).

    Returns (written, removed): how many MONTHLY entries were created/updated and how many were deleted.
    If that month's schedule is missing or inactive, every MONTHLY entry for the period is removed.
    """
    charge = AkMonthlyCharge.objects.filter(month=month).first()
    existing = AkEntry.objects.filter(kind=AkEntry.Kind.MONTHLY, year=year, month=month)

    if charge is None or not charge.active:
        removed = existing.delete()[0]
        return (0, removed)

    member_ids = set(Residency.objects.filter(year=year, month=month).values_list("resident_id", flat=True))

    # Drop charges for anyone no longer on that month's list (e.g. removed from the alumneliste).
    removed = existing.exclude(resident_id__in=member_ids).delete()[0]

    written = 0
    for resident_id in member_ids:
        AkEntry.objects.update_or_create(
            resident_id=resident_id,
            kind=AkEntry.Kind.MONTHLY,
            year=year,
            month=month,
            defaults={
                "delta": -charge.krydser,
                "reason": _tag(year, month),
                "created_by": officer,
            },
        )
        written += 1
    return (written, removed)


def charge_period() -> tuple[int, int]:
    """The (year, month) due for charging: the calendar month before today. Calendar-based rather
    than `active_period()`, so a month whose successor's list has not been published yet is still
    charged on the 1st."""
    from core.clock import current_date

    today = current_date()
    return prev_period((today.year, today.month))


def charge_previous_month() -> tuple[int, int, int, int]:
    """Charge the month that just ended and record it as handled. Returns (year, month, written,
    removed). Used by the `ak_monthly_assessment` task and the lazy backstop below."""
    year, month = charge_period()
    written, removed = apply_monthly_charge(year, month)
    state = AkAutoApply.get()
    state.year, state.month = year, month
    state.save(update_fields=["year", "month"])
    return (year, month, written, removed)


def ensure_previous_month_charged() -> None:
    """Charge the month that just ended if the scheduled task has not already done it.

    The `ak_monthly_assessment` task (DEPLOY.md §4b) normally gets there first, on the night of the
    1st; this is the backstop that makes a missed or misconfigured schedule self-healing instead of
    silently leaving every resident's balance wrong. Called from hot internal pages (the dashboard and
    the AK pages) and cheap there — a single-row read of the marker — because it only does real work
    when a new month has started since the last charge. Failures are logged, not raised, so
    a page load is never broken by this; the marker is only advanced on success, so the next request
    retries.
    """
    state = AkAutoApply.get()
    if state.year is not None and (state.year, state.month) >= charge_period():
        return
    try:
        charge_previous_month()
    except Exception:
        year, month = charge_period()
        logger.exception("AK auto-charge failed for %s-%02d", year, month)
