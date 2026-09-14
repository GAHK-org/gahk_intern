"""Den Hurtige - short-lived urgent messages, replacing the kollegium's Messenger group.

Posts are deliberately ephemeral: they are relevant for the next 30-120 minutes and are then
*hard-deleted* (see QuickPostQuerySet.purge_expired), which is the whole point of the feature — a
thread that cannot accumulate off-topic history. Purging happens lazily on every feed load plus via
`manage.py purge_quick_posts` on a schedule (DEPLOY.md §4b), so a quiet week still drains the table.

Posts are filed into a *channel* (`QuickPost.channel`). The channels themselves are constants in
den_hurtige.channels, not rows here — see that module for why. Nothing in this file knows which
channels exist, and nothing that expires may become channel-aware: `active()`, `expired()` and
`purge_expired()` all stay deliberately channel-agnostic, so a post in a channel nobody has opened
for a week still dies on time.
"""

from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from core.files import delete_attached_files

# How long a post stays visible unless the author picks otherwise. Two døgn, raised from one after
# the feature had been live a while: a day sounds generous and is not, because the thing people
# actually want to find again is a message from *yesterday* -- a plan for tonight, a borrowed drill,
# a package in the porten. At 1440 those expired at exactly the moment they became relevant.
#
# It is still short enough to keep the promise the whole feature rests on: nothing here is a record,
# and anything worth keeping belongs on opslagstavlen or in ankebogen.
#
# Whatever this is set to MUST appear in DURATION_CHOICES -- it is what the composer's <select>
# preselects, and checks.E009 refuses to start if a channel defaults to a value the picker cannot
# offer.
DEFAULT_DURATION_MINUTES = 2880
DURATION_CHOICES = [
    (30, "30 min"),
    (60, "1 time"),
    (120, "2 timer"),
    (240, "4 timer"),
    (720, "12 timer"),
    (1440, "1 døgn"),
    (2880, "2 døgn"),
]

# The channel a post lands in when nothing says otherwise. A bare string, not an import from
# den_hurtige.channels: that module imports DURATION_CHOICES from here, and models.py must stay the
# leaf of that dependency. checks.py (E010) asserts the two agree, so they cannot drift apart.
DEFAULT_CHANNEL_SLUG = "generelt"

# One-tap reactions offered by the picker. Any emoji is still accepted (forms.ReactionForm) — this
# is only the shortlist, because in a desktop browser a bare text field gives you a cursor and no
# help, while these are a single click. Written as escapes so the file stays ASCII-safe; the heart
# carries U+FE0F because that is the form every mobile keyboard sends, and counts must not split.
QUICK_EMOJI = [
    "👍",
    "❤️",
    "😂",
    "🎉",
    "🙏",
    "👀",
    "🔥",
    "😮",
    "😢",
    "👎",
    "✅",
    "❌",
    "☕",
    "🍺",
    "🍕",
    "🎂",
    "🚲",
    "🔑",
]


def get_default_expiration() -> datetime:
    """Default expiration time is 60 minutes from creation."""
    return timezone.now() + timedelta(minutes=DEFAULT_DURATION_MINUTES)


class QuickPostQuerySet(models.QuerySet["QuickPost"]):
    """Custom QuerySet to handle filtering and permanent deletion of expired posts."""

    def active(self) -> "QuickPostQuerySet":
        """Returns posts that have not yet expired."""
        return self.filter(expires_at__gt=timezone.now())

    def expired(self) -> "QuickPostQuerySet":
        """Returns posts that have reached or passed their expiration time."""
        return self.filter(expires_at__lte=timezone.now())

    def purge_expired(self) -> int:
        """Permanently deletes all expired posts. Returns the number of *posts* removed (the
        `delete()` total also counts cascaded comments, which is not what callers report)."""
        _total, per_model = self.expired().delete()
        return per_model.get("den_hurtige.QuickPost", 0)


