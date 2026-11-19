from django.urls import path

from . import views, views_items, views_locks

app_name = "drive"

urlpatterns = [
    path("", views.home, name="home"),
    path("folder/<int:pk>/", views.folder, name="folder"),
    path("folder/<int:pk>/new-folder", views.create_folder, name="create_folder"),
    path("folder/<int:pk>/rename", views_items.rename_folder, name="rename_folder"),
    path("folder/<int:pk>/move", views_items.move, name="move"),
    path("folder/<int:pk>/delete", views_items.delete, name="delete"),
    path("folder/<int:pk>/bin", views_items.bin_page, name="bin"),
    path("folder/<int:pk>/history", views_items.folder_history, name="folder_history"),
    path("folder/<int:pk>/upload/begin", views.upload_begin, name="upload_begin"),
    path("folder/<int:pk>/upload/direct", views.upload_direct, name="upload_direct"),
    path("upload/sign", views.upload_sign, name="upload_sign"),
    path("upload/commit", views.upload_commit, name="upload_commit"),
    path("file/<int:pk>/", views.file_page, name="file"),
    path("file/<int:pk>/download", views.download, name="download"),
    path("file/<int:pk>/rename", views_items.rename_file, name="rename_file"),
    path("file/<int:pk>/restore", views_items.restore, name="restore"),
    path("file/<int:pk>/version/<int:number>/download", views.download, name="download_version"),
    path("file/<int:pk>/version/<int:number>/restore", views_items.restore_version, name="restore_version"),
    path("file/<int:pk>/lock", views_locks.lock_file, name="lock_file"),
    path("file/<int:pk>/unlock-request", views_locks.unlock_file, name="unlock_file"),
    path("file/<int:pk>/purge-request", views_locks.purge_request, name="purge_request"),
    path("folder/<int:pk>/lock", views_locks.lock_folder, name="lock_folder"),
    path("folder/<int:pk>/unlock-request", views_locks.unlock_folder, name="unlock_folder"),
    path("folder/<int:pk>/activity", views_locks.activity, name="activity"),
    path("requests/", views_locks.requests_page, name="requests"),
    path("requests/<int:pk>/decide", views_locks.decide, name="decide"),
]
