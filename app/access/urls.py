from django.urls import path

from . import views

app_name = "access"

urlpatterns = [
    path("", views.index, name="index"),
    path("grants/<int:pk>/end", views.grant_end, name="grant_end"),
    path("kitchen-staff/add", views.kitchen_staff_add, name="kitchen_staff_add"),
]
