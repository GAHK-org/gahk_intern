"""The site-wide search: the top bar's suggestions as you type, and the results page behind Enter.

Both ask every source in search.sources and draw what comes back; neither knows a feature's rules.
"""

from dataclasses import replace
from urllib.parse import urlsplit

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from core.middleware import kitchen_may_open
from opslagstavle.search import highlight_terms
from residents.permissions import is_kitchen

from .sources import SOURCES, Query, Section

# Shorter queries match nearly everything and say nothing.
MIN_LENGTH = 2
MAX_LENGTH = 200

SUGGESTIONS_PER_SECTION = 4
RESULTS_PER_SECTION = 10


def _query(request: HttpRequest) -> Query | None:
    text = " ".join((request.GET.get("q") or "").split())[:MAX_LENGTH]
    if len(text) < MIN_LENGTH:
        return None
    return Query(text=text, marks=highlight_terms(text))


def _for_kitchen(section: Section) -> Section:
    """`section` with only what the kitchen account may open (core.middleware.KitchenAccountGate):
    the sources answer for its roles, but the gate is what really keeps it to its few pages, so a
    hit past it (an alumne, an event, an album) would be a title it was never meant to see."""

    def may_open(url: str) -> bool:
        return kitchen_may_open(urlsplit(url).path)

    hits = [hit for hit in section.hits if not hit.external and may_open(hit.url)]
    more_url = section.more_url if may_open(section.more_url) else ""
    return replace(section, hits=hits, more_url=more_url)


def _sections(request: HttpRequest, query: Query, limit: int) -> list[Section]:
    found = [section for section in (source(request, query, limit) for source in SOURCES) if section]
    if is_kitchen(request):
        found = [_for_kitchen(section) for section in found]
    return [section for section in found if section.hits]


@login_required
def results(request: HttpRequest) -> HttpResponse:
    query = _query(request)
    sections = _sections(request, query, RESULTS_PER_SECTION) if query else []
    return render(
        request,
        "search/results.html",
        {
            "q": (request.GET.get("q") or "").strip()[:MAX_LENGTH],
            "query": query,
            "sections": sections,
            "too_short": query is None and bool((request.GET.get("q") or "").strip()),
            "min_length": MIN_LENGTH,
        },
    )


@login_required
def suggestions(request: HttpRequest) -> HttpResponse:
    """The top bar's dropdown (an htmx fragment). Empty for a query too short to search."""
    query = _query(request)
    sections = _sections(request, query, SUGGESTIONS_PER_SECTION) if query else []
    return render(request, "search/_suggestions.html", {"query": query, "sections": sections})
