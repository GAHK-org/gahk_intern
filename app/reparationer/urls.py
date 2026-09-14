"""Mounted at /intern/reparationer/ from residents/urls.py — global names, not namespaced there,
but this module has its own `app_name` so callers always use `{% url 'reparationer:board' %}`."""

from django.urls import path

from . import views

app_name = "reparationer"

urlpatterns = [
    path("", views.board, name="board"),
    path("opret", views.create, name="create"),
    path("arkiv", views.archive_list, name="archive_list"),
    path("abonner", views.save_subscription, name="save_subscription"),
    # Before <int:pk>, so this fixed segment is never read as a task id (mirrors opslagstavle/urls.py).
    path("kommentar/<int:pk>/slet", views.delete_comment, name="delete_comment"),
    path("<int:pk>", views.detail, name="detail"),
    path("<int:pk>/status", views.set_status, name="set_status"),
    path("<int:pk>/ansvarlig", views.set_responsible, name="set_responsible"),
    path("<int:pk>/arkiver", views.archive_now, name="archive_now"),
    path("<int:pk>/genaabn", views.unarchive, name="unarchive"),
    path("<int:pk>/slet", views.delete, name="delete"),
    path("<int:pk>/kommentar", views.create_comment, name="create_comment"),
]