class QuickPost(models.Model):
    """A short-lived, urgent message in 'Den Hurtige'."""

    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="quick_posts")
    content = models.TextField(help_text="Selve beskeden.")

    # Optional image attachment. FileField, not ImageField: ImageField requires Pillow, which is not
    # a dependency of this project (same call as rooms.RoomConditionScore.photo). The view validates
    # content type and size instead.
    image = models.FileField(
        upload_to="quick_posts/%Y/%m/",
        max_length=255,
        blank=True,
        help_text="Valgfrit billede.",
    )

    # Which feed this belongs to. A slug into den_hurtige.channels, not a ForeignKey, because the
    # channel list is code rather than data. Deliberately no `choices=`: choices built from that
    # tuple would make every channel edit emit a no-op AlterField migration, and the value is
    # already validated where it enters (channels.lookup) and at startup (checks.E007-E010).
    channel = models.CharField(max_length=32, default=DEFAULT_CHANNEL_SLUG)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        default=get_default_expiration,
        help_text="Hvornår opslaget udløber og slettes permanent.",
    )

    objects = QuickPostQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Hurtigt opslag"
        verbose_name_plural = "Hurtige opslag"
        # active() and purge_expired() both filter on expires_at and run on every feed load.
        # The composite serves the feed itself, which is always one channel's live posts; the lone
        # expires_at index stays because purge_expired() sweeps every channel at once and would
        # otherwise have to scan the composite's leading column.
        indexes = [
            models.Index(fields=["expires_at"]),
            models.Index(fields=["channel", "expires_at"]),
        ]

    def __str__(self) -> str:
        return f"Opslag af {self.author} kl. {self.created_at:%H:%M}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def minutes_left(self) -> int:
        """Whole minutes until expiry, floored at 0 — the primitive expires_label is built from."""
        return max(0, int((self.expires_at - timezone.now()).total_seconds() // 60))

    @property
    def expires_label(self) -> str:
        """Time left, humanised: "1 døgn", "12 timer", "1 time", "45 min", "udløbet"."""
        minutes = round((self.expires_at - timezone.now()).total_seconds() / 60)
        if minutes <= 0:
            return "udløbet"
        if minutes < 60:
            return f"{minutes} min"
        hours = minutes // 60
        if hours < 24:
            return "1 time" if hours == 1 else f"{hours} timer"
        # ROUNDED to days, where the hours above are floored, and the difference is deliberate: the
        # coarser the unit, the more a floor hides. Flooring minutes into hours is wrong by at most
        # 59 minutes, which nobody acts on. Flooring hours into days is wrong by up to 23h59, and it
        # showed: 2878 minutes is 1.998 days, so a message posted with the 2-døgn default read
        # "1 døgn" two minutes later and stayed wrong for a day. Computed from `minutes` rather than
        # from the floored `hours` so the two roundings cannot compound.
        #
        # The cost is overstating by up to half a day in the middle of a bucket -- 36 hours reads
        # "2 døgn". That is the nearer of the two answers, which is the most a single word can do.
        days = round(minutes / (24 * 60))
        return "1 døgn" if days == 1 else f"{days} døgn"


class QuickComment(models.Model):
    """A comment on a QuickPost. Deleted with its post when the post expires."""

    post = models.ForeignKey(QuickPost, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="quick_comments"
    )
    # Blank when the reply IS the photo.
    content = models.TextField(blank=True)

    # Same FileField-not-ImageField call as QuickPost.image: ImageField needs Pillow, which is not a
    # dependency. The view validates content type and size.
    image = models.FileField(
        upload_to="quick_comments/%Y/%m/",
        max_length=255,
        blank=True,
        help_text="Valgfrit billede.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    # Determines notification scope: everyone subscribed, or only the original poster.
    notify_everyone = models.BooleanField(
        default=False,
        help_text="Sand: push til alle abonnenter. Falsk: kun til opslagets forfatter.",
    )

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Kommentar"
        verbose_name_plural = "Kommentarer"

    def __str__(self) -> str:
        return f"Kommentar af {self.author} på opslag #{self.post.pk}"


@receiver(post_delete, sender=QuickPost)
@receiver(post_delete, sender=QuickComment)
def _delete_image_file(sender: type[models.Model], instance: models.Model, **kwargs: Any) -> None:  # noqa: ANN401
    """Remove an attached file from storage when its message or reply goes.

    Without this the *text* expires on schedule while the *photo* stays on disk forever — the
    opposite of what the feature promises, and an unbounded pile of orphaned uploads. The reasons
    this is a signal rather than a `delete()` override (bulk purges and cascades never call it) are
    documented in core.files, which also does the work.
    """
    delete_attached_files(instance)


class QuickReaction(models.Model):
    """One emoji one resident put on one message.

    The unique constraint is what makes the endpoint a toggle rather than a counter: tapping an emoji
    you already used deletes the row instead of adding a second one, so a reaction cannot be
    double-counted and no separate "have I reacted?" bookkeeping is needed.

    Deliberately never notified — a dorm-wide feed where every 👍 buzzes everyone's phone is
    exactly the noise Den Hurtige exists to remove, and it would stop people reacting at all.
    """

    post = models.ForeignKey(QuickPost, on_delete=models.CASCADE, related_name="reactions")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="quick_reactions"
    )
    # Long enough for a ZWJ sequence: a family emoji is 7 code points, and skin-tone variants more.
    emoji = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Reaktion"
        verbose_name_plural = "Reaktioner"
        constraints = [
            # ONE reaction per person per message, not one of each emoji. Picking a different emoji
            # replaces yours; picking the one you already used clears it. The constraint is what
            # makes that safe under a double tap.
            models.UniqueConstraint(fields=["post", "author"], name="uniq_reaction_per_author")
        ]

    def __str__(self) -> str:
        return f"{self.emoji} af {self.author}"


class ChannelMute(models.Model):
    """One resident silencing push from one channel.

    A *mute*, not a subscription: the row's presence means "do not notify me here", so the default
    for every channel is on. That direction is deliberate. With opt-in, a newly launched channel
    notifies nobody until people find it and join — and a channel nobody hears from is a channel
    nobody posts in, which is how a small kollegium's second feed dies in a week. Muting is also the
    only half anyone actually asks for ("stop buzzing me about i-byen at 2am").

    It is per resident, not per device: nobody wants to mute the same channel on their phone and
    again on their laptop. PushSubscription stays the device table; this is the preference.
    """

    resident = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="channel_mutes"
    )
    # A den_hurtige.channels slug. A mute for a channel that later disappears from the registry is
    # harmless — nothing queries it — so retiring a channel needs no cleanup migration.
    channel = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Kanal-mute"
        verbose_name_plural = "Kanal-mutes"
        constraints = [models.UniqueConstraint(fields=["resident", "channel"], name="uniq_channel_mute")]

    def __str__(self) -> str:
        return f"{self.resident} har slået {self.channel} fra"
