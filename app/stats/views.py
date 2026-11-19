"""Statistics (F-012) — members-only (the legacy JSON feeders were unauthenticated). The "this year"
figures are correctly year-scoped (legacy bug used all-time). The visit figures are gahk.dk's, pulled
from its outbox (stats.services)."""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from admissions.models import Application
from stats.models import DailyVisitCount, PageViewDay
from stats.services import SITE_PATH

TOP_PAGES = 15


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
    visits = list(DailyVisitCount.objects.order_by("-date")[:30])[::-1]  # chronological for the graph
    since = timezone.localdate() - timedelta(days=29)
    top_pages = [
        {**row, "url": f"{settings.PUBLIC_SITE_URL.rstrip('/')}/{row['path']}"}
        for row in PageViewDay.objects.filter(date__gte=since)
        .exclude(path=SITE_PATH)
        .values("path")
        .annotate(total_views=Sum("views"), total_visitors=Sum("visitors"))
        .order_by("-total_views", "path")[:TOP_PAGES]
    ]
    tours = by_type.get(Application.Type.TOUR, 0)
    sublets = by_type.get(Application.Type.SUBLET, 0)
    charts = {
        "applications": {"labels": ["Rundvisninger", "Fremleje"], "data": [tours, sublets]},
        "heard": {
            "labels": [h["heard_about_us"] for h in heard_this_year],
            "data": [h["c"] for h in heard_this_year],
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
            "universities": by_university,
            "top_pages": top_pages,
            "charts": charts,
        },
    )
