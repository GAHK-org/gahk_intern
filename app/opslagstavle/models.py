"""Opslagstavlen — the kollegium's noticeboard, replacing its Facebook group.

Content with a lifetime of weeks to years: important events, the results of værelsesrunden,
birthdays, practical notices. That is the whole reason this is not Den Hurtige, whose docstring
promises the opposite ("deliberately ephemeral… a thread that cannot accumulate off-topic history")
and whose posts are hard-deleted after 30 minutes to 24 hours.

Two things follow from posts living for years rather than minutes, and both are deliberate
divergences from the sibling feature:

  * **Nothing expires.** The board keeps its archive: no retention window, no purge of posts, and
    only the author or a moderator removes one. `manage.py purge_notices` still runs nightly, but
    only to sweep uploads no post ever referenced. There is no lazy purge on page load (Den Hurtige
    has one): its tolerance is minutes, so a missed cron is visibly wrong within the hour; here a
    missed night affects nothing a reader can see. See spec/features/opslagstavle.md for why the
    two-year window was removed.
  * **The body is Markdown, rendered on read.** Only the source is stored — see core.markdown for
    why that is worth a few milliseconds a page.
"""

from collections.abc import Iterable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import models
from django.db.models import F
from django.db.models.base import ModelBase
from django.db.models.signals import post_delete
from django.dispatch import receiver

from core.files import delete_attached_files
from residents.models import Resident, embedsgruppe_of

# NO RETENTION. Opslag are kept indefinitely; nothing deletes a post but its author or a moderator.
#
# There used to be a RETENTION_DAYS window here — five years, shortened to two after user testing on
# the grounds that "a board people actually read does not need a half-decade of history". That was a
# judgement about what a *reader* wants near the top of a board, and pagination and the category
# filter already answer it: an opslag from 2029 costs a reader nothing if they never page back to it.
# What the window did cost was the archive — værelsesrunden results, the practical notices, who
# announced what — which is the half of leaving Facebook that a two-year window quietly threw away.
#
# The board is now unbounded, so the things that used to be bounded by it are worth stating: the
# category index below carries the list query, and Notice rows carry no files (NoticeImage does), so
# growth is rows of text at a kollegium's posting rate. See spec/features/opslagstavle.md.

# An uploaded image nobody referenced within this window is an abandoned draft (the composer was
# opened, pictures were added, the tab was closed). Long enough that removing an image and
# immediately re-adding it cannot lose the file.
ORPHAN_IMAGE_GRACE = timedelta(days=1)

# A pinned post sits permanently above everything else, so an unbounded pin list would fill the top
# of the board and make pinning meaningless. (It used to also mean "exempt from the purge"; with no
# retention left, pinning is now only about prominence.)
MAX_PINNED = 5

MAX_BODY_CHARS = 8_000
MAX_COMMENT_CHARS = 1_000


class Category(models.TextChoices):
    """The fixed set from the feature request.

    Values are ASCII because they appear in a querystring (`?kategori=vaerelsesrunde`); the labels
    carry the Danish. A lookup table would need an admin, a seed and a migration per entry, for a set
    that changes about once a year.
    """

    NYT = "nyt", "Nyt & info"
    BEGIVENHED = "begivenhed", "Begivenhed"
    VAERELSESRUNDE = "vaerelsesrunde", "Værelsesrunden"
    FOEDSELSDAG = "foedselsdag", "Fødselsdag"
    PRAKTISK = "praktisk", "Praktisk"
    ANDET = "andet", "Andet"


class NoticeQuerySet(models.QuerySet["Notice"]):
    def pinned(self) -> "NoticeQuerySet":
        """Pinned posts, newest pin first — so pinning a second thing puts it on top, which is what
        a moderator expects. (A boolean flag could not express that.)"""
        return self.filter(pinned_at__isnull=False).order_by(F("pinned_at").desc())

    def unpinned(self) -> "NoticeQuerySet":
        """Everything else, newest first. The board paginates *this*, and renders `pinned()` above
        the paginator on every page — otherwise a pin would only be visible on page 1, which makes
        pinning pointless once the board is a few pages deep."""
        return self.filter(pinned_at__isnull=True).order_by("-created_at")


