"""AK monthly-charge reconciliation (F-009).

The AK calendar (per-month `AkMonthlyCharge` config) is the source of truth for the monthly kryds
deduction. `apply_monthly_charge` makes the append-only `AkEntry` ledger match it for one period:
each resident on that month's alumneliste gets exactly one MONTHLY entry of `-krydser`; residents not
on the list are never charged (and any stale charge is removed). Idempotent — safe to re-run.
"""

import logging

from django.db import transaction

from residents.models import Residency, Resident, active_period

from .models import AkAutoApply, AkEntry, AkMonthlyCharge

logger = logging.getLogger(__name__)


def _tag(year: int, month: int) -> str:
    """Human-readable reason, kept for continuity with the legacy/monthly convention."""
    return f"Månedlig vurdering {year}-{month:02d}"


def ensure_schedule_row(month: int) -> AkMonthlyCharge:
    """The schedule row for a calendar month, creating the historical default (active, 2 krydser) if it
    is missing. Both the scheduled command and the page-load backstop go through this, so a missing
    row means the same thing whichever of them reaches the month first — it used to be "charge 2" from
    the command but "delete every charge for the period" from the backstop."""
    return AkMonthlyCharge.objects.get_or_create(month=month, defaults={"active": True, "krydser": 2})[0]


@transaction.atomic
def apply_monthly_charge(year: int, month: int, *, officer: Resident | None = None) -> tuple[int, int]:
    """Reconcile the MONTHLY ledger for (year, month) against the calendar-month schedule and current
    membership. The amount comes from the `AkMonthlyCharge` row for that *calendar* month (same every
    year).

    Returns (written, removed): how many residents now carry the period's MONTHLY entry (created,
    updated or already correct) and how many entries were deleted.
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

    # Only rows whose amount actually changes are rewritten. `created_by` records who booked *that*
    # amount; re-saving the schedule used to stamp the saving officer on every charge of the period,
    # including ones the scheduled run wrote and the officer never changed.
    current = dict(existing.filter(resident_id__in=member_ids).values_list("resident_id", "delta"))
    written = 0
    for resident_id in member_ids:
        if current.get(resident_id) != -charge.krydser:
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


def ensure_active_month_applied() -> None:
    """Book the current month's AK deduction if the scheduled task has not already done it.

    The `ak_monthly_assessment` cron job (DEPLOY.md §4b) normally gets there first; this is the
    backstop that makes a missed or misconfigured cron self-healing instead of silently leaving every
    resident's balance wrong. Called from hot internal pages (the dashboard and the AK pages) and
    cheap there — two indexed single-row lookups, measured — because it only does real work when the
    active period has advanced past what was last applied. Failures are logged, not raised, so a page
    load is never broken by this; the marker is only advanced on success, so the next request retries.
    """
    year, month = active_period()
    state = AkAutoApply.get()
    if (state.year, state.month) == (year, month):
        return
    try:
        ensure_schedule_row(month)
        apply_monthly_charge(year, month)
    except Exception:
        logger.exception("AK auto-apply failed for %s-%02d", year, month)
        return
    state.year, state.month = year, month
    state.save(update_fields=["year", "month"])
