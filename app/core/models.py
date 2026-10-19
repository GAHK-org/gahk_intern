"""Shared lookups (02-schema-etl.md §4).

Room is seeded from the hard-coded room map in legacy `intern/delt.php` (there is no rooms table);
Workgroup/Cleaning come from `intern_alumne_workgroup` / `intern_alumne_cleaning`.
"""

from django.conf import settings
from django.db import models


class Room(models.Model):
    # Both legacy identifiers coexist in the old code: the 0..61 index ("vaerelse_id", used by the
    # kvotient lottery) and the human room number ("003", "101", … stored as int).
    legacy_index = models.PositiveSmallIntegerField(unique=True)
    number = models.PositiveSmallIntegerField(unique=True)
    floor = models.CharField(max_length=20)  # "stuen", "1. sal", …
    side = models.CharField(max_length=20)  # "mod gaden" / "mod gården"
    note = models.CharField(max_length=40, blank=True)  # "(røvhullet)", "(fængslet)", …

    class Meta:
        ordering = ["number"]

    def __str__(self) -> str:
        return f"Værelse {self.number:03d}"

    @property
    def plan_image(self) -> str:
        """Static path of this floor's plan drawing, for the værelsestjek room picker (F-005).

        The five legacy PNGs were copied to app/static/legacy/image/intern/ and renamed to drop the
        spaces in "1. sal.png" — a filename with a space under CompressedManifestStaticFilesStorage
        is a hard 500 on a missing manifest entry, not a broken image.
        """
        stem = "stuen" if self.floor == "stuen" else f"sal{self.floor[0]}"
        return f"legacy/image/intern/{stem}.png"


class Workgroup(models.Model):  # intern_alumne_workgroup (the monthly chore/embedsgruppe label)
    legacy_id = models.PositiveIntegerField(unique=True, null=True, blank=True)
    name = models.CharField(max_length=100, unique=True)
    # Exact number of members a next-month list must give this group (legacy `w_amount`); 0 = no limit.
    size = models.PositiveSmallIntegerField(default=0)

    def __str__(self) -> str:
        return self.name


class Cleaning(models.Model):  # intern_alumne_cleaning
    legacy_id = models.PositiveIntegerField(unique=True, null=True, blank=True)
    name = models.CharField(max_length=100, unique=True)
    # Exact number of members a next-month list must give this group (legacy `c_amount`); 0 = no limit.
    size = models.PositiveSmallIntegerField(default=0)

    def __str__(self) -> str:
        return self.name


class DevClock(models.Model):
    """DEV-ONLY simulated "today", for local testing of month rollover (F-004 room rounds).

    Single row (pk=1). `core.clock.current_date()` honours `simulated_date` ONLY when settings.DEBUG,
    so this is inert in production — the row is never read there. The table ships (a migration creates
    it) but stays empty and untouched when DEBUG is off.
    """

    simulated_date = models.DateField(null=True, blank=True)  # None = use the real clock

    def save(self, *args: object, **kwargs: object) -> None:
        """Save, then drop core.clock's memo of this row.

        core.clock caches the simulated date for the length of a request, because Den Hurtige reads
        the clock per message and the row was otherwise a query per read. That cache has to be
        invalidated by the write as well as by the next request, or `dev_clock_set` — which saves
        and then renders — would answer from the value it has just replaced.

        An override rather than a post_save receiver so it sits on the model it belongs to; the
        cost is that a queryset `.update()` slips past it, which is true of a signal as well.
        """
        super().save(*args, **kwargs)  # type: ignore[arg-type]
        from .clock import clear_cache

        clear_cache()

    @classmethod
    def get(cls) -> "DevClock":
        return cls.objects.get_or_create(pk=1)[0]


