"""Public-site admin (F-002), mounted at /admin/ (legacy URL). Django's own admin is at /django-admin/."""

from django.urls import path

from notifications import views_admin as notifications_admin

from . import views_admin

app_name = "siteadmin"

urlpatterns = [
    path("", views_admin.home, name="home"),
    path("worker-jobs", views_admin.worker_jobs, name="worker_jobs"),
    path("worker-jobs/<str:task_id>", views_admin.worker_job_detail, name="worker_job_detail"),
    path("roles", views_admin.roles, name="roles"),
    path("notifikationer", notifications_admin.notifications, name="notifications"),
    path(
        "notifikationer/enhed/<int:pk>/annuller",
        notifications_admin.cancel_device,
        name="notification_device_cancel",
    ),
    path("adminadgang", views_admin.admin_access_view, name="admin_access"),
    path("preview", views_admin.preview, name="preview"),
    path("preview/set", views_admin.preview_set, name="preview_set"),
    path("dev-clock/set", views_admin.dev_clock_set, name="dev_clock_set"),  # DEBUG-only
]
