"""Statistics (F-012) — members-only (the legacy JSON feeders were unauthenticated). The "this year"
figures are correctly year-scoped (legacy bug used all-time)."""

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from admissions.api import application_stats
from stats.models import DailyVisitCount


@login_required
def stats(request: HttpRequest) -> HttpResponse:
    now = timezone.localtime()
    apps = application_stats(now.year)
    visits = list(DailyVisitCount.objects.order_by("-date")[:30])[::-1]  # chronological for the graph
    tours, sublets = apps.tours, apps.sublets
    charts = {
        "applications": {"labels": ["Rundvisninger", "Fremleje"], "data": [tours, sublets]},
        "heard": {
            "labels": [answer for answer, _ in apps.heard_about_us],
            "data": [count for _, count in apps.heard_about_us],
        },
        "visits": {
            "labels": [v.date.isoformat() for v in visits],
            "data": [v.count for v in visits],
        },
    }
    return render(
        request,
        "statistik/stats.html",
        {
            "year": now.year,
            "tours": tours,
            "sublets": sublets,
            "universities": [{"university": u, "c": c} for u, c in apps.universities],
            "charts": charts,
        },
    )
