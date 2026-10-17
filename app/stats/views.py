"""Statistics (F-012) — members-only (the legacy JSON feeders were unauthenticated). The "this year"
figures are correctly year-scoped (legacy bug used all-time)."""

from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from admissions.models import Application
from stats.models import DailyVisitCount

VISIT_CHART_DAYS = 30


def _visit_series(today: date) -> tuple[list[str], list[int]]:
    """The last VISIT_CHART_DAYS calendar days ending today, oldest first, with 0 for days that have no
    row. A day without visits has no DailyVisitCount row at all, so taking the last 30 *rows* stretched
    the chart over however long it took to collect 30 visited days and drew straight past the gaps."""
    days = [today - timedelta(days=n) for n in range(VISIT_CHART_DAYS - 1, -1, -1)]
    counts = dict(
        DailyVisitCount.objects.filter(date__gte=days[0], date__lte=today).values_list("date", "count")
    )
    return [d.isoformat() for d in days], [counts.get(d, 0) for d in days]


@login_required
def stats(request: HttpRequest) -> HttpResponse:
    now = timezone.localtime()
    by_type = dict(Application.objects.values_list("type").annotate(c=Count("id")).values_list("type", "c"))
    # this-year "heard about us" for tours — year-scoped (the donut the legacy got wrong)
    heard_this_year = list(
        Application.objects.filter(type=Application.Type.TOUR, submitted_at__year=now.year)
        .exclude(heard_about_us="")
        .values("heard_about_us")
        .annotate(c=Count("id"))
        .order_by("-c")
    )
    by_university = list(
        Application.objects.filter(type=Application.Type.TOUR, submitted_at__year=now.year)
        .exclude(university="")
        .values("university")
        .annotate(c=Count("id"))
        .order_by("-c")[:15]
    )
    visit_labels, visit_data = _visit_series(now.date())
    tours = by_type.get(Application.Type.TOUR, 0)
    sublets = by_type.get(Application.Type.SUBLET, 0)
    charts = {
        "applications": {"labels": ["Rundvisninger", "Fremleje"], "data": [tours, sublets]},
        "heard": {
            "labels": [h["heard_about_us"] for h in heard_this_year],
            "data": [h["c"] for h in heard_this_year],
        },
        "visits": {"labels": visit_labels, "data": visit_data},
    }
    return render(
        request,
        "statistik/stats.html",
        {
            "year": now.year,
            "tours": tours,
            "sublets": sublets,
            "universities": by_university,
            "charts": charts,
            # The series is always 30 entries now (zero-filled), so "is there anything to draw" has to
            # look at the counts rather than at whether the list is empty.
            "has_visits": any(visit_data),
        },
    )