class PushSubscription(models.Model):
    """One browser/device that has opted in to Web Push notifications.

    Lives in core, not in a feature app, because a browser has exactly **one** push endpoint per
    service-worker registration — so this row is the *device*, not the feature. Subscribing is done
    once per device (notifications.views.subscription); WHAT the device is told is the resident's
    notification settings, which are per person (notifications.models.NotificationPreference).

    It used to carry one consent column per feature (wants_den_hurtige, wants_opslagstavle, …), set
    from four separate subscribe buttons. That made a device's consent depend on which pages its
    owner had happened to visit, and the settings could not follow the person to their next phone.
    The columns were dropped in core/migrations/0011.

    Replaces django-webpush's three-table layout (SubscriptionInfo + PushInformation + Group). The
    Group indirection bought nothing — there was exactly one audience, everyone who opted in — and
    its get_or_create keyed on every field, so a browser that merely bumped its user-agent string
    quietly produced a duplicate row and a duplicate notification.

    `endpoint` is the device identity assigned by the push service, so it is the natural key: the
    subscribe view upserts on it, which keeps re-subscribing (or a second person logging in on the
    same browser) to a single row.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions"
    )
    endpoint = models.URLField(max_length=500, unique=True)
    # Encryption material from the browser's PushSubscription — opaque base64url, not secrets of
    # ours: without them the push service cannot decrypt anything we send to this device.
    auth = models.CharField(max_length=100)
    p256dh = models.CharField(max_length=100)
    user_agent = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # For the admin page: a device that has not accepted a push in months is the one to cancel.
    last_success_at = models.DateTimeField(null=True, blank=True)
    # Consecutive transient failures (404/410 delete the row outright). Reset by a success.
    failure_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Push-abonnement"
        verbose_name_plural = "Push-abonnementer"

    def __str__(self) -> str:
        return f"Push-abonnement for {self.user}"

    def as_subscription_info(self) -> dict[str, object]:
        """The shape pywebpush expects — mirrors the browser's `PushSubscription.toJSON()`."""
        return {"endpoint": self.endpoint, "keys": {"auth": self.auth, "p256dh": self.p256dh}}


class WorkerJobLog(models.Model):
    """What one Celery task logged while it ran — see core/worker_logs.py. Shown on the worker-jobs
    admin page. A successful job's is purged after WORKER_JOB_RETENTION by
    core.tasks.purge_worker_job_logs; any other only from a pile of the same task and state, past
    UNSUCCESSFUL_JOBS_KEPT and UNSUCCESSFUL_JOB_RETENTION."""

    task_id = models.CharField(max_length=255, unique=True)
    task_name = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=20, blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    # [{"at": ISO time, "level": "INFO", "logger": "photo_album.tasks", "message": "…"}, …]
    lines = models.JSONField(default=list)
    # Lines past core.worker_logs.MAX_LINES, counted but not kept.
    omitted = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=["finished_at"])]

    def __str__(self) -> str:
        return f"{self.task_name} {self.task_id}"


class PushDeliveryDay(models.Model):
    """Push outcomes per day, for the admin statistics.

    Counted in core.push._dispatch, the one place that sees every attempt. A counter rather than a
    row per attempt: a dorm-wide chat message is a hundred attempts, and nobody needs them one by
    one — they need to see that yesterday half of them failed.
    """

    date = models.DateField(unique=True)
    delivered = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    # Endpoints the push service reported gone (404/410), and which were deleted for it.
    removed = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Push-statistik (dag)"
        verbose_name_plural = "Push-statistik (dage)"
        ordering = ["-date"]


class ServiceHeartbeat(models.Model):
    """When a Celery process last said it was alive — see core/heartbeat.py. Read by /health.

    One row per process name: "beat", and each worker's node name before the "@" ("default",
    "media" — the -n in docker-compose.prod.yml), so a redeploy's new container id overwrites the
    old row instead of leaving a dead one behind."""

    name = models.CharField(max_length=100, unique=True)
    hostname = models.CharField(max_length=255, blank=True)
    beat_at = models.DateTimeField()

    def __str__(self) -> str:
        return f"{self.name} {self.beat_at:%Y-%m-%d %H:%M:%S}"
