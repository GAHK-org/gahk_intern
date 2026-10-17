"""Mounted under /intern/regnskab/ by residents.urls, which is the /intern/ hub. No namespace: the URL
name `regnskab` predates this app, when the view lived in residents."""

from django.urls import path

from . import views

urlpatterns = [path("", views.overview, name="regnskab")]
