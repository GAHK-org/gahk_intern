from django.urls import path

from . import views

app_name = "photo_album"

urlpatterns = [
    path("", views.index, name="index"),
    path("tidslinje", views.timeline, name="timeline"),
    # Every album's bin together, for administrators. Each album has its own at <pk>/papirkurv.
    path("papirkurv", views.bin, name="bin"),
    path("opret", views.create_album, name="create_album"),
    path("importer-zip", views.import_zip, name="import_zip"),
    path("importer-zip/upload", views.import_zip_upload, name="import_zip_upload"),
    path("upload/underskriv", views.direct_upload_sign, name="direct_upload_sign"),
    path("importer-zip/system", views.system_import_zip, name="system_import_zip"),
    path("importer-zip/system/albummer", views.system_albums, name="system_albums"),
    path("importer-zip/system/upload", views.system_import_upload, name="system_import_upload"),
    path("importer-zip/<uuid:token>/status", views.import_zip_status, name="import_zip_status"),
    path("downloads", views.downloads, name="downloads"),
    path("downloads/<uuid:token>", views.download_album_archive, name="download_album_archive"),
    path("markering", views.selection, name="selection"),
    path("tags", views.tag_search, name="tag_search"),
    path("<int:pk>", views.detail, name="detail"),
    path("<int:pk>/download", views.download_album, name="download_album"),
    path("<int:pk>/papirkurv", views.album_bin, name="album_bin"),
    path("<int:pk>/omdoeb", views.rename_album, name="rename_album"),
    path("<int:pk>/nyt-underalbum", views.create_sub_album, name="create_sub_album"),
    path("<int:pk>/slet", views.delete_album, name="delete_album"),
    path("<int:pk>/laas", views.lock_album, name="lock_album"),
    path("<int:pk>/laas-op", views.unlock_album, name="unlock_album"),
    path("<int:pk>/upload", views.upload, name="upload"),
    path("<int:pk>/upload/start", views.upload_begin, name="upload_begin"),
    path("<int:pk>/upload/gem", views.upload_commit, name="upload_commit"),
    path("<int:pk>/tags", views.album_tags, name="album_tags"),
    # Everything the viewer needs about one item, fetched when it is opened rather than rendered
    # into every tile of the grid. See views.media_detail.
    path("media/<int:pk>/detaljer", views.media_detail, name="media_detail"),
    path("media/<int:pk>/visning", views.record_view, name="record_view"),
    path("media/<int:pk>/tags", views.media_tags, name="media_tags"),
    path("media/<int:pk>/original", views.download_original, name="download_original"),
    path("media/<int:pk>/godkend", views.approve, name="approve"),
    path("media/<int:pk>/afvis", views.reject, name="reject"),
    path("media/<int:pk>/slet", views.delete, name="delete"),
    path("media/<int:pk>/gendan", views.restore, name="restore"),
    path("media/<int:pk>/slet-permanent", views.permanently_delete, name="permanently_delete"),
]
