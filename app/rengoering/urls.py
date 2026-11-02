"""Mounted at /intern/rengoering/ from residents/urls.py."""

from django.urls import path

from . import views

app_name = "rengoering"

urlpatterns = [
    path("", views.mine, name="mine"),
    path("vagt/<int:pk>", views.assignment, name="assignment"),
    path("vagt/<int:pk>/taget", views.sign_off, name="sign_off"),
    path("vagt/<int:pk>/udbedret", views.mark_fixed, name="mark_fixed"),
    path("vagt/<int:pk>/byt", views.swap_request, name="swap_request"),
    path("bytte/<int:pk>", views.swap_answer, name="swap_answer"),
    path("tjek/", views.inspect_index, name="inspect_index"),
    # Before tjek/<day>, so the fixed segment is never read as a date (mirrors reparationer/urls.py).
    path("tjek/vagt/<int:pk>", views.inspect_save, name="inspect_save"),
    path("tjek/<str:day>", views.inspect, name="inspect"),
    path("oversigt/", views.overview, name="overview"),
    path("oversigt/<str:day>", views.overview, name="overview_day"),
    path("historik/", views.history, name="history"),
    path("historik/omraade/<slug:slug>", views.area_history, name="area_history"),
    path("historik/beboer/<int:pk>", views.resident_history, name="resident_history"),
    path("lister/", views.rotas, name="rotas"),
    path("lister/opret", views.rota_create, name="rota_create"),
    path("lister/importer", views.rota_import, name="rota_import"),
    path("lister/<int:pk>", views.rota_detail, name="rota_detail"),
    path("lister/<int:pk>/erstat", views.rota_replace, name="rota_replace"),
    path("lister/<int:pk>/slet", views.rota_delete, name="rota_delete"),
    path("vagt/<int:pk>/rediger", views.edit_assignment, name="edit_assignment"),
]
