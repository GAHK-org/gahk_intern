"""Danish, mostly slash-less paths, matching den_hurtige/urls.py.

Mounted at /intern/opslagstavle/ from residents/urls.py — which has no `app_name`, so its names are
global while these are namespaced. Always `{% url 'opslagstavle:detail' notice.pk %}`.
"""

from django.urls import path

from . import views

app_name = "opslagstavle"

urlpatterns = [
    path("", views.board, name="board"),
    path("opret", views.create, name="create"),
    # Before <int:pk>, so these fixed segments are never read as a post id.
    path("forhaandsvisning", views.preview, name="preview"),
    # The composer's album and film: the block it draws them in, and the picker's two lists.
    path("vedhaeft", views.attachments_block, name="attachments"),
    path("vedhaeft/album", views.attach_choices, {"kind": "album"}, name="attach_albums"),
    path("vedhaeft/video", views.attach_choices, {"kind": "video"}, name="attach_videos"),
    path("billede", views.upload_image, name="upload_image"),
    path("kommentar/<int:pk>/slet", views.delete_comment, name="delete_comment"),
    # What a live signal re-fetches: fragments, never pages (see views.live_cards).
    path("kort", views.live_cards, name="live_cards"),
    path("<int:pk>", views.detail, name="detail"),
    path("<int:pk>/rediger", views.edit, name="edit"),
    path("<int:pk>/indhold", views.live_detail, name="live_detail"),
    path("<int:pk>/slet", views.delete, name="delete"),
    path("<int:pk>/fastgoer", views.toggle_pin, name="toggle_pin"),
    path("<int:pk>/kommentar", views.create_comment, name="create_comment"),
    path("<int:pk>/reaktion", views.toggle_reaction, name="toggle_reaction"),
]
