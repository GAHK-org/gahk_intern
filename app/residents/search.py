"""Finding residents on a month's alumneliste by what people type: the list's own search box and the
site-wide search (search.sources) ask the same question here, so both find the same people.

Every word must match the resident, each in any of: first or last name, study, e-mail, embedsgruppe,
or the room number. So "anders hansen" finds Anders Hansen and "medicin" every medicine student. A
number is matched against the room exactly ("42" and "042" are room 42), never as a prefix, since a
single digit would match half the house; it still matches digits in an e-mail address as text.
"""

from django.db.models import Q, QuerySet

from .models import Residency

MAX_WORDS = 6


def words(query: str) -> list[str]:
    return (query or "").split()[:MAX_WORDS]


def matching(qs: QuerySet[Residency], query: str) -> QuerySet[Residency]:
    """`qs` narrowed to the residencies matching every word of `query` (unchanged for a blank one)."""
    for word in words(query):
        match = (
            Q(resident__first_name__icontains=word)
            | Q(resident__last_name__icontains=word)
            | Q(resident__study__icontains=word)
            | Q(resident__email__icontains=word)
            | Q(workgroup__name__icontains=word)
        )
        if word.isdigit():
            match |= Q(room__number=int(word))
        qs = qs.filter(match)
    return qs
