"""A film's release date: how much of it is known, how it is written, and how it is guessed.

A date is known to the day, to the month or only to the year, so nobody has to make up the rest.
It is stored as the first day of what is known (Video.released_on) with its precision, and it
travels as ISO 8601 cut to that precision — "2015", "2015-03", "2015-03-12" — wherever it is a
value: the edit form, a proposal, the history.

THE GUESS, made when a file is processed (`guess`): a date somebody wrote — in the file's name, or
in one of its date tags — beats the camera's clock when the two disagree. An exported or
re-encoded film carries the day it was exported, while "Revy 2009.mp4" says what it is. The file's
last-modified time comes last: it is as often the day the file was copied.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from django.db import models
from django.utils import timezone
from django.utils.formats import date_format

FIRST_YEAR = 1900


class DatePrecision(models.TextChoices):
    DAY = "day", "Dag"
    MONTH = "month", "Måned"
    YEAR = "year", "År"


class DateSource(models.TextChoices):
    MANUAL = "manual", "Sat i hånden"
    NAME = "name", "Filnavnet"
    METADATA = "metadata", "Videofilens metadata"
    MODIFIED = "modified", "Filens ændringsdato"


@dataclass(frozen=True)
class PartialDate:
    start: date  # the first day of what is known
    precision: str  # a DatePrecision

    @classmethod
    def of(cls, year: int, month: int | None = None, day: int | None = None) -> "PartialDate":
        """Raises ValueError for a date that does not exist."""
        if month is None:
            return cls(date(year, 1, 1), DatePrecision.YEAR)
        if day is None:
            return cls(date(year, month, 1), DatePrecision.MONTH)
        return cls(date(year, month, day), DatePrecision.DAY)

    @classmethod
    def parse(cls, value: str | None) -> "PartialDate | None":
        """ "2015", "2015-03" or "2015-03-12"; None for anything else."""
        match = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", value or "")
        if match is None:
            return None
        year, month, day = (int(part) if part else None for part in match.groups())
        try:
            return cls.of(year, month, day)  # type: ignore[arg-type]
        except ValueError:
            return None

    @property
    def year(self) -> int:
        return self.start.year

    @property
    def month(self) -> int | None:
        return None if self.precision == DatePrecision.YEAR else self.start.month

    @property
    def day(self) -> int | None:
        return self.start.day if self.precision == DatePrecision.DAY else None

    def iso(self) -> str:
        if self.precision == DatePrecision.YEAR:
            return f"{self.start.year:04d}"
        if self.precision == DatePrecision.MONTH:
            return f"{self.start.year:04d}-{self.start.month:02d}"
        return self.start.isoformat()

    def label(self) -> str:
        """ "12. marts 2015", "marts 2015" or "2015"."""
        if self.precision == DatePrecision.YEAR:
            return str(self.start.year)
        return date_format(self.start, "F Y" if self.precision == DatePrecision.MONTH else "j. F Y")

    def contains(self, day: date) -> bool:
        if self.precision == DatePrecision.YEAR:
            return day.year == self.start.year
        if self.precision == DatePrecision.MONTH:
            return (day.year, day.month) == (self.start.year, self.start.month)
        return day == self.start


def label(value: str | None) -> str:
    """A date as a proposal or the history stores it, for people to read."""
    parsed = PartialDate.parse(value)
    return parsed.label() if parsed else ""


def _plausible(year: int, month: int | None = None, day: int | None = None) -> PartialDate | None:
    if not FIRST_YEAR <= year <= timezone.localdate().year:
        return None
    try:
        return PartialDate.of(year, month, day)
    except ValueError:
        return None


_YEAR = r"(?P<y>(?:19|20)\d{2})"
# 20150312, 2015-03-12, VID_20150312_183045, "2015-03-12 18.30.45" (Dropbox's camera uploads).
_NAME_ISO = re.compile(rf"(?<!\d){_YEAR}[-_.]?(?P<m>0[1-9]|1[0-2])[-_.]?(?P<d>0[1-9]|[12]\d|3[01])(?!\d)")
# 12-03-2015, 12.3.2015: day first, the Danish way.
_NAME_DANISH = re.compile(rf"(?<!\d)(?P<d>0?[1-9]|[12]\d|3[01])[-.](?P<m>0?[1-9]|1[0-2])[-.]{_YEAR}(?!\d)")
# "Revy 2009", "Julefrokost (2014)": a year on its own. Not after "_" or a letter, so a camera's
# file counter (IMG_1999, DSC_2010, MVI2015) is not taken for one.
_NAME_YEAR = re.compile(rf"(?:^|(?<=[\s(\[.-])){_YEAR}(?=$|[\s)\]._,-])")


def date_in_name(name: str) -> PartialDate | None:
    """A date written in a file's name: a whole date, else a year on its own."""
    stem = Path(name).stem
    for pattern in (_NAME_ISO, _NAME_DANISH):
        for match in pattern.finditer(stem):
            if found := _plausible(int(match["y"]), int(match["m"]), int(match["d"])):
                return found
    for match in _NAME_YEAR.finditer(stem):
        if found := _plausible(int(match["y"])):
            return found
    return None


