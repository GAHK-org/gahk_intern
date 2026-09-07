"""Arkiv URLs, mounted under /intern/arkiv/ by residents.urls."""

from django.urls import path

from . import views

app_name = "arkiv"

urlpatterns = [
    path("", views.browse, name="root"),
    # pk rather than a slug path. A path would be prettier, but it would also have to resolve and
    # access-check every segment on the way down, and the archive's URLs are followed from the page
    # rather than typed or shared. Revisit if that stops being true.
    path("mappe/<int:pk>/", views.browse, name="folder"),
    path("fil/<int:pk>/hent", views.download, name="download"),
    path("fil/<int:pk>/miniature", views.thumbnail, name="thumbnail"),
    # Upload is three routes rather than one because the bytes do not come here in production - see
    # arkiv/uploads.py. `direkte` is the dev/CI path and refuses to run when a bucket is configured.
    path("mappe/<int:pk>/upload/start", views.upload_begin, name="upload_begin"),
    path("mappe/<int:pk>/upload/direkte", views.upload_direct, name="upload_direct"),
    path("mappe/<int:pk>/upload/faerdig", views.upload_commit, name="upload_commit"),
    path("mappe/<int:pk>/ny-mappe", views.folder_create, name="folder_create"),
    path("fil/<int:pk>/fjern", views.file_delete, name="file_delete"),
]