class AuthoredByResident(models.Model):
    """A post or a comment, with its author's embedsgruppe frozen at the moment it was written.

    The pill beside a byline (see templates/opslagstavle/_notice_card.html) is a snapshot, not a
    lookup. Embedsgrupper rotate monthly (F-010), so resolving it on read would relabel the whole
    board every time indstilling saves a månedsliste — a post from last spring would be attributed
    to whatever group its author happens to be in this month, and the archive would spend most of
    its life wrong. Written once, it stays true.

    A NAME, not a FK to core.Workgroup, for the same reason: a snapshot has to survive the group
    being renamed or deleted, and a FK would follow the rename and take the post's history with it.

    Stamped in `save()` rather than by each caller. There are four ways a post gets created (the
    compose view, the comment view, opslagstavle.demo and the admin) and a snapshot that is
    sometimes missing is worse than no snapshot at all — the pill would silently vanish from
    whichever path someone forgot. It costs one query per *creation*, which is rare; reading the
    board costs none, which is not.
    """

    author_embedsgruppe = models.CharField(
        max_length=100,  # core.Workgroup.name
        blank=True,
        verbose_name="Embedsgruppe",
        help_text="Forfatterens embedsgruppe da opslaget blev skrevet. Sættes automatisk.",
    )

    class Meta:
        abstract = True

    if TYPE_CHECKING:  # declared as a real FK by each concrete model, with its own related_name
        author: Resident

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        # Only on the way in, and only if nothing set it: an edit must not re-stamp a post with its
        # author's group today (the same reasoning as `edited_at` not being auto_now), and a fixture
        # that wants a post attributed to a particular month can set the field itself.
        if self._state.adding and not self.author_embedsgruppe:
            self.author_embedsgruppe = embedsgruppe_of(self.author)
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )


class Notice(AuthoredByResident):
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notices")
    category = models.CharField(
        max_length=20, choices=Category.choices, default=Category.NYT, verbose_name="Kategori"
    )
    body = models.TextField(
        verbose_name="Indhold",
        help_text="Markdown. Kun kilden gemmes — HTML dannes når opslaget vises.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Set explicitly by the edit view, deliberately NOT auto_now: auto_now fires on every save(), so
    # pinning a post would stamp it "Redigeret" with Inspektionen's timestamp. Readers see this, and
    # a false edit marker is worse than none.
    edited_at = models.DateTimeField(null=True, blank=True)

    # Pinning as a nullable timestamp rather than a boolean. One column answers two questions: is it
    # pinned, and how do pins order among themselves (newest pin first) — which a boolean cannot
    # express, so it would need a second column anyway. It used to answer a third (exemption from
    # the purge, where `pinned_at__isnull=True` was literally the filter); retention is gone and the
    # column is unchanged.
    pinned_at = models.DateTimeField(null=True, blank=True)
    pinned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pinned_notices",
    )

    # "This post is about that begivenhed." Announce here, sign up there — which is what the two
    # features' docstrings have said all along, with no way to actually get from one to the other
    # until now.
    #
    # A STRING REFERENCE, not an import. `events` is the newer app and its own docstring points back
    # here; importing the model would make the dependency mutual at module level, and a
    # ForeignKey("events.Event") costs nothing to avoid it.
    #
    # SET_NULL, and that is the whole retention story in one keyword: an event is deleted a week
    # after it is held (events.models), a notice lives about two years. CASCADE would mean Tuesday's
    # fællesspisning quietly deleting the post that announced it — along with its comments and
    # reactions — the following Wednesday. The link simply disappears and the post stays, which is
    # also why the card has to cope with `event` being None on a post that once had one.
    #
    # ONLY OPEN EVENTS may be linked (see forms.NoticeForm), because opslagstavlen is visible to
    # every resident and a chip naming a private party would leak exactly what "kun inviterede" is
    # there to hide. The template checks visibility again on the way out, for the event that was
    # open when it was linked and was made private afterwards.
    event = models.ForeignKey(
        "events.Event",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notices",
        verbose_name="Begivenhed",
    )

    objects = NoticeQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Opslag"
        verbose_name_plural = "Opslag"
        # The list is always "optional category filter + newest first"; this composite serves the
        # filtered and the unfiltered query, and it is what carries the board now that nothing bounds
        # its length. Deliberately NO index on pinned_at: the pinned set is capped at MAX_PINNED, so
        # Postgres seq-scans it faster than it reads an index, and an unused index is a write cost
        # plus a lie about which queries matter.
        indexes = [models.Index(fields=["category", "-created_at"], name="notice_cat_recent_idx")]
        # NO CheckConstraint tying pinned_by to pinned_at, tempting as it looks: pinned_by is
        # SET_NULL, so deleting the resident who pinned a post would violate it and make the delete
        # fail. The pair is maintained by the pin view instead.

    def __str__(self) -> str:
        # No title to name a post by any more, so identify it the way the board does: by who wrote
        # it. The pk keeps two posts from one person distinguishable in the admin changelist.
        return f"Opslag #{self.pk} af {self.author.full_name}"

    @property
    def is_pinned(self) -> bool:
        return self.pinned_at is not None


