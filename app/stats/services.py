"""Visit counts from gahk.dk's outbox (GET /api/outbox "visits"), stored as day totals.

gahk.dk sends, for each day it has not been told to forget, the TOTALS so far per page and for the
whole site (path "*"): today's row grows between pulls. So a row is overwritten, never added to,
and pulling the same outbox twice changes nothing.

Untrusted input, like the applications next to it: a row of the wrong shape is skipped and logged.
"""

import logging
import re
from datetime import date, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from .models import DailyVisitCount, PageViewDay

logger = logging.getLogger(__name__)

SITE_PATH = "*"
PATH_RE = re.compile(r"^[a-z0-9/_.-]{0,200}$")
MAX_COUNT = 10**9


def _row(item: object, today: date) -> tuple[date, str, int, int] | None:
    if not isinstance(item, dict):
        return None
    day_text, path = item.get("day"), item.get("path")
    views, visitors = item.get("views"), item.get("visitors")
    if not isinstance(day_text, str) or not isinstance(path, str):
        return None
    try:
        day = date.fromisoformat(day_text)
    except ValueError:
        return None
    # A day after tomorrow is a broken clock on one side or the other; nothing to count.
    if day > today + timedelta(days=1):
        return None
    path = path.strip("/")
    if path != SITE_PATH and not PATH_RE.match(path):
        return None
    counts = (views, visitors)
    if not all(isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= MAX_COUNT for n in counts):
        return None
    return day, path, int(views), int(visitors)  # type: ignore[arg-type]  # checked just above


def ingest_outbox_visits(items: list[Any]) -> date | None:
    """Store the visit rows; return the last COMPLETE day among them (before today), for the ack.

    gahk.dk deletes acknowledged days except today, which is still counting and will be sent again.
    """
    today = timezone.localdate()
    rows = []
    for item in items:
        row = _row(item, today)
        if row is None:
            logger.warning("gahk.dk outbox: a malformed visit row was skipped (%r)", item)
            continue
        rows.append(row)
    with transaction.atomic():
        for day, path, views, visitors in rows:
            PageViewDay.objects.update_or_create(
                date=day, path=path, defaults={"views": views, "visitors": visitors}
            )
            if path == SITE_PATH:
                DailyVisitCount.objects.update_or_create(date=day, defaults={"count": visitors})
    complete = [day for day, *_rest in rows if day < today]
    return max(complete) if complete else None
