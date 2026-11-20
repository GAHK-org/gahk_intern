"""/calendar/: the resident's calendar subscription. English paths (docs: technical names), mounted
from config.urls BEFORE the intranet so the feed never sits behind a gate added to it later — a
calendar app does not report a failed refresh, it just stops updating."""

from django.urls import path

from . import views

app_name = "calendar_feed"

urlpatterns = [
    path("", views.settings_page, name="settings"),
    path("new-link", views.rotate_token, name="rotate_token"),
    path("<str:token>.ics", views.calendar_feed, name="feed"),
]
