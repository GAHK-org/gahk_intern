"""Reparationer — a kanban board for tracking repairs on the kollegium.

Any resident may report something broken (views.create). Two crews then own the pipeline in
sequence, tracked separately from `status`:

  * `responsible` says WHO currently owns the ticket — Viceværterne by default (they triage), or
    Reppergruppen once a Vicevært hands it over (views.set_responsible). It exists apart from
    `status` because the two axes are independent: a ticket can sit "I gang" under either owner.
  * `status` says WHERE it is in the pipeline. Viceværterne may move a ticket through most columns,
    but two are Reppergruppen/Inspektionen/administrator-only (views.MANAGER_ONLY_STATUSES): "AK
    projekt" — routing a job to an AK work session is a commitment of AK hours only that crew should
    make — and "Færdig" itself, for the same reason "Godkend af Repper" exists as its own column
    rather than folding into it: a Vicevært needs somewhere to leave a ticket they believe is
    finished without being able to actually close it.

Status.choices is the single source both the board and the per-card "move to…" buttons iterate
over, so column order here IS column order on the page.

**Archiving.** A ticket left in Færdig for ARCHIVE_AFTER_DAYS is swept off the board by the
`archive_finished_repairs` management command (run nightly, mirrors opslagstavle's purge_notices —
see DEPLOY.md §4b), which stamps `archived_at` rather than deleting the row: a repair's history is
worth keeping searchable, unlike a stale noticeboard post. A manager may also archive/reopen a
ticket immediately (views.archive_now/unarchive) instead of waiting for the sweep.

**Column age.** `status_changed_at` is stamped whenever a ticket moves column (views.set_status), and
the board colours a card by how long it has sat where it is (RepairTask.column_age): yellow, then red,
at thresholds per column (COLUMN_AGE_DAYS). A move resets it — the question the colour answers is
"has anyone touched this lately?", not "how old is the fault?". Færdig is always green instead: a
finished ticket waiting for the sweep is not neglected, it is done.

**Photos.** RepairImage holds them — on the ticket itself (comment empty) or on one of its notes. A
row per photo rather than a FileField on each model because both places take several at once; see
views._attached_images for the upload rules (shared with every other feature via core.uploads).
"""

from datetime import timedelta
from typing import Any

from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver

from core.clock import current_datetime
from core.files import delete_attached_files
from residents.models import Resident

# How long a ticket may sit in Færdig before the nightly sweep archives it. Long enough that a
# just-closed repair does not vanish from the board while still fresh, short enough that Færdig does
# not quietly become a permanent record of every repair the kollegium has ever made.
ARCHIVE_AFTER_DAYS = 30

# How long a ticket may sit in one column before its card turns yellow, then red ("a month", "two
# months" in the feature request). See RepairTask.column_age.
STALE_AFTER_DAYS = 30
OVERDUE_AFTER_DAYS = 60
# Columns on a shorter clock, as (yellow, red) days. "Godkend af Repper" is a ticket someone believes
# is finished, waiting only for Reppergruppen to check it — that should take days, not months.
COLUMN_AGE_DAYS = {"godkendt": (7, 14)}


class RepairTaskQuerySet(models.QuerySet["RepairTask"]):
    def active(self) -> "RepairTaskQuerySet":
        """Everything the board shows — the default view of the table."""
        return self.filter(archived_at__isnull=True)

    def archived(self) -> "RepairTaskQuerySet":
        """Swept off the board, but still here to search — see the module docstring."""
        return self.filter(archived_at__isnull=False)

    def due_for_archive(self, now: Any = None) -> "RepairTaskQuerySet":  # noqa: ANN401 — a datetime
        """Finished, not yet archived, and past ARCHIVE_AFTER_DAYS since the move to Færdig."""
        cutoff = (now or current_datetime()) - timedelta(days=ARCHIVE_AFTER_DAYS)
        return self.filter(
            status=RepairTask.Status.FAERDIG, archived_at__isnull=True, status_changed_at__lt=cutoff
        )


