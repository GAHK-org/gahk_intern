"""The audit trail of Reparationer (reparationer.auditing)."""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from audit.models import AuditChange
from reparationer.models import ARCHIVE_AFTER_DAYS, RepairComment, RepairTask
from residents.models import Resident, Role

from .audit_support import actions, changes, events

pytestmark = pytest.mark.django_db


def _client(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def test_a_repair_from_report_to_deletion_is_in_the_trail(
    make_resident: Callable[..., Resident], settings: object, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    bo = make_resident(email="bo@gahk.dk", first_name="Bo")
    vig = make_resident(email="vig@gahk.dk", first_name="Vig", roles=(Role.VICEVAERT,))
    rep = make_resident(email="rep@gahk.dk", first_name="Rep", roles=(Role.REPPER,))
    photo = SimpleUploadedFile("hane.png", b"\x89PNG\r\n\x1a\n" + b"x" * 64, content_type="image/png")
    _client(bo).post("/reparationer/opret", {"title": "Utæt vandhane", "location": "", "images": [photo]})
    task = RepairTask.objects.get()
    _client(vig).post(f"/reparationer/{task.pk}/status", {"status": "i_gang"})
    _client(vig).post(f"/reparationer/{task.pk}/ansvarlig", {"responsible": "repper"})
    _client(rep).post(f"/reparationer/{task.pk}/kommentar", {"body": "Skifter pakningen"})
    comment = RepairComment.objects.get()
    _client(rep).post(f"/reparationer/kommentar/{comment.pk}/slet")
    _client(rep).post(f"/reparationer/{task.pk}/status", {"status": "faerdig"})
    _client(rep).post(f"/reparationer/{task.pk}/arkiver")
    _client(rep).post(f"/reparationer/{task.pk}/genaabn")
    _client(rep).post(f"/reparationer/{task.pk}/slet")

    assert actions("reparationer.") == [
        "reparationer.task.reported",
        "reparationer.task.moved",
        "reparationer.task.handed_over",
        "reparationer.comment.added",
        "reparationer.comment.deleted",
        "reparationer.task.moved",
        "reparationer.task.archived",
        "reparationer.task.unarchived",
        "reparationer.task.deleted",
    ]
    reported = events("reparationer.task.reported").get()
    assert reported.actor_id == bo.pk
    assert changes(reported)["title"] == [None, "Utæt vandhane"]
    assert reported.entities.filter(kind="reparationer.image", operation="created").count() == 1
    moved = events("reparationer.task.moved").first()
    assert moved is not None and moved.actor_id == vig.pk
    assert changes(moved) == {"status": ["ny", "i_gang"]}, "the column clock is bookkeeping"
    assert changes(events("reparationer.task.handed_over").get()) == {"responsible": ["vicevaert", "repper"]}
    added = events("reparationer.comment.added").get()
    assert added.entities.get(role="task").entity_id == task.pk
    assert events("reparationer.task.archived").get().entities.get(role="subject").entity_id == task.pk
    deleted = events("reparationer.task.deleted").get()
    assert deleted.actor_id == rep.pk
    assert deleted.entities.filter(kind="reparationer.image", operation="deleted").count() == 1
    assert not AuditChange.objects.filter(
        field__in=["status_changed_at", "updated_at", "archived_at"]
    ).exists()


def test_the_nightly_sweep_is_one_event_naming_each_ticket(make_resident: Callable[..., Resident]) -> None:
    bo = make_resident(email="bo@gahk.dk")
    old = timezone.now() - timedelta(days=ARCHIVE_AFTER_DAYS + 1)
    done = [
        RepairTask.objects.create(reported_by=bo, title=f"Sag {i}", status="faerdig", status_changed_at=old)
        for i in range(2)
    ]
    RepairTask.objects.create(reported_by=bo, title="I gang", status="i_gang", status_changed_at=old)

    call_command("archive_finished_repairs")
    call_command("archive_finished_repairs")  # nothing left: no event

    swept = events("reparationer.archive.swept").get()
    assert (swept.actor_id, swept.details) == (None, {"count": 2})
    assert {e.entity_id for e in swept.entities.filter(role="archived")} == {t.pk for t in done}
