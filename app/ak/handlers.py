"""What AK does when other domains publish events. Imported once, from AkConfig.ready()."""

from core.domain_events import subscribe
from residents.api import MemberAreaVisited

from .services import ensure_previous_month_charged


@subscribe(MemberAreaVisited)
def book_monthly_charge_backstop(event: MemberAreaVisited) -> None:
    """The backstop for the ak_monthly_assessment cron (DEPLOY.md §4b), on the first visit of a month.

    Used to be a direct call from residents.views.dashboard — which made identity depend on AK.
    Now identity says what happened and AK decides what that means. Idempotent and cheap: a no-op
    unless a month has ended since the last charge (see services).
    """
    ensure_previous_month_charged()
