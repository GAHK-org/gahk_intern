"""Delete events that retention is done with, and their images with them.

Scheduled nightly (DEPLOY.md §4b). Idempotent: it deletes what is already past its clock, so a
double run or a run after a week of downtime does the same thing as a run last night.

Two clocks, both in EventQuerySet.expired: an event that was HELD goes a week after it ends, and
one that was CANCELLED goes thirty days after it was cancelled. See events.models for why they
differ.

**Also purged lazily on page load** (events.views.index), which is Den Hurtige's idiom rather than
opslagstavlen's. Nothing about a missed sweep is visible on the list — that shows only what is
coming up either way — but a held event stays in every subscriber's .ics until it is deleted, so a
cron that has silently stopped leaves last month's dinners sitting in people's calendars. The
sweep is a handful of rows, so traffic pays for it and this covers the quiet weeks.

Images go with the rows through the post_delete receiver on Event, which is a signal precisely
because a bulk queryset delete never calls Model.delete().
"""

import argparse

from django.core.management.base import BaseCommand

from core.clock import current_datetime
from events.models import CANCELLED_GRACE, RETENTION_AFTER_END, Event


class Command(BaseCommand):
    help = "Slet begivenheder der er ude over deres opbevaringstid (og deres billeder)."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        now = current_datetime()
        expired = Event.objects.expired(now)
        # Counted before the delete, and counted separately, because delete() returns the cascade
        # total — invitations and tilmeldinger included — which is not the number a reader of this
        # line wants.
        n_held = expired.filter(cancelled_at__isnull=True).count()
        n_cancelled = expired.filter(cancelled_at__isnull=False).count()

        if opts["dry_run"]:
            self.stdout.write(
                f"[dry-run] ville slette {n_held} afholdt(e) begivenhed(er) fra før "
                f"{now - RETENTION_AFTER_END:%Y-%m-%d %H:%M} og {n_cancelled} aflyst(e) fra før "
                f"{now - CANCELLED_GRACE:%Y-%m-%d}."
            )
            return

        expired.delete()
        self.stdout.write(
            self.style.SUCCESS(f"Begivenheder: {n_held} afholdt(e) og {n_cancelled} aflyst(e) slettet.")
        )
