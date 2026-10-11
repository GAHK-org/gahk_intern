from django.urls import path

from . import views

app_name = "documents"

urlpatterns = [
    path("", views.index, name="index"),
    path("<uuid:document_id>/", views.editor, name="editor"),
    path("<uuid:document_id>/fullscreen/", views.editor_fullscreen, name="editor_fullscreen"),
    path("<uuid:document_id>/editor-config/", views.editor_config, name="editor_config"),
    path("<uuid:document_id>/callback/", views.callback, name="callback"),
    path("<uuid:document_id>/download/", views.download, name="download"),
    path("<uuid:document_id>/versions/", views.versions, name="versions"),
    path("<uuid:document_id>/versions/<uuid:version_id>/download/", views.download, name="version_download"),
    path("<uuid:document_id>/versions/<uuid:version_id>/restore/", views.restore, name="restore"),
    path("<uuid:document_id>/access/", views.grant_access, name="grant_access"),
    path("<uuid:document_id>/delete/", views.delete, name="delete"),
]