class RepairTask(models.Model):
    class Status(models.TextChoices):
        NY = "ny", "Ny"
        I_GANG = "i_gang", "I gang"
        AFVENTER = "afventer", "Afventer"
        AK_PROJEKT = "ak_projekt", "AK projekt"
        GODKENDT = "godkendt", "Godkend af Repper"
        FAERDIG = "faerdig", "Færdig"

    class Responsible(models.TextChoices):
        VICEVAERT = "vicevaert", "Vicevært"
        REPPER = "repper", "Repper"

    title = models.CharField("Titel", max_length=200)
    description = models.TextField("Beskrivelse", blank=True)
    location = models.CharField("Sted", max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NY)
    responsible = models.CharField(
        "Ansvarlig", max_length=20, choices=Responsible.choices, default=Responsible.VICEVAERT
    )
    reported_by = models.ForeignKey(Resident, on_delete=models.CASCADE, related_name="reported_repairs")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # When the ticket entered its current column — set by views.set_status on a real move only, so
    # a handoff or a note does not reset a card's colour. Not auto_now: updated_at already is, and
    # it moves for every edit. Read from core.clock, like every date this feature decides on, so a
    # developer fast-forwarding the dev clock sees cards age (and a move made "in the future" is
    # stamped there too).
    status_changed_at = models.DateTimeField(default=current_datetime)
    archived_at = models.DateTimeField(null=True, blank=True)

    objects = RepairTaskQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title

    def days_in_column(self, now: Any = None) -> int:  # noqa: ANN401 — a datetime
        # Clamped: winding the dev clock back past a stamp would otherwise read "-3 dage".
        return max(0, ((now or current_datetime()) - self.status_changed_at).days)

    @property
    def column_age(self) -> str:
        """The board's card colour: "done", "overdue", "stale" or "" (see the module docstring)."""
        if self.status == self.Status.FAERDIG:
            return "done"
        stale, overdue = COLUMN_AGE_DAYS.get(self.status, (STALE_AFTER_DAYS, OVERDUE_AFTER_DAYS))
        days = self.days_in_column()
        if days >= overdue:
            return "overdue"
        if days >= stale:
            return "stale"
        return ""


class RepairComment(models.Model):
    """A note on a repair — progress updates from Reppergruppen, extra detail from whoever reported
    it. Open to any resident to add, like Opslagstavlen's comments; see views.can_delete_comment for
    who may remove one."""

    task = models.ForeignKey(RepairTask, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(Resident, on_delete=models.CASCADE, related_name="repair_comments")
    # Blank when the photos ARE the note — "se her" is an answer. The view refuses a note with
    # neither (views.create_comment), as opslagstavle's comments do.
    body = models.TextField("Note", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.author}: {self.body[:40]}"


class RepairImage(models.Model):
    """A photo of the fault — on the ticket itself (`comment` empty) or on one of its notes.

    FileField, not ImageField: ImageField requires Pillow, which is not a production dependency (same
    call as NoticeComment.image). core.uploads decides what counts as an image; views applies it.
    """

    task = models.ForeignKey(RepairTask, on_delete=models.CASCADE, related_name="images")
    comment = models.ForeignKey(
        RepairComment, null=True, blank=True, on_delete=models.CASCADE, related_name="images"
    )
    file = models.FileField("Billede", upload_to="reparationer/%Y/%m/", max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at", "pk"]

    def __str__(self) -> str:
        return self.file.name or "(uden fil)"


@receiver(post_delete, sender=RepairImage)
def _delete_repair_image_file(sender: type[models.Model], instance: models.Model, **kwargs: Any) -> None:  # noqa: ANN401
    """Remove the upload from storage when its row goes — usually by cascade from a deleted ticket
    or note, which never calls Model.delete(). See core.files."""
    delete_attached_files(instance)
