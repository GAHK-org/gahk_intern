"""Den Hurtige - short-lived urgent messages, replacing the kollegium's Messenger group.

Posts leave the feed on a timer and are then ARCHIVED, never deleted. That is a reversal: they used
to be hard-deleted, on the argument that a thread which cannot accumulate off-topic history is the
whole point of the feature. The feed still gets that — a message is gone from it within hours — but
the house lost things it wanted back (who borrowed the drill, what time we agreed on), and "it is
deleted, sorry" was the wrong answer to a question people kept asking. So the countdown on every
bubble now means "leaves the feed", not "is destroyed".

ARCHIVED IS A TIMESTAMP, NOT A FLAG. `expires_at <= now` IS the archive; there is no second field to
write, no sweep to run, and therefore no window in which a message is expired-but-not-yet-archived
for something to get wrong. `active()` and `archived()` are the two halves of one comparison and
partition the table between them. Nothing has to happen on a schedule for a message to archive,
which is why the cron job that used to drain this table is gone (DEPLOY.md §4b).

What archiving COSTS, so it is not a surprise later: the table and its images now grow without
bound. That is the deliberate choice — the archive is a record — but it is the reason
QuickPost.image has no derivative pipeline and the reason Meta.indexes gained a third entry.

THE CLOCK IS core.clock, NOT django.utils.timezone. Every comparison below reads
`current_datetime()`, which is the real clock in production and the DEV-ONLY simulated one under
DEBUG (core/clock.py). This used to call `timezone.now()` directly, and the consequence was that
advancing the dev clock a month — the obvious way to see what the archive looks like with history
in it — moved every other feature's sense of "now" and left this one in the present. The rule is
the same one begivenheder follows: a feature that decides anything from the date asks core.clock.

`created_at` is the exception, and deliberately: it is `auto_now_add`, so a message written while
the clock is advanced is stamped with the real instant. The alternative is worse than the oddity —
the simulated clock has a resolution of one DAY, so every message posted under it would share a
timestamp to the microsecond, and a chat log whose messages cannot be ordered is not a log.

An archived post is READ-ONLY, and that is enforced in views.py rather than here: no reply, no
reaction, no deletion (den_hurtige.views._post_or_404 resolves writes against `active()` alone).
A model-level guard was considered and rejected — the admin must still be able to remove something
genuinely unlawful, and a save() override that silently refused would make that impossible to do.

Deleting a message is two acts, split by DELETE_GRACE: inside five minutes the row is destroyed,
after it the message becomes a tombstone (`deleted_at` set, text and image wiped, replies kept). The
split exists because a message nobody has read yet is a slip, and one that has been answered is part
of a conversation that stops making sense without it. Moderators follow the same rule; the Django
admin is the only place a row is destroyed outright. Archived messages cannot be deleted at all.

Posts are filed into a *channel* (`QuickPost.channel`). The channels themselves are constants in
den_hurtige.channels, not rows here — see that module for why. Nothing in this file knows which
channels exist, and neither half of the partition may become channel-aware: `active()` and
`archived()` stay channel-agnostic, so a post in a channel nobody has opened for a week still
leaves the feed on time.
"""

from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from core.clock import current_datetime
from core.files import delete_attached_files

# Compared against `created_at`, which is auto_now_add and so always the REAL clock -- hence
# timezone.now() rather than core.clock in within_delete_grace. Coincides with views.GROUPING_WINDOW
# and is deliberately not shared with it; the two measure unrelated things.
DELETE_GRACE = timedelta(minutes=5)

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
    """When a post archives if the composer sends no duration: DEFAULT_DURATION_MINUTES from now."""
    return current_datetime() + timedelta(minutes=DEFAULT_DURATION_MINUTES)


