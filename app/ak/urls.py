from django.urls import path

from . import views

app_name = "ak"

urlpatterns = [
    path("", views.my_ak, name="index"),
    path("tilfoej", views.add_self_entry, name="add_self"),
    path("admin", views.overview, name="overview"),
    path("admin/gem-maaneder", views.save_monthly_charges, name="save_months"),
    path("log/<int:pk>", views.resident_log, name="log"),
    path("log/<int:pk>/add", views.add_entry, name="add"),
    path("log/<int:pk>/delete/<int:entry_id>", views.delete_entry, name="delete"),
]