class NoticeComment(AuthoredByResident):
    """A reply to a notice. **Plain text, deliberately not Markdown — with one attached photo.**

    STILL NOT MARKDOWN, and the photo does not change that. The reason Markdown stays on the post
    body is the embedded-image LIFECYCLE: a picture referenced from Markdown has no FK to hang on,
    so NoticeImage exists to claim uploads at save time and an orphan sweep exists to collect the
    ones nobody referenced. `image` here is an ordinary FileField on the row — one file, owned by
    one comment, deleted with it by the post_delete receiver below. No claiming step, no orphan
    window, nothing added to the compose toolbar. Body text is rendered autoescaped with
    `white-space: pre-wrap` and `|urlize`, exactly like a Den Hurtige reply.

    `body` IS BLANK-ABLE because the photo can be the whole comment — "her, se" is an answer, and
    Den Hurtige's replies have always worked that way. What must not happen is a row with neither,
    which the view enforces rather than the field: whether a photo counts depends on whether it
    survived validation, and only the view knows that (see views.create_comment).
    """

    notice = models.ForeignKey(Notice, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notice_comments"
    )
    # Blank when the photo IS the comment.
    body = models.TextField(max_length=MAX_COMMENT_CHARS, blank=True, verbose_name="Kommentar")
    # FileField, not ImageField, for the same reason QuickPost.image is one: ImageField needs
    # Pillow, which is deliberately not a production dependency. core.uploads decides what counts
    # as an image and the view applies it.
    image = models.FileField(
        upload_to="opslag/kommentarer/%Y/%m/",
        max_length=255,
        blank=True,
        verbose_name="Billede",
        help_text="Valgfrit billede.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Kommentar"
        verbose_name_plural = "Kommentarer"

    def __str__(self) -> str:
        return f"Kommentar af {self.author} på #{self.notice_id}"


class NoticeReaction(models.Model):
    """One emoji one resident put on one notice. Semantics and grammar are shared with Den Hurtige
    (core.reactions, core.emoji): a different emoji moves yours, the same one clears it."""

    notice = models.ForeignKey(Notice, on_delete=models.CASCADE, related_name="reactions")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notice_reactions"
    )
    # Long enough for a ZWJ sequence: a family emoji is 7 code points, skin-tone variants more.
    emoji = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Reaktion"
        verbose_name_plural = "Reaktioner"
        constraints = [
            # Present from migration 0001 on purpose: den_hurtige had to ship a RunPython data step
            # (its migration 0004) to collapse pre-existing duplicates when this constraint was
            # added late. Starting with it means that can never be needed here.
            models.UniqueConstraint(fields=["notice", "author"], name="uniq_notice_reaction_per_author")
        ]

    def __str__(self) -> str:
        return f"{self.emoji} af {self.author}"


class NoticeImage(models.Model):
    """An image uploaded from the compose toolbar and referenced by URL from a Notice body.

    The FK is nullable because the image is uploaded *before* the post exists — the toolbar inserts
    `![alt](url)` into a textarea, and there is no server-side draft to attach it to. It is claimed
    at save time by parsing the Markdown (opslagstavle.images), which is what turns "delete a post,
    its pictures go" into a database property (CASCADE plus the post_delete receiver below) rather
    than a nightly body-scanning job that can never be exact.

    FileField, not ImageField: ImageField requires Pillow, which is not a dependency of this project
    (same call as QuickPost.image, CmsImage.file, RoomConditionScore.photo). core.uploads validates.
    """

    notice = models.ForeignKey(Notice, null=True, blank=True, on_delete=models.CASCADE, related_name="images")
    file = models.FileField(upload_to="opslag/%Y/%m/", max_length=255, verbose_name="Fil")
    # Doubles as the Markdown alt text the toolbar inserts, so a described image starts accessible.
    alt = models.CharField(max_length=255, blank=True, verbose_name="Beskrivelse")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notice_images",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]
        verbose_name = "Billede"
        verbose_name_plural = "Billeder"
        # Serves the orphan sweep: unclaimed rows older than the grace period.
        indexes = [models.Index(fields=["notice", "uploaded_at"], name="notice_img_orphan_idx")]

    def __str__(self) -> str:
        return self.alt or (self.file.name or "(uden fil)")

    @property
    def url(self) -> str:
        return self.file.url if self.file else ""


@receiver(post_delete, sender=NoticeComment)
@receiver(post_delete, sender=NoticeImage)
def _delete_notice_files(sender: type[models.Model], instance: models.Model, **kwargs: Any) -> None:  # noqa: ANN401
    """Remove the upload from storage when its row goes.

    Registered as a signal rather than an override of delete() for two reasons that both matter
    here: a bulk queryset delete never calls Model.delete(), and these rows are usually removed by
    cascade from Notice rather than directly. See core.files.
    """
    delete_attached_files(instance)
