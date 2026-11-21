"""A form's send is saved once, by the token the form carries (core.send_once): a second press during a
slow upload or a browser retry arrives with the same token, a new message with a new one."""

import re
import uuid
from collections.abc import Callable
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from core.models import SentForm
from core.send_once import KEPT
from den_hurtige.models import QuickComment, QuickPost
from opslagstavle import access as board_access
from opslagstavle.models import Category, Notice, NoticeComment
from residents.models import Resident

pytestmark = pytest.mark.django_db

HX = {"HX-Request": "true"}


def message(token: str, content: str = "Nogen der har en oplader?") -> dict[str, str]:
    return {"content": content, "duration": "60", "send_token": token}


def token() -> str:
    return str(uuid.uuid4())


def tokens_in(html: str) -> list[str]:
    return re.findall(r'name="send_token" value="([0-9a-f-]{36})" data-send-token', html)


@pytest.fixture
def me(client: Client, make_resident: Callable[..., Resident]) -> Resident:
    resident = make_resident(email="a@gahk.dk")
    client.force_login(resident)
    return resident


@pytest.fixture(autouse=True)
def board_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(board_access, "ACCESS_ROLES", None)  # the rollout gate, as test_opslagstavle


@pytest.fixture
def media(settings: object, tmp_path: object) -> None:
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]


def png() -> SimpleUploadedFile:
    return SimpleUploadedFile("billede.png", b"\x89PNG\r\n\x1a\n" + b"x" * 64, content_type="image/png")


# --- the forms carry a token ------------------------------------------------------------------------


def test_every_send_form_carries_its_own_token(client: Client, me: Resident) -> None:
    post = QuickPost.objects.create(author=me, content="Kaffe?")
    notice = Notice.objects.create(author=me, body="Fællesspisning")

    pages = {
        "Den Hurtige": client.get("/den-hurtige/"),
        "thread panel": client.get(f"/den-hurtige/{post.pk}/traad", headers=HX),
        "new post": client.get("/opslagstavle/opret"),
        "post page": client.get(f"/opslagstavle/{notice.pk}"),
    }

    found = {name: tokens_in(page.content.decode()) for name, page in pages.items()}
    assert all(found.values()), found
    every = [t for ts in found.values() for t in ts]
    assert len(set(every)) == len(every)  # a token per form, never shared


# --- Den Hurtige ------------------------------------------------------------------------------------


def test_a_message_arriving_twice_is_saved_once_and_answered_alike(client: Client, me: Resident) -> None:
    sent = token()

    first = client.post("/den-hurtige/opret", message(sent), headers=HX)
    second = client.post("/den-hurtige/opret", message(sent), headers=HX)

    assert QuickPost.objects.count() == 1
    assert second.status_code == first.status_code == 200
    assert "Nogen der har en oplader?" in second.content.decode()


def test_the_same_text_sent_again_on_purpose_is_a_new_message(client: Client, me: Resident) -> None:
    """A new send is a new token, whatever it says."""
    client.post("/den-hurtige/opret", message(token()))
    client.post("/den-hurtige/opret", message(token()))

    assert QuickPost.objects.count() == 2


def test_a_photo_reply_arriving_twice_is_saved_once(client: Client, me: Resident, media: None) -> None:
    post = QuickPost.objects.create(author=me, content="Hvem har set min cykel?")
    sent = token()

    for _ in range(2):
        response = client.post(
            f"/den-hurtige/{post.pk}/kommentar",
            {"content": "", "image": png(), "send_token": sent},
            headers=HX,
        )

    assert response.status_code == 200
    assert QuickComment.objects.count() == 1


def test_a_refused_message_keeps_its_token_for_the_send_that_follows(client: Client, me: Resident) -> None:
    """Nothing was saved, so the same form sending again (now with a duration) is not a copy."""
    sent = token()
    refused = client.post("/den-hurtige/opret", {"content": "Hej", "send_token": sent}, headers=HX)

    client.post("/den-hurtige/opret", message(sent, "Hej"), headers=HX)

    assert refused.status_code == 422
    assert QuickPost.objects.count() == 1


def test_a_send_without_a_token_is_saved_as_before(client: Client, me: Resident) -> None:
    """A page drawn before tokens existed, or a token that is not one."""
    client.post("/den-hurtige/opret", {"content": "Hej", "duration": "60"})
    client.post("/den-hurtige/opret", {"content": "Hej", "duration": "60"})
    client.post("/den-hurtige/opret", message("ikke-et-token"))

    assert QuickPost.objects.count() == 3
    assert not SentForm.objects.exists()


def test_a_token_is_only_ever_someone_elses_copy_if_it_is_theirs(
    client: Client, me: Resident, make_resident: Callable[..., Resident]
) -> None:
    sent = token()
    client.post("/den-hurtige/opret", message(sent))
    client.force_login(make_resident(email="b@gahk.dk"))

    client.post("/den-hurtige/opret", message(sent))

    assert QuickPost.objects.count() == 2


def test_tokens_are_kept_a_day(client: Client, me: Resident) -> None:
    old = SentForm.objects.create(resident=me, token=uuid.uuid4())
    SentForm.objects.filter(pk=old.pk).update(created_at=old.created_at - KEPT - timedelta(minutes=1))

    client.post("/den-hurtige/opret", message(token()))

    assert not SentForm.objects.filter(pk=old.pk).exists()


# --- opslagstavlen ----------------------------------------------------------------------------------


def test_a_post_arriving_twice_is_saved_once_and_both_go_to_it(client: Client, me: Resident) -> None:
    data = {"category": Category.NYT, "body": "Vaskemaskine 2 er i stykker.", "send_token": token()}

    first = client.post("/opslagstavle/opret", data)
    second = client.post("/opslagstavle/opret", data)

    notice = Notice.objects.get()
    assert first["Location"] == second["Location"] == f"/opslagstavle/{notice.pk}"


def test_a_comment_arriving_twice_is_saved_once(client: Client, me: Resident) -> None:
    notice = Notice.objects.create(author=me, body="Fællesspisning fredag")
    data = {"body": "Jeg tager dessert med", "send_token": token()}

    for _ in range(2):
        client.post(f"/opslagstavle/{notice.pk}/kommentar", data)

    assert NoticeComment.objects.count() == 1
