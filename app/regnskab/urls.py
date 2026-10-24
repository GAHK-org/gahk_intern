from django.urls import path

from . import views

app_name = "regnskab"

urlpatterns = [
    path("", views.overview, name="overview"),
    path(
        "<int:pk>/economic-nummer",
        views.set_economic_account_number,
        name="set_economic_account_number",
    ),
    path(
        "alumneregninger/forhaandsvisning",
        views.preview_alumneregninger_view,
        name="preview_alumneregninger",
    ),
    path(
        "alumneregninger/<int:pk>/pdf",
        views.alumneregning_statement_pdf_view,
        name="alumneregning_statement_pdf",
    ),
    path("alumneregninger/send", views.send_alumneregninger_view, name="send_alumneregninger"),
    path("alumneregninger/status", views.alumneregninger_status_view, name="alumneregninger_status"),
    path(
        "alumneregninger/indstillinger",
        views.alumneregning_settings_view,
        name="alumneregning_settings",
    ),
]
