from django.urls import path

from . import views

app_name = "search"

urlpatterns = [
    path("", views.results, name="results"),
    path("suggestions", views.suggestions, name="suggestions"),
]
