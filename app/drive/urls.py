from django.urls import path

from . import views

app_name = "drive"

urlpatterns = [
    path("", views.home, name="home"),
    path("folder/<uuid:uuid>/", views.folder, name="folder"),
    path("folder/<uuid:uuid>/new-folder", views.create_folder, name="create_folder"),
    path("folder/<uuid:uuid>/upload/begin", views.upload_begin, name="upload_begin"),
    path("folder/<uuid:uuid>/upload/direct", views.upload_direct, name="upload_direct"),
    path("upload/sign", views.upload_sign, name="upload_sign"),
    path("upload/commit", views.upload_commit, name="upload_commit"),
    path("file/<uuid:uuid>/download", views.download, name="download"),
]