class QuickPostQuerySet(models.QuerySet["QuickPost"]):
    """The live feed and the archive, which are one comparison read in both directions.

    They are written as a strict partition -- `> now` and `<= now` on the same column, evaluated
    against the same clock -- so no message can be in both or in neither. That matters more than it
    looks: the archive is now the only copy, so a post that fell out of `active()` without falling
    into `archived()` would not be hidden, it would be LOST to every reader.
    """

    def active(self) -> "QuickPostQuerySet":
        """On the feed: posts whose timer has not run out."""
        return self.filter(expires_at__gt=current_datetime())

    def archived(self) -> "QuickPostQuerySet":
        """In the archive: posts whose timer has run out.

        There is no `purge_expired()` any more, and nothing replaced it. Archiving is this filter
        reading the other way -- it happens at the moment the clock passes `expires_at`, with no
        row written and no job to run. Removed rather than renamed, so a caller that still wanted
        the old destructive behaviour fails at import instead of silently getting the new one.
        """
        return self.filter(expires_at__lte=current_datetime())


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
    # Named for what it still is -- the moment the timer runs out -- rather than renamed to
    # `archived_at` when the meaning of that moment changed. A rename would have been a migration
    # plus an edit to every caller, to say the same thing about the same instant; what changed is
    # what HAPPENS at it, and that is in views.py and in the docstring above, not in a column name.
    expires_at = models.DateTimeField(
        default=get_default_expiration,
        help_text="Hvornår beskeden forlader feedet og lægges i arkivet.",
    )

    # Deliberately NOT part of active()/archived(): a tombstone stays on the feed and then in the
    # archive, holding the place of the message that was there.
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Sat hvis beskeden er slettet. Selve teksten og billedet er så væk for altid.",
    )

    objects = QuickPostQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Hurtigt opslag"
        verbose_name_plural = "Hurtige opslag"
        # Three indexes for three questions, and the third is the one archiving added.
        #
        # (channel, expires_at) serves the FEED: one channel's live posts, on every page load and
        # every 5s poll. The lone expires_at index is what is left of the purge, which swept every
        # channel at once; it still serves any cross-channel question about the clock.
        #
        # (channel, -created_at) serves the ARCHIVE, and it is not optional. That query filters on
        # expires_at and orders by created_at DESCENDING, so neither index above can answer it
        # without sorting the result -- and unlike the feed, whose working set is a few hours wide,
        # the archive is every message the channel has ever held and only ever grows. Leading with
        # `channel` and descending on `created_at` lets the paging read the index in order and stop
        # at the page size; expires_at is left as a residual filter, which costs almost nothing
        # because all but the newest handful of rows match it.
        indexes = [
            models.Index(fields=["expires_at"]),
            models.Index(fields=["channel", "expires_at"]),
            models.Index(fields=["channel", "-created_at"], name="dh_archive_page_idx"),
        ]

    def __str__(self) -> str:
        return f"Opslag af {self.author} kl. {self.created_at:%H:%M}"

    @property
    def is_archived(self) -> bool:
        """Off the feed and read-only. Was `is_expired`, and renamed rather than aliased: the two
        would have meant the same thing while reading as different states, and every caller of it
        is asking whether the message may still be written to."""
        return current_datetime() >= self.expires_at

    @property
    def is_deleted(self) -> bool:
        """A tombstone: the message is gone, its place in the conversation is not.

        A stored fact, unlike `is_archived` -- which is why templates read this directly but must be
        handed `archived` by the view (_message.html says why).
        """
        return self.deleted_at is not None

    @property
    def within_delete_grace(self) -> bool:
        """Still young enough to be taken back without leaving a tombstone. See DELETE_GRACE."""
        return timezone.now() - self.created_at < DELETE_GRACE

    def soft_delete(self) -> bool:
        """Turn this message into a tombstone: destroy what it said, keep where it said it.

        Returns False if the row was hard-deleted or already tombstoned by a concurrent request, so
        the caller can report that instead of claiming a success that did not happen.

        The replies are left alone on purpose (see the module docstring), which is also why this
        cannot be a delete() override -- the cascade would take them. The reactions go with the
        content they were applied to.

        A CONDITIONAL UPDATE RATHER THAN save(update_fields=...), because there is no transaction
        around the request (no ATOMIC_REQUESTS) and two deletions can therefore overlap: `save` on a
        row that has just been hard-deleted raises "Save with update_fields did not affect any
        rows", which is a 500 on the one gesture people double-tap. The filter makes the tombstone a
        claim -- exactly one caller can win it -- and the rowcount says whether this one did.
        """
        with transaction.atomic():
            claimed = QuickPost.objects.filter(pk=self.pk, deleted_at__isnull=True).update(
                content="", image="", deleted_at=timezone.now()
            )
            if not claimed:
                return False
            # Reads `self.image`, which the UPDATE above cleared in the database but not in memory.
            # The atomic block is what makes this safe: core.files defers the storage delete to
            # commit, so without one it would unlink the photograph before the row was written.
            delete_attached_files(self)
            self.reactions.all().delete()
        self.refresh_from_db()
        return True

    @property
    def minutes_left(self) -> int:
        """Whole minutes until expiry, floored at 0 — the primitive expires_label is built from."""
        return max(0, int((self.expires_at - current_datetime()).total_seconds() // 60))

    @property
    def expires_label(self) -> str:
        """Time left on the feed, humanised: "1 døgn", "12 timer", "1 time", "45 min", "arkiveret".

        The zero case reads "arkiveret" rather than "udløbet" because that is now where the message
        goes, and this label is the only place the feed ever explains the countdown. A bubble
        rendered at exactly the crossing -- the poll caught it between two ticks -- would otherwise
        announce that it had been deleted, which is the thing this feature stopped doing.
        """
        minutes = round((self.expires_at - current_datetime()).total_seconds() / 60)
        if minutes <= 0:
            return "arkiveret"
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
    """A reply to a QuickPost. Archived with its post, and read-only from that moment on.

    There is no `expires_at` here and there never was: a reply's lifetime is its post's. What
    changed with archiving is only what that means at the end of it -- the whole thread is kept and
    rendered read-only, rather than cascaded away. The CASCADE below therefore now fires only on a
    real deletion (an admin removing something, or an author deleting a message that is still live).

    It also outlives its message being deleted: soft_delete never touches this table, so what an
    author takes back is their own words and only those.
    """

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
    """Remove an attached file from storage when its message or reply is genuinely deleted.

    STILL NEEDED, AND FOR LESS THAN IT USED TO BE. Archiving removed the case this was written for
    -- the nightly purge that dropped expired rows in bulk and left their photographs on disk
    forever -- so what is left is the narrow one: an author deleting a live message, a moderator
    removing something, an admin cleaning up. Those are ordinary deletes and would leak the same
    way, because Django has not deleted FileField files on row delete since 1.3.

    It stays a signal rather than a `delete()` override for the reason core.files documents: a
    cascade never calls Model.delete(), so a reply's image would outlive the message it hung off.
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
