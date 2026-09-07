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
    path("billede", views.upload_image, name="upload_image"),
    path("abonner", views.save_subscription, name="save_subscription"),
    path("kommentar/<int:pk>/slet", views.delete_comment, name="delete_comment"),
    path("<int:pk>", views.detail, name="detail"),
    path("<int:pk>/rediger", views.edit, name="edit"),
    path("<int:pk>/slet", views.delete, name="delete"),
    path("<int:pk>/fastgoer", views.toggle_pin, name="toggle_pin"),
    path("<int:pk>/kommentar", views.create_comment, name="create_comment"),
    path("<int:pk>/reaktion", views.toggle_reaction, name="toggle_reaction"),
]
