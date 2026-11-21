"""The site-wide search: the top bar's suggestions as you type, and the results page behind Enter.

Both ask every source in search.sources and draw what comes back; neither knows a feature's rules.
"""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

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


def _sections(request: HttpRequest, query: Query, limit: int) -> list[Section]:
    found = (source(request, query, limit) for source in SOURCES)
    return [section for section in found if section is not None and section.hits]


def _guard(request: HttpRequest) -> None:
    # The kitchen account's middleware gate already keeps it out (its allowlist has no /search/); this
    # says so here too, since a search for it would have to filter every source by its own rules.
    if is_kitchen(request):
        raise PermissionDenied


@login_required
def results(request: HttpRequest) -> HttpResponse:
    _guard(request)
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
    _guard(request)
    query = _query(request)
    sections = _sections(request, query, SUGGESTIONS_PER_SECTION) if query else []
    return render(request, "search/_suggestions.html", {"query": query, "sections": sections})
