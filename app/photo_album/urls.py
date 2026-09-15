from django.urls import path

from . import views

app_name = "photo_album"

urlpatterns = [
    path("", views.index, name="index"),
    path("bin", views.bin, name="bin"),
    path("opret", views.create_album, name="create_album"),
    path("<int:pk>", views.detail, name="detail"),
    path("<int:pk>/slet", views.delete_album, name="delete_album"),
    path("<int:pk>/upload", views.upload, name="upload"),
    path("media/<int:pk>/original", views.download_original, name="download_original"),
    path("media/<int:pk>/godkend", views.approve, name="approve"),
    path("media/<int:pk>/afvis", views.reject, name="reject"),
    path("media/<int:pk>/slet", views.delete, name="delete"),
    path("media/<int:pk>/gendan", views.restore, name="restore"),
    path("media/<int:pk>/slet-permanent", views.permanently_delete, name="permanently_delete"),
]
