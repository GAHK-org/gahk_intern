"""Mounted at /intern/rengoering/ from residents/urls.py."""

from django.urls import path

from . import views

app_name = "rengoering"

urlpatterns = [
    path("lister/", views.rotas, name="rotas"),
    path("lister/opret", views.rota_create, name="rota_create"),
    path("lister/<int:pk>", views.rota_detail, name="rota_detail"),
    path("lister/<int:pk>/erstat", views.rota_replace, name="rota_replace"),
    path("lister/<int:pk>/slet", views.rota_delete, name="rota_delete"),
    path("vagt/<int:pk>/rediger", views.edit_assignment, name="edit_assignment"),
]
