"""Kollegiets mærkedage: days the calendar marks every year, whoever lives here.

Like residents.birthdays, a note on the day rather than an Event: nobody answers ja to a founder's
birthday, it is never aflyst and it must not be purged a day later. Constants in code, because the
list changes about once a century.
"""

import datetime
from dataclasses import dataclass


@dataclass(frozen=True)
class Maerkedag:
    month: int
    day: int
    title: str
    note: str = ""


# G.A. Hagemanns Kollegium was founded in 1908 by Gustav Adolf and Mathilde Hagemann.
MAERKEDAGE: tuple[Maerkedag, ...] = (
    Maerkedag(5, 16, "Gustav Adolf Hagemanns fødselsdag", "Kollegiets stifter · 16. maj 1842"),
    Maerkedag(9, 27, "Mathilde Hagemanns fødselsdag", "Kollegiets stifter · 27. september 1846"),
)


def in_span(first: datetime.date, last: datetime.date) -> dict[datetime.date, list[Maerkedag]]:
    """Every mærkedag between `first` and `last` inclusive, keyed by the day it lands on.

    Per year in the span, since a grid padded from December into January covers two."""
    found: dict[datetime.date, list[Maerkedag]] = {}
    for year in range(first.year, last.year + 1):
        for maerkedag in MAERKEDAGE:
            day = datetime.date(year, maerkedag.month, maerkedag.day)
            if first <= day <= last:
                found.setdefault(day, []).append(maerkedag)
    return found
