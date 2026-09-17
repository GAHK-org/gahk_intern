"""Public-site admin (F-002), mounted at /admin/ (legacy URL). Django's own admin is at /django-admin/."""

from django.urls import path

from . import views_admin

app_name = "siteadmin"

urlpatterns = [
    path("", views_admin.home, name="home"),
    path("worker-jobs", views_admin.worker_jobs, name="worker_jobs"),
    path("roles", views_admin.roles, name="roles"),
    path("preview", views_admin.preview, name="preview"),
    path("preview/set", views_admin.preview_set, name="preview_set"),
    path("dev-clock/set", views_admin.dev_clock_set, name="dev_clock_set"),  # DEBUG-only
]
