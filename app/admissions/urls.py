"""Admissions URLs under /optagelse/ — legacy paths preserved (F-001)."""

from django.urls import path

from . import views

app_name = "admissions"

urlpatterns = [
    path("", views.index, name="index"),
    path("ansoeg", views.ansoeg, name="ansoeg"),
    path("send_rundvisning", views.ansoeg, name="send_rundvisning"),  # POST target (legacy URL)
    path("fremlej", views.fremlej, name="fremlej"),
    path("send_fremleje", views.fremlej, name="send_fremleje"),  # POST target (legacy URL)
    path("ansoeg/success", views.success, name="success"),
    path("listansoegninger", views.list_applications, name="list"),
    path("eksport", views.export_applications, name="export"),
    path("showAnsoegning/<int:pk>", views.show_application, name="show"),
    path("setasreceived/<int:pk>", views.mark_received, name="setasreceived"),
    path("kasser/<int:pk>", views.toggle_discarded, name="toggle_discarded"),
]
