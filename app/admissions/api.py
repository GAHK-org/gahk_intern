"""Admissions' public interface — the only module other domains may import (enforced by Tach).

Applications hold personal data from people who are not residents, so nothing here hands out an
Application: only the aggregate figures the statistics page draws.
"""

from dataclasses import dataclass

from django.db.models import Count

from .models import Application

__all__ = ["ApplicationStats", "application_stats"]


@dataclass(frozen=True)
class ApplicationStats:
    tours: int  # all-time
    sublets: int  # all-time
    heard_about_us: list[tuple[str, int]]  # tours this year, by answer, most common first
    universities: list[tuple[str, int]]  # tours this year, top 15


def application_stats(year: int) -> ApplicationStats:
    """All-time counts by type, plus the `year`-scoped breakdowns of tours (the legacy got the year
    scoping wrong and used all-time)."""
    by_type = dict(Application.objects.values_list("type").annotate(c=Count("id")).values_list("type", "c"))
    tours_this_year = Application.objects.filter(type=Application.Type.TOUR, submitted_at__year=year)
    heard = (
        tours_this_year.exclude(heard_about_us="")
        .values("heard_about_us")
        .annotate(c=Count("id"))
        .order_by("-c")
        .values_list("heard_about_us", "c")
    )
    universities = (
        tours_this_year.exclude(university="")
        .values("university")
        .annotate(c=Count("id"))
        .order_by("-c")
        .values_list("university", "c")
    )[:15]
    return ApplicationStats(
        tours=by_type.get(Application.Type.TOUR, 0),
        sublets=by_type.get(Application.Type.SUBLET, 0),
        heard_about_us=list(heard),
        universities=list(universities),
    )
