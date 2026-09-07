"""Danish, mostly slash-less paths, matching opslagstavle/urls.py and den_hurtige/urls.py.

Mounted at /intern/begivenheder/ from residents/urls.py — which has no `app_name`, so its names are
global while these are namespaced. Always `{% url 'events:detail' event.pk %}`.

Note the ROOT /begivenheder/ (no /intern/) is the public CMS page and has nothing to do with this;
see the models docstring on the three-way name collision.
"""

from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    path("", views.index, name="index"),
    path("opret", views.create, name="create"),
    # Before <int:pk>, so these fixed segments are never read as an event id.
    path("abonner", views.save_subscription, name="save_subscription"),
    path("kalender", views.calendar, name="calendar"),
    path("kalender/abonnement", views.feed_settings, name="feed_settings"),
    path("kalender/nyt-link", views.rotate_token, name="rotate_token"),
    # Keyed on the COMMENT's pk, not the event's, so it belongs with the fixed segments up here
    # rather than nested under <int:pk> (mirrors reparationer/urls.py).
    path("kommentar/<int:pk>/slet", views.delete_comment, name="delete_comment"),
    path("<int:pk>", views.detail, name="detail"),
    path("<int:pk>/rediger", views.edit, name="edit"),
    path("<int:pk>/slet", views.delete, name="delete"),
    path("<int:pk>/aflys", views.cancel, name="cancel"),
    path("<int:pk>/svar", views.answer, name="answer"),
    path("<int:pk>/kommentar", views.create_comment, name="create_comment"),
    path("<int:pk>/ics", views.event_ics, name="event_ics"),
]
