"""The statistik page's visits chart."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from stats.models import DailyVisitCount


@pytest.mark.django_db
def test_visits_chart_covers_the_last_30_calendar_days_with_gaps_as_zero(make_resident: Callable) -> None:
    """It took the last 30 *rows*, and a day without visits has no row — so the chart spanned however
    long it took to collect 30 visited days and drew straight across the gaps."""
    today = timezone.localdate()
    DailyVisitCount.objects.create(date=today, count=5)
    DailyVisitCount.objects.create(date=today - timedelta(days=3), count=7)
    DailyVisitCount.objects.create(date=today - timedelta(days=45), count=99)  # outside the window
    c = Client()
    c.force_login(make_resident(email="stats@gahk.dk"))

    visits = c.get("/intern/statistik/").context["charts"]["visits"]

    assert visits["labels"] == [(today - timedelta(days=n)).isoformat() for n in range(29, -1, -1)]
    assert len(visits["data"]) == 30
    assert visits["data"][-1] == 5
    assert visits["data"][-4] == 7
    assert sum(visits["data"]) == 12  # every other day is a 0, and day -45 is not shown


@pytest.mark.django_db
def test_visits_chart_says_no_data_when_the_window_is_empty(make_resident: Callable) -> None:
    DailyVisitCount.objects.create(date=timezone.localdate() - timedelta(days=60), count=3)
    c = Client()
    c.force_login(make_resident(email="stats2@gahk.dk"))

    html = c.get("/intern/statistik/").content.decode()

    assert 'id="chart-visits"' not in html
    assert "Ingen besøgsdata endnu." in html
