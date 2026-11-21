"""POST data for the forms' date-and-time fields (core.date_and_time), which post two boxes each."""

import datetime

from django.utils import timezone


def date_and_time(name: str, when: datetime.datetime | str) -> dict[str, str]:
    """`<name>_date` and `<name>_time` for a moment, or for a "YYYY-MM-DDTHH:MM" local time."""
    if isinstance(when, datetime.datetime):
        when = timezone.localtime(when).strftime("%Y-%m-%dT%H:%M")
    day, _, clock = when.partition("T")
    return {f"{name}_date": day, f"{name}_time": clock}
