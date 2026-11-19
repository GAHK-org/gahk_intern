from django.urls import path

from . import views

app_name = "workgroups"

urlpatterns = [
    path("", views.mine, name="mine"),
    path("<int:pk>/", views.detail, name="detail"),
]
