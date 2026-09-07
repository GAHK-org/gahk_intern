"""Who has a birthday when.

A birthday is NOT an Event, and deliberately so. Nobody organises it, nobody answers ja or nej to
it, it cannot be aflyst, it has no capacity, and it must never reach a subscriber's phone as a push
or a VEVENT. It is a fact about a date that the calendar happens to be able to show — a note
printed on the day, the way a paper calendar prints one. Writing Event rows for sixty residents
every year would have handed all of that machinery something to do, and every bit of it would have
been wrong.

That is also why this lives in `residents` rather than in `events`. It answers a question about
PEOPLE, and the month grid is only its first caller; a dashboard strip of "fødselsdage i denne uge"
would use the same function without going anywhere near begivenheder.

WHOSE BIRTHDAYS: WHOEVER IS ON THE ACTIVE ALUMNELISTE, and nobody else. Not every Resident row —
that table holds every alumne the ETL has ever imported, so an unfiltered query would put several
hundred names a year on a calendar belonging to the sixty people who live here now.

"The active alumneliste" is not a paraphrase, it is the same query: residents.views.directory
defaults its period to `active_period()` and lists the Residency rows for it, which is exactly what
`residents_with_birthdays` filters on. Somebody who has moved out keeps their rows on the months
they lived here — the alumneliste's period picker still shows them there — and is off the calendar
the moment the list rolls over, with no separate flag to keep in step. A test asserts the two
against each other rather than against a hand-written idea of either.

NO NEW DISCLOSURE. The full date of birth is already on every resident's profile page and in the
alumneliste, both of which any logged-in resident can read. This shows the day, and the age it
makes, to exactly that audience.
"""

import datetime
from dataclasses import dataclass

from .models import Resident, active_period

# Above this, the birth year is a legacy placeholder rather than a fact. "Fylder 126" on the
# kollegium's calendar is worse than saying nothing about the age at all.
MAX_BELIEVABLE_AGE = 120


@dataclass(frozen=True)
class Birthday:
    """One person's birthday, ON A PARTICULAR DAY of a particular year.

    `day` is the cell it belongs in, which is not `resident.birthday` — that is the date of birth,
    forty years and one leap-day rule away. See `celebrated_on`.
    """

    resident: Resident
    day: datetime.date
    turning: int | None  # age reached that day, or None when the birth year is not usable

    @property
    def label(self) -> str:
        """ "Mette Hansen fylder 23" — or just the fact, when the year is missing."""
        if self.turning is None:
            return f"{self.resident.full_name} har fødselsdag"
        return f"{self.resident.full_name} fylder {self.turning}"


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def celebrated_on(born: datetime.date, year: int) -> datetime.date:
    """Which day of `year` a date of birth falls on.

    29 FEBRUARY IS THE WHOLE REASON THIS IS A FUNCTION. Three years in four it names no day at all,
    and `date(year, 2, 29)` raises rather than telling you so — so the alternative to deciding here
    is dropping somebody off the calendar for three years running and never noticing. Danish
    practice is to celebrate on 1 March, which is the answer this gives.
    """
    if born.month == 2 and born.day == 29 and not _is_leap(year):
        return datetime.date(year, 3, 1)
    return datetime.date(year, born.month, born.day)


def _turning(born: datetime.date, day: datetime.date) -> int | None:
    age = day.year - born.year
    return age if 0 < age < MAX_BELIEVABLE_AGE else None


def residents_with_birthdays() -> list[Resident]:
    """The people this module considers: the active alumneliste, in name order.

    The period comes from `active_period` — the newest published list that has already started —
    so this is the set the alumneliste shows when you open it without touching its period picker.
    Nothing here knows about moving in or out; being on the current month's list IS the condition.

    Name order is what makes a day with three birthdays render the same way twice, and it is the
    order the days below inherit for free by appending as they go.
    """
    year, month = active_period()
    return list(
        Resident.objects.filter(residencies__year=year, residencies__month=month, birthday__isnull=False)
        .distinct()
        .order_by("first_name", "last_name", "pk")
    )


def in_span(first: datetime.date, last: datetime.date) -> dict[datetime.date, list[Birthday]]:
    """Every birthday falling between `first` and `last` inclusive, keyed by the day it lands on.

    Asked per YEAR IN THE SPAN rather than per date of birth, because a calendar grid's padding
    routinely runs from December into January: a span is not confined to one month, or even to one
    year, and the same date of birth can want a different leap-day answer at each end of it.

    One query however long the span; the rest is a couple of dates per resident.
    """
    years = range(first.year, last.year + 1)
    found: dict[datetime.date, list[Birthday]] = {}
    for resident in residents_with_birthdays():
        born = resident.birthday
        if born is None:  # excluded by the queryset; narrows the type for the checker
            continue
        for year in years:
            day = celebrated_on(born, year)
            if first <= day <= last:
                found.setdefault(day, []).append(Birthday(resident, day, _turning(born, day)))
    return found
