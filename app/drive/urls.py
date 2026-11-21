from django.urls import path

from . import views, views_items, views_locks

app_name = "drive"

urlpatterns = [
    path("", views.home, name="home"),
    path("folder/<uuid:uuid>/", views.folder, name="folder"),
    path("folder/<uuid:uuid>/new-folder", views.create_folder, name="create_folder"),
    path("folder/<uuid:uuid>/rename", views_items.rename_folder, name="rename_folder"),
    path("folder/<uuid:uuid>/move", views_items.move, name="move"),
    path("folder/<uuid:uuid>/delete", views_items.delete, name="delete"),
    path("folder/<uuid:uuid>/bin", views_items.bin_page, name="bin"),
    path("folder/<uuid:uuid>/history", views_items.folder_history, name="folder_history"),
    path("folder/<uuid:uuid>/upload/begin", views.upload_begin, name="upload_begin"),
    path("folder/<uuid:uuid>/upload/direct", views.upload_direct, name="upload_direct"),
    path("upload/sign", views.upload_sign, name="upload_sign"),
    path("upload/commit", views.upload_commit, name="upload_commit"),
    path("file/<uuid:uuid>/", views.file_page, name="file"),
    path("file/<uuid:uuid>/download", views.download, name="download"),
    path("file/<uuid:uuid>/rename", views_items.rename_file, name="rename_file"),
    path("file/<uuid:uuid>/restore", views_items.restore, name="restore"),
    path("file/<uuid:uuid>/version/<int:number>/download", views.download, name="download_version"),
    path(
        "file/<uuid:uuid>/version/<int:number>/restore", views_items.restore_version, name="restore_version"
    ),
    path("file/<uuid:uuid>/lock", views_locks.lock_file, name="lock_file"),
    path("file/<uuid:uuid>/unlock-request", views_locks.unlock_file, name="unlock_file"),
    path("file/<uuid:uuid>/purge-request", views_locks.purge_request, name="purge_request"),
    path("folder/<uuid:uuid>/lock", views_locks.lock_folder, name="lock_folder"),
    path("folder/<uuid:uuid>/unlock-request", views_locks.unlock_folder, name="unlock_folder"),
    path("folder/<uuid:uuid>/activity", views_locks.activity, name="activity"),
    path("requests/", views_locks.requests_page, name="requests"),
    path("requests/<uuid:uuid>/decide", views_locks.decide, name="decide"),
]
