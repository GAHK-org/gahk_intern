"""Who else has an editor open, so Hjemmeside can say so before two people overwrite each other.

The editor page reports in when it opens and then every half minute while it is open (cms.ts), and
says goodbye when it is closed. Somebody counts as present while their last report is younger than
FRESH. A closed laptop sends no goodbye, so it simply stops counting after that.

This is a warning, not a lock: nobody is ever kept out. What actually protects a save is the
version check in cms.services, which works whether or not anyone was seen here.

Arriving and leaving are signalled on the thing's live topic (cms.live), so the others' boxes update
at once; the half-minute reports themselves signal nothing.
"""

import datetime

from django.utils import timezone

from residents.models import Resident

from . import live
from .models import OpenEditor

# Long enough for a background tab, whose timers a browser may slow to once a minute.
FRESH = datetime.timedelta(minutes=2)

# Rows nobody has reported on for this long are deleted, so the table stays a handful of rows.
FORGET = datetime.timedelta(days=1)


def others(target: str, resident: Resident) -> list[OpenEditor]:
    """Everybody but `resident` who has `target` open now, longest-open first."""
    return list(
        OpenEditor.objects.filter(target=target, seen_at__gte=timezone.now() - FRESH)
        .exclude(resident=resident)
        .select_related("resident")
        .order_by("opened_at")
    )


def arrive(target: str, pk: int | None, resident: Resident) -> None:
    """`resident` has `target` open (first report or a later one)."""
    now = timezone.now()
    OpenEditor.objects.filter(seen_at__lt=now - FORGET).delete()
    row, created = OpenEditor.objects.get_or_create(
        target=target, resident=resident, defaults={"opened_at": now, "seen_at": now}
    )
    if created:
        live.changed(target, pk)
        return
    if row.seen_at < now - FRESH:
        # Back after counting as gone (a laptop opened again): to the others this is an arrival.
        row.opened_at = now
        live.changed(target, pk)
    row.seen_at = now
    row.save(update_fields=["opened_at", "seen_at"])


def leave(target: str, pk: int | None, resident: Resident) -> None:
    """`resident` closed `target`'s editor (the tab, or went elsewhere)."""
    deleted, _ = OpenEditor.objects.filter(target=target, resident=resident).delete()
    if deleted:
        live.changed(target, pk)
