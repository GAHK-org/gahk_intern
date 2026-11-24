"""Roles in tests: given by hand for a month (access.models.Grant), and what someone held in one."""

import datetime

from access.models import Grant
from residents.models import Resident, Role, active_period


def month_days(period: tuple[int, int]) -> tuple[datetime.date, datetime.date]:
    year, month = period
    first = datetime.date(year, month, 1)
    return first, datetime.date(year + month // 12, month % 12 + 1, 1) - datetime.timedelta(days=1)


def give(resident: Resident, role: str, period: tuple[int, int] | None = None) -> Grant:
    """`role` by hand for one month (the month in effect by default)."""
    first, last = month_days(period or active_period())
    return Grant.objects.create(resident=resident, role=role, reason="Test", starts_on=first, ends_on=last)


def held(resident: Resident, period: tuple[int, int] | None = None) -> set[str]:
    """The roles `resident` held in a month, from the list or by hand (Resident.has_role)."""
    return {role for role in Role.values if resident.has_role(role, period)}
