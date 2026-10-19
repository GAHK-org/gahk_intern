import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("gahk_intern")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Every worker reports it is alive, for its container healthcheck and the admin status page.
from core.celery_health import HeartbeatStep  # noqa: E402

app.steps["worker"].add(HeartbeatStep)
