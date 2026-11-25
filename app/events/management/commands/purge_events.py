"""Apply begivenheder's retention: delete what is done with, and forget old guest lists.

Scheduled nightly (DEPLOY.md §4b). Idempotent: it acts on what is already past its clock, so a
double run or a run after a week of downtime does the same thing as a run last night.

Three clocks, all in EventQuerySet (see events.models for why they differ):

  * an INVITE-ONLY event that was held is deleted a week after it ends;
  * a CANCELLED event is deleted thirty days after it was cancelled;
  * an OPEN event that was held is kept, but its RSVPs and invites are deleted five years after it
    ends, leaving only the number who were coming (Event.attendee_count).

**Also run lazily on page load** (events.views.index), which is Den Hurtige's idiom rather than
opslagstavlen's. A held invite-only event stays in every invitee's .ics until it is deleted, so a
cron that has silently stopped leaves last month's dinners sitting in people's calendars. The sweep
is a handful of rows, so traffic pays for it and this covers the quiet weeks.

Images go with the rows through the post_delete receiver on Event, which is a signal precisely
because a bulk queryset delete never calls Model.delete().
"""

import argparse

from django.core.management.base import BaseCommand

from core.clock import current_datetime
from events.models import ATTENDANCE_RETENTION, CANCELLED_GRACE, PRIVATE_RETENTION_AFTER_END, Event


class Command(BaseCommand):
    help = "Slet begivenheder og gamle deltagerlister der er ude over deres opbevaringstid."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        now = current_datetime()
        expired = Event.objects.expired(now)
        # Counted before the delete, and counted separately, because delete() returns the cascade
        # total — invitations and tilmeldinger included — which is not the number a reader of this
        # line wants.
        n_private = expired.filter(cancelled_at__isnull=True).count()
        n_cancelled = expired.filter(cancelled_at__isnull=False).count()

        if opts["dry_run"]:
            n_lists = Event.objects.attendance_expired(now).count()
            self.stdout.write(
                f"[dry-run] ville slette {n_private} afholdt(e) privat(e) begivenhed(er) fra før "
                f"{now - PRIVATE_RETENTION_AFTER_END:%Y-%m-%d %H:%M}, {n_cancelled} aflyst(e) fra før "
                f"{now - CANCELLED_GRACE:%Y-%m-%d} og deltagerlisten på {n_lists} begivenhed(er) "
                f"fra før {now - ATTENDANCE_RETENTION:%Y-%m-%d}."
            )
            return

        expired.delete()
        n_lists = Event.objects.forget_attendance(now)
        self.stdout.write(
            self.style.SUCCESS(
                f"Begivenheder: {n_private} afholdt(e) privat(e) og {n_cancelled} aflyst(e) slettet; "
                f"deltagerlisten slettet på {n_lists}."
            )
        )
