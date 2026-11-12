"""Admissions review under /optagelse/ on intern.gahk.dk — legacy paths preserved (F-001).

The public pages and forms that used to be here (/optagelse/, /optagelse/ansoeg, /optagelse/fremlej
and their POST targets) are gahk.dk's own now; gahk.dk forwards these review paths to intern.
"""

from django.urls import path
from django.views.generic import RedirectView

from . import views

app_name = "admissions"

urlpatterns = [
    # Somebody typing intern.gahk.dk/optagelse lands on the list (which checks the role).
    path("", RedirectView.as_view(pattern_name="admissions:list"), name="index"),
    path("listansoegninger", views.list_applications, name="list"),
    path("eksport", views.export_applications, name="export"),
    path("showAnsoegning/<int:pk>", views.show_application, name="show"),
    path("setasreceived/<int:pk>", views.mark_received, name="setasreceived"),
    path("kasser/<int:pk>", views.toggle_discarded, name="toggle_discarded"),
]
