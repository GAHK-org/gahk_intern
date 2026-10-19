"""Container healthchecks that need Django: `manage.py healthcheck beat` (docker-compose.prod.yml)."""

import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

# Beat writes `last_run_at` back every three minutes (celery.beat.Scheduler.sync_every), and the
# heartbeat is due every minute, so this is well clear of an honest gap.
BEAT_DISPATCH_STALE_AFTER = datetime.timedelta(minutes=10)


class Command(BaseCommand):
    help = "Exit non-zero when a background service is unhealthy."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("service", choices=["beat"])

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        # Beat itself, not the worker: when beat SENDS the heartbeat it records last_run_at, whether
        # or not a worker ever runs it. The status page's "beat" heartbeat covers the whole chain.
        task = PeriodicTask.objects.filter(name="heartbeat").first()
        if task is None or task.last_run_at is None:
            raise CommandError("beat has not sent the heartbeat task yet")
        age = timezone.now() - task.last_run_at
        if age > BEAT_DISPATCH_STALE_AFTER:
            raise CommandError(f"beat last sent the heartbeat {age} ago")
        self.stdout.write(f"beat ok ({int(age.total_seconds())}s)")
