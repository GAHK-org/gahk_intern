"""Search and the period filter on the board.

Postgres does the searching, not a separate search server: the board is a few thousand posts a
year, and Elasticsearch would be a second service to run, back up and keep in sync for no gain.

Every word of the query must match the post, in any of three ways:

  * full text, stemmed in Danish and as a prefix, so "fødselsdage" finds "fødselsdag" and
    "værelses" finds "værelsesrunden";
  * a plain substring of the body, which catches the middle of Danish compounds ("fest" in
    "julefest") that stemming cannot;
  * the author's name, so "anders" finds what Anders wrote.

Migration 0006 indexes the first two (GIN on the tsvector and a trigram GIN on the body), which
keeps a search at about a millisecond on 50,000 posts instead of a second.
"""

import html
import re
from datetime import timedelta
from typing import TYPE_CHECKING

from django.contrib.postgres.search import SearchQuery, SearchVector
from django.db import connection
from django.db.models import Min, Q
from django.utils import timezone

from core.markdown import plain_text
from residents.models import Resident

from .models import NoticeQuerySet

if TYPE_CHECKING:
    from .models import Notice

# Must match the expression migration 0006 indexes, or Postgres cannot use the index.
CONFIG = "danish"

MAX_TERMS = 6
# Shorter words match nearly every post and cannot use the trigram index.
MIN_TERM = 2

# Rolling periods, then one entry per year with posts.
PERIODS = {"uge": ("Seneste uge", 7), "maaned": ("Seneste måned", 31), "aar": ("Seneste år", 365)}


def terms(query: str) -> list[str]:
    return [word for word in query.split() if len(word) >= MIN_TERM][:MAX_TERMS]


def _prefix_query(term: str) -> SearchQuery | None:
    """`værelses:*`, built from the word's letters only so no input is a tsquery syntax error."""
    lexemes = re.findall(r"\w+", term)
    if not lexemes:
        return None
    return SearchQuery(" & ".join(f"{lexeme}:*" for lexeme in lexemes), config=CONFIG, search_type="raw")


def search(qs: NoticeQuerySet, query: str) -> NoticeQuerySet:
    words = terms(query)
    if not words:
        return qs
    postgres = connection.vendor == "postgresql"
    if postgres:
        qs = qs.annotate(search_vector=SearchVector("body", config=CONFIG))
    for word in words:
        match = Q(body__icontains=word)
        # A list rather than a join: an OR across a join stops Postgres combining the two indexes.
        authors = list(
            Resident.objects.filter(Q(first_name__icontains=word) | Q(last_name__icontains=word)).values_list(
                "pk", flat=True
            )[:200]
        )
        if authors:
            match |= Q(author_id__in=authors)
        if postgres and (prefix := _prefix_query(word)) is not None:
            match |= Q(search_vector=prefix)
        qs = qs.filter(match)
    return qs


def period_choices(qs: NoticeQuerySet) -> list[tuple[str, str]]:
    """The rolling periods, then every year from the oldest post to this one, newest first."""
    choices = [(key, label) for key, (label, _) in PERIODS.items()]
    oldest = qs.aggregate(oldest=Min("created_at"))["oldest"]
    this_year = timezone.localdate().year
    first_year = timezone.localtime(oldest).year if oldest else this_year
    choices += [(str(year), str(year)) for year in range(this_year, first_year - 1, -1)]
    return choices


def in_period(qs: NoticeQuerySet, period: str) -> NoticeQuerySet:
    """`period` is a key of PERIODS or a year; anything else (already checked against
    period_choices by the view) leaves the queryset alone."""
    if period in PERIODS:
        return qs.filter(created_at__gte=timezone.now() - timedelta(days=PERIODS[period][1]))
    if period.isdigit():
        return qs.filter(created_at__year=int(period))
    return qs


# --- highlighting --------------------------------------------------------------------------------

# Cards show about the first 40 words; a match past this many words gets a snippet of its own.
EXCERPT_WORDS = 30
SNIPPET_BEFORE = 8
SNIPPET_AFTER = 14


def highlight_terms(query: str) -> list[str]:
    """What the page marks: each search word, plus its Danish stem where Postgres has one, so a
    search for "fødselsdage" also marks "fødselsdag" in "fødselsdagen"."""
    words = [lexeme.lower() for term in terms(query) for lexeme in re.findall(r"\w+", term)]
    found = list(dict.fromkeys(words))
    if words and connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT unnest(ts_lexize('danish_stem', w)) FROM unnest(%s::text[]) AS w", [words])
            found += [stem for (stem,) in cursor.fetchall() if len(stem) >= MIN_TERM]
    # Longest first, so the page marks "fødselsdage" before trying its stem inside it.
    return sorted(dict.fromkeys(found), key=len, reverse=True)


def snippet(body: str, marks: list[str]) -> str:
    """The words around the first match, when that match is past what the card's excerpt shows;
    otherwise "" (the excerpt already shows it, or only the author's name matched)."""
    words = html.unescape(plain_text(body)).split()
    lowered = [word.lower() for word in words]
    hit = next((i for i, word in enumerate(lowered) if any(mark in word for mark in marks)), None)
    if hit is None or hit < EXCERPT_WORDS:
        return ""
    start, end = max(hit - SNIPPET_BEFORE, 0), hit + SNIPPET_AFTER
    return f"… {' '.join(words[start:end])}{' …' if end < len(words) else ''}"


def attach_snippets(notices: list["Notice"], marks: list[str]) -> None:
    for notice in notices:
        notice.match_snippet = snippet(notice.body, marks) if marks else ""  # type: ignore[attr-defined]
