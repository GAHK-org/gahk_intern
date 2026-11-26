"""Mounted at /facilities/ from residents/urls.py, namespaced `facilities:`. English paths (technical
names are English); the pages themselves are Danish."""

from django.urls import path

from . import views

app_name = "facilities"

urlpatterns = [
    path("", views.index, name="index"),
    path("mine/", views.mine, name="mine"),
    path("requests/", views.requests, name="requests"),
    path("new/", views.create_facility, name="create"),
    path("bookings/<int:pk>/", views.booking, name="booking"),
    path("bookings/<int:pk>/cancel/", views.cancel, name="cancel"),
    path("bookings/<int:pk>/approve/", views.approve, name="approve"),
    path("bookings/<int:pk>/reject/", views.reject, name="reject"),
    path("<int:pk>/", views.facility, name="facility"),
    path("<int:pk>/book/", views.book, name="book"),
    path("<int:pk>/edit/", views.edit_facility, name="edit"),
    path("<int:pk>/history/", views.history, name="history"),
]
