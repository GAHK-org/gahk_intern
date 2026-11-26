from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.index, name="index"),
    path("seneste", views.dropdown, name="dropdown"),
    path("taeller", views.badge, name="badge"),  # the bell's badge alone, re-read on a live signal
    path("indstillinger", views.preferences, name="preferences"),
    path("enhed", views.subscription, name="subscription"),
    path("<int:pk>/", views.open_item, name="open"),
    path("<int:pk>/seen", views.mark_seen, name="mark_seen"),
    path("<int:pk>/done", views.mark_done, name="mark_done"),
    path("<int:pk>/not-done", views.mark_not_done, name="mark_not_done"),
    path("aabn/<uuid:batch>/", views.open_batch, name="open_batch"),
]
