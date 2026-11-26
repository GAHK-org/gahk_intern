"""What opslagstavlen leaves in the audit trail (opslagstavle.auditing)."""

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from opslagstavle import access
from opslagstavle.models import Category, Notice, NoticeComment, NoticeImage
from residents.models import Resident, Role

from .audit_support import actions, changes, events

pytestmark = pytest.mark.django_db

BOARD = "/opslagstavle/"


@pytest.fixture(autouse=True)
def rollout_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access, "ACCESS_ROLES", None)


@pytest.fixture
def media_tmp(settings: object, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    return tmp_path


@pytest.fixture
def bo(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="bo@gahk.dk", first_name="Bo", last_name="Beboer")


def png() -> SimpleUploadedFile:
    return SimpleUploadedFile("billede.png", b"\x89PNG\r\n\x1a\n" + b"x" * 64, content_type="image/png")


def test_a_post_its_edit_and_its_deletion_are_recorded(client: Client, bo: Resident) -> None:
    client.force_login(bo)
    client.post(BOARD + "opret", {"category": Category.NYT, "body": "Fællesspisning fredag"})
    notice = Notice.objects.get()
    client.post(f"{BOARD}{notice.pk}/rediger", {"category": Category.NYT, "body": "Fællesspisning lørdag"})
    # Saved as it was: nothing changed, so nothing is recorded.
    client.post(f"{BOARD}{notice.pk}/rediger", {"category": Category.NYT, "body": "Fællesspisning lørdag"})
    client.post(f"{BOARD}{notice.pk}/slet")

    assert actions("opslagstavle.") == [
        "opslagstavle.notice.posted",
        "opslagstavle.notice.edited",
        "opslagstavle.notice.deleted",
    ]
    posted = events("opslagstavle.notice.posted").get()
    assert posted.actor_id == bo.pk
    assert changes(posted)["body"] == [None, "Fællesspisning fredag"]
    edited = events("opslagstavle.notice.edited").get()
    assert changes(edited) == {"body": ["Fællesspisning fredag", "Fællesspisning lørdag"]}, (
        "the Redigeret stamp is the event's own time"
    )
    gone = events("opslagstavle.notice.deleted").get().entities.get(role="subject")
    assert (gone.entity_id, gone.operation) == (notice.pk, "deleted")
    assert gone.name == "Bo Beboer: Fællesspisning lørdag"


def test_pinning_is_recorded_by_the_moderator(client: Client, bo: Resident, make_resident: Callable) -> None:
    inspektion = make_resident(email="ins@gahk.dk", roles=(Role.INSPEKTION,))
    notice = Notice.objects.create(author=bo, body="Husk vagtplanen")
    client.force_login(inspektion)

    client.post(f"{BOARD}{notice.pk}/fastgoer")
    client.post(f"{BOARD}{notice.pk}/fastgoer")

    pinned, unpinned = events("opslagstavle.notice.")
    assert (pinned.action, unpinned.action) == ("opslagstavle.notice.pinned", "opslagstavle.notice.unpinned")
    assert pinned.actor_id == inspektion.pk
    assert changes(pinned)["pinned_by"] == [None, inspektion.pk]
    assert changes(unpinned)["pinned_at"][1] is None


def test_a_comment_and_its_deletion_name_the_post(client: Client, bo: Resident) -> None:
    notice = Notice.objects.create(author=bo, body="Hvem kommer?")
    client.force_login(bo)
    client.post(f"{BOARD}{notice.pk}/kommentar", {"body": "Jeg gør"})
    comment = NoticeComment.objects.get()
    client.post(f"{BOARD}kommentar/{comment.pk}/slet")

    posted = events("opslagstavle.comment.posted").get()
    assert changes(posted)["body"] == [None, "Jeg gør"]
    assert posted.entities.get(role="notice").entity_id == notice.pk
    deleted = events("opslagstavle.comment.deleted").get()
    assert deleted.entities.get(role="subject").operation == "deleted"


def test_an_upload_and_the_sweep_of_unused_ones_are_recorded(
    client: Client, bo: Resident, media_tmp: Path
) -> None:
    client.force_login(bo)
    client.post(BOARD + "billede", {"file": png(), "alt": "Plakat"})
    upload = events("opslagstavle.image.uploaded").get()
    assert upload.actor_id == bo.pk
    assert changes(upload)["alt"] == [None, "Plakat"]

    NoticeImage.objects.update(uploaded_at=timezone.now() - timedelta(days=7))
    call_command("purge_notices")
    call_command("purge_notices")  # nothing left: no second event

    purged = events("opslagstavle.image.purged").get()
    assert purged.actor_id is None
    assert purged.details == {"count": 1}
    assert purged.entities.get(kind="opslagstavle.image").operation == "deleted"


def test_claiming_a_picture_and_reacting_leave_no_trail_of_their_own(
    client: Client, bo: Resident, media_tmp: Path
) -> None:
    client.force_login(bo)
    url = json.loads(client.post(BOARD + "billede", {"file": png()}).content)["url"]
    client.post(BOARD + "opret", {"category": Category.NYT, "body": f"![]({url})"})
    notice = Notice.objects.get()
    assert NoticeImage.objects.get().notice_id == notice.pk
    client.post(f"{BOARD}{notice.pk}/reaktion", {"emoji": "👍"})

    assert actions("opslagstavle.") == ["opslagstavle.image.uploaded", "opslagstavle.notice.posted"]
    assert not events("opslagstavle.notice.posted").get().entities.filter(kind="opslagstavle.image").exists()
    assert not events().filter(unclaimed=True).exists()
