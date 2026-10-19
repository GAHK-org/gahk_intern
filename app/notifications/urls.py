from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.index, name="index"),
    path("seneste", views.dropdown, name="dropdown"),
    path("set", views.mark_all_seen, name="mark_all_seen"),
    path("indstillinger", views.preferences, name="preferences"),
    path("enhed", views.subscription, name="subscription"),
    path("<int:pk>/", views.open_item, name="open"),
    path("aabn/<uuid:batch>/", views.open_batch, name="open_batch"),
]