# The tags that say when a film is from, the most telling first. "date" is what ffmpeg calls an
# MP4's ©day and an AVI's ICRD; the others are Matroska's.
DATE_TAGS = ("date_released", "date", "year", "date_recorded")
_TAG_PREFIX = re.compile(r"(?P<y>\d{4})(?:[-:/.](?P<m>\d{1,2})(?:[-:/.](?P<d>\d{1,2}))?)?")
_TAG_YEAR = re.compile(rf"(?<!\d){_YEAR}(?!\d)")


def date_in_tag(value: str) -> PartialDate | None:
    """ "2015", "2015-03-12T18:30:00Z", "2009:05:12 14:30:00", "Tue May 12 14:30:00 2009"."""
    if match := _TAG_PREFIX.match(value.strip()):
        year = int(match["y"])
        month = int(match["m"]) if match["m"] else None
        day = int(match["d"]) if match["d"] else None
        # A day or a month that does not exist is dropped, not the whole date.
        return (
            _plausible(year, month, day) or (_plausible(year, month) if month else None) or _plausible(year)
        )
    if match := _TAG_YEAR.search(value):
        return _plausible(int(match["y"]))
    return None


def date_in_tags(tags: dict[str, str]) -> PartialDate | None:
    for name in DATE_TAGS:
        if name in tags and (found := date_in_tag(tags[name])):
            return found
    return None


@dataclass(frozen=True)
class Guess:
    date: PartialDate
    source: str  # a DateSource


def guess(
    *, name: str, recorded_at: datetime | None, tags: dict[str, str], modified_at: datetime | None
) -> Guess | None:
    """The best guess at a film's release date from its file, or None.

    1. A whole date somebody wrote: in the name, else in a tag.
    2. The camera's clock, when every date somebody wrote agrees with it (or there is none).
    3. The more precise of the dates somebody wrote, the name's on a tie.
    4. The file's last-modified time.
    """
    written = [
        (found, source)
        for found, source in (
            (date_in_name(name), DateSource.NAME),
            (date_in_tags(tags), DateSource.METADATA),
        )
        if found is not None
    ]
    for found, source in written:
        if found.precision == DatePrecision.DAY:
            return Guess(found, source)
    if recorded_at is not None:
        clock = timezone.localdate(recorded_at)
        if all(found.contains(clock) for found, _source in written):
            return Guess(PartialDate(clock, DatePrecision.DAY), DateSource.METADATA)
    if written:
        months = [pair for pair in written if pair[0].precision == DatePrecision.MONTH]
        found, source = (months or written)[0]
        return Guess(found, source)
    if modified_at is not None:
        return Guess(PartialDate(timezone.localdate(modified_at), DatePrecision.DAY), DateSource.MODIFIED)
    return None
