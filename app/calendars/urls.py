"""/calendar/: the resident's calendar subscription. English paths (docs: technical names), mounted
from config.urls BEFORE the intranet so the feed never sits behind a gate added to it later — a
calendar app does not report a failed refresh, it just stops updating.

/calendar/ itself is left free for a calendar view of one's own. The feeds have a prefix of their
own, so a page under /calendar/ can never collide with a token, and plumbing rules (the kitchen
account's allowlist) can name them by prefix."""

from django.urls import path

from . import views

app_name = "calendars"

urlpatterns = [
    path("settings", views.settings_page, name="settings"),
    path("settings/new-link", views.rotate_token, name="rotate_token"),
    path("feed/<str:token>.ics", views.calendar_feed, name="feed"),
]
