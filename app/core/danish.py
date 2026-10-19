"""Danish month names, in one place.

Django's `|date` filter localises perfectly well with LANGUAGE_CODE="da", and every template that
only needs a formatted date should keep using it. This exists for the cases that build a label in
PYTHON — "September 2026" on the calendar header, "marts 2024" in the alumneliste, the kvotient
month index — where there is no template filter to reach for.

It was copy-pasted twice before this: residents.views.DA_MONTHS (1-indexed, with a leading "") and
rooms.kvotient._DA_MONTHS (0-indexed, no blank). Two subtly different conventions for the same
twelve strings is exactly the drift worth removing, and the calendar in `events` was about to be the
third. One-indexed won because that is what `datetime.month` gives you.
"""

# Index 0 is deliberately empty so MONTHS[date.month] works without an off-by-one at every call.
MONTHS = (
    "",
    "januar",
    "februar",
    "marts",
    "april",
    "maj",
    "juni",
    "juli",
    "august",
    "september",
    "oktober",
    "november",
    "december",
)

# Monday first, matching how a Danish calendar is printed and how datetime.weekday() counts.
WEEKDAYS_SHORT = ("man", "tir", "ons", "tor", "fre", "lør", "søn")


def month_label(year: int, month: int) -> str:
    """ "September 2026" — capitalised, as a heading rather than as prose."""
    return f"{MONTHS[month].capitalize()} {year}"
