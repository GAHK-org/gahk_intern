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

    @classmethod
    def get(cls) -> "DevClock":
        return cls.objects.get_or_create(pk=1)[0]


class PushSubscription(models.Model):
    """One browser/device that has opted in to Web Push notifications.

    Lives in core, not in a feature app, because a browser has exactly **one** push endpoint per
    service-worker registration — so this row is the *device*, not the feature. Two features now
    notify through it (Den Hurtige and opslagstavlen), and per-topic consent is therefore a column
    here rather than a second table keyed on the same endpoint: two rows on one natural key would
    mean two upsert paths that can disagree about who owns the device.

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

    # Per-topic consent. BOTH default to False, and that is the important part: consent to one
    # feature's notifications is never consent to another's, so a device subscribing to the
    # noticeboard must not come out of it also wanting Den Hurtige's urgent buzz. A True default
    # here would do exactly that to every new subscriber, silently.
    #
    # Rows that predate topics are opted into Den Hurtige by an explicit data migration
    # (core/migrations/0005) rather than by this default — they subscribed when it was the only
    # topic, and dropping them would look like push breaking.
    #
    # The subscribe view writes exactly ONE of these per request; writing both would clear the topic
    # the resident did not just ask about.
    wants_den_hurtige = models.BooleanField(default=False, verbose_name="Den Hurtige")
    wants_opslagstavle = models.BooleanField(default=False, verbose_name="Opslagstavlen")
    # No data migration opting existing rows in, unlike 0005 above. That one was right because those
    # rows had subscribed when Den Hurtige was the only topic, so dropping them would have looked
    # like push breaking. Nobody has ever consented to event notifications, and consent granted by
    # migration is not consent.
    wants_begivenheder = models.BooleanField(default=False, verbose_name="Begivenheder")
    wants_reparationer = models.BooleanField(default=False, verbose_name="Reparationer")

    class Meta:
        verbose_name = "Push-abonnement"
        verbose_name_plural = "Push-abonnementer"

    def __str__(self) -> str:
        return f"Push-abonnement for {self.user}"

    def as_subscription_info(self) -> dict[str, object]:
        """The shape pywebpush expects — mirrors the browser's `PushSubscription.toJSON()`."""
        return {"endpoint": self.endpoint, "keys": {"auth": self.auth, "p256dh": self.p256dh}}


# Topic -> the consent column above. A dict rather than hardcoded branches so adding a third topic
# is one AddField and one entry, and so core.push.subscribers cannot be called with a topic nobody
# declared: a KeyError beats silently notifying everyone. Lives here, with the columns it names, so
# core.forms can validate against it without importing core.push (which imports core.forms).
TOPIC_FIELDS = {
    "den_hurtige": "wants_den_hurtige",
    "opslagstavle": "wants_opslagstavle",
    "begivenheder": "wants_begivenheder",
    "reparationer": "wants_reparationer",
}
