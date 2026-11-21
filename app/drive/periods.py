"""The half-year periods group folders are filed by, and the calendar years Fælles is filed by.

A period runs from 1 October to 31 March (`Vinter`) or from 1 April to 30 September (`Sommer`). Its
folder is named year first, and a winter carries both years, so names sort by date anywhere they
are plain text - in a zip, on a laptop: `2026 Sommer` < `2026-27 Vinter` < `2027 Sommer`.

The next period's folder, and the next year's, appear LEAD before they begin, so a group can set up
its successor's folder and the incoming members can start before the first day.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta

LEAD = timedelta(days=14)

SUMMER_MONTH = 4  # April
WINTER_MONTH = 10  # October


@dataclass(frozen=True, order=True)
class Period:
    start: date  # 1 April or 1 October

    @property
    def is_summer(self) -> bool:
        return self.start.month == SUMMER_MONTH

    @property
    def name(self) -> str:
        year = self.start.year
        return f"{year} Sommer" if self.is_summer else f"{year}-{(year + 1) % 100:02d} Vinter"

    def next(self) -> "Period":
        if self.is_summer:
            return Period(date(self.start.year, WINTER_MONTH, 1))
        return Period(date(self.start.year + 1, SUMMER_MONTH, 1))

    def previous(self) -> "Period":
        if self.is_summer:
            return Period(date(self.start.year - 1, WINTER_MONTH, 1))
        return Period(date(self.start.year, SUMMER_MONTH, 1))

    def months(self) -> Iterator[tuple[int, int]]:
        """The six (year, month) pairs of the period, as Residency rows are keyed."""
        year, month = self.start.year, self.start.month
        for _ in range(6):
            yield year, month
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def period_containing(day: date) -> Period:
    if SUMMER_MONTH <= day.month < WINTER_MONTH:
        return Period(date(day.year, SUMMER_MONTH, 1))
    if day.month >= WINTER_MONTH:
        return Period(date(day.year, WINTER_MONTH, 1))
    return Period(date(day.year - 1, WINTER_MONTH, 1))


def periods_with_folders(day: date) -> list[Period]:
    """The periods that have a folder on `day`: the current one, and the next once it is LEAD away."""
    current = period_containing(day)
    following = current.next()
    return [current, following] if day >= following.start - LEAD else [current]


def years_with_folders(day: date) -> list[int]:
    """The calendar years Fælles has a folder for on `day`: this one, and the next from mid-December."""
    following = date(day.year + 1, 1, 1)
    return [day.year, day.year + 1] if day >= following - LEAD else [day.year]
