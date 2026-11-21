"""The same send arriving twice is saved once (core.resend): a second press during a slow upload, a
browser retry, a page brought back with the sent text still in it."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.resend import RESEND_WINDOW
from den_hurtige.models import QuickComment, QuickPost
from opslagstavle import access as board_access
from opslagstavle.models import Category, Notice, NoticeComment
from residents.models import Resident

pytestmark = pytest.mark.django_db

HX = {"HX-Request": "true"}
MESSAGE = {"content": "Nogen der har en oplader?", "duration": "60"}


@pytest.fixture
def me(client: Client, make_resident: Callable[..., Resident]) -> Resident:
    resident = make_resident(email="a@gahk.dk")
    client.force_login(resident)
    return resident


@pytest.fixture
def board_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(board_access, "ACCESS_ROLES", None)  # the rollout gate, as test_opslagstavle


def png() -> SimpleUploadedFile:
    return SimpleUploadedFile("billede.png", b"\x89PNG\r\n\x1a\n" + b"x" * 64, content_type="image/png")


# --- Den Hurtige ------------------------------------------------------------------------------------


def test_a_message_sent_twice_is_saved_once_and_answered_alike(client: Client, me: Resident) -> None:
    first = client.post("/den-hurtige/opret", MESSAGE, headers=HX)
    second = client.post("/den-hurtige/opret", MESSAGE, headers=HX)

    assert QuickPost.objects.count() == 1
    assert second.status_code == first.status_code == 200
    assert MESSAGE["content"] in second.content.decode()


def test_the_same_message_later_is_a_new_message(client: Client, me: Resident) -> None:
    client.post("/den-hurtige/opret", MESSAGE)
    QuickPost.objects.update(created_at=timezone.now() - RESEND_WINDOW - timedelta(seconds=1))

    client.post("/den-hurtige/opret", MESSAGE)

    assert QuickPost.objects.count() == 2


def test_a_different_message_or_sender_is_not_a_copy(
    client: Client, me: Resident, make_resident: Callable[..., Resident]
) -> None:
    client.post("/den-hurtige/opret", MESSAGE)
    client.post("/den-hurtige/opret", {**MESSAGE, "content": "Fundet!"})
    client.force_login(make_resident(email="b@gahk.dk"))
    client.post("/den-hurtige/opret", MESSAGE)

    assert QuickPost.objects.count() == 3


def test_a_reply_sent_twice_is_saved_once(client: Client, me: Resident) -> None:
    post = QuickPost.objects.create(author=me, content="Kaffe?")

    for _ in range(2):
        response = client.post(f"/den-hurtige/{post.pk}/kommentar", {"content": "Ja tak"}, headers=HX)

    assert response.status_code == 200
    assert QuickComment.objects.count() == 1


def test_two_photos_sent_on_their_own_are_two_replies(
    client: Client, me: Resident, settings: object, tmp_path: object
) -> None:
    """No text to compare, and opening the pictures to compare them is not worth it."""
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    post = QuickPost.objects.create(author=me, content="Hvem har set min cykel?")

    for _ in range(2):
        client.post(f"/den-hurtige/{post.pk}/kommentar", {"content": "", "image": png()})

    assert QuickComment.objects.count() == 2


def test_copies_arriving_together_wait_for_each_other(client: Client, me: Resident) -> None:
    """Both would otherwise look before either is saved. The second waits on the sender's row."""
    with CaptureQueriesContext(connection) as queries:
        client.post("/den-hurtige/opret", MESSAGE)

    locks = [q["sql"] for q in queries.captured_queries if "FOR UPDATE" in q["sql"]]
    assert any("residents_resident" in sql for sql in locks)


# --- opslagstavlen ----------------------------------------------------------------------------------


def test_a_post_sent_twice_is_saved_once_and_both_go_to_it(
    client: Client, me: Resident, board_open: None
) -> None:
    data = {"category": Category.NYT, "body": "Vaskemaskine 2 er i stykker."}

    first = client.post("/opslagstavle/opret", data)
    second = client.post("/opslagstavle/opret", data)

    notice = Notice.objects.get()
    assert first["Location"] == second["Location"] == f"/opslagstavle/{notice.pk}"


def test_a_comment_sent_twice_is_saved_once(client: Client, me: Resident, board_open: None) -> None:
    notice = Notice.objects.create(author=me, body="Fællesspisning fredag")

    for _ in range(2):
        client.post(f"/opslagstavle/{notice.pk}/kommentar", {"body": "Jeg tager dessert med"})

    assert NoticeComment.objects.count() == 1
