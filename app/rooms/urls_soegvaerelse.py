from django.urls import path

from . import views_soegvaerelse as views

app_name = "soegvaerelse"

urlpatterns = [
    path("", views.soeg, name="soeg"),
    path("mine", views.my, name="my"),
    path("kvotient", views.kvotient, name="kvotient"),
    path("ansoegning/<int:pk>", views.detail, name="detail"),
    path("ansoegning/<int:pk>/slet", views.delete_application, name="delete_application"),
    path("admin", views.admin, name="admin"),
    path("admin/opret-tilbud", views.create_offer, name="create_offer"),
    path("admin/afslut-runde", views.end_round, name="end_round"),
    path("admin/tilbud/<int:offer_id>/ansoegere", views.applicants, name="applicants"),
    path("admin/tilbud/<int:offer_id>/luk", views.close_offer, name="close_offer"),
    path("admin/tilbud/<int:offer_id>/afgoer", views.resolve_tie, name="resolve_tie"),
]
