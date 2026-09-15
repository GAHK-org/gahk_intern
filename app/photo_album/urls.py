from django.urls import path

from . import views

app_name = "photo_album"

urlpatterns = [
    path("", views.index, name="index"),
    path("opret", views.create_album, name="create_album"),
    path("<int:pk>", views.detail, name="detail"),
    path("<int:pk>/upload", views.upload, name="upload"),
    path("media/<int:pk>/godkend", views.approve, name="approve"),
    path("media/<int:pk>/afvis", views.reject, name="reject"),
    path("media/<int:pk>/slet", views.delete, name="delete"),
    path("media/<int:pk>/gendan", views.restore, name="restore"),
]
