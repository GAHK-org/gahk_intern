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
"""

from datetime import timedelta
from typing import Any

from django.db import models
from django.utils import timezone

from residents.models import Resident

# How long a ticket may sit in Færdig before the nightly sweep archives it. Long enough that a
# just-closed repair does not vanish from the board while still fresh, short enough that Færdig does
# not quietly become a permanent record of every repair the kollegium has ever made.
ARCHIVE_AFTER_DAYS = 30


class RepairTaskQuerySet(models.QuerySet["RepairTask"]):
    def active(self) -> "RepairTaskQuerySet":
        """Everything the board shows — the default view of the table."""
        return self.filter(archived_at__isnull=True)

    def archived(self) -> "RepairTaskQuerySet":
        """Swept off the board, but still here to search — see the module docstring."""
        return self.filter(archived_at__isnull=False)

    def due_for_archive(self, now: Any = None) -> "RepairTaskQuerySet":  # noqa: ANN401 — a datetime
        """Finished, not yet archived, and past ARCHIVE_AFTER_DAYS since the move to Færdig.

        `updated_at` is the proxy for "when it reached Færdig": the model has no separate timestamp
        for the move, and every status change touches `updated_at` anyway (see views.set_status).
        """
        cutoff = (now or timezone.now()) - timedelta(days=ARCHIVE_AFTER_DAYS)
        return self.filter(status=RepairTask.Status.FAERDIG, archived_at__isnull=True, updated_at__lt=cutoff)


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
    archived_at = models.DateTimeField(null=True, blank=True)

    objects = RepairTaskQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title


class RepairComment(models.Model):
    """A note on a repair — progress updates from Reppergruppen, extra detail from whoever reported
    it. Open to any resident to add, like Opslagstavlen's comments; see views.can_delete_comment for
    who may remove one."""

    task = models.ForeignKey(RepairTask, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(Resident, on_delete=models.CASCADE, related_name="repair_comments")
    body = models.TextField("Note")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.author}: {self.body[:40]}"
