"""Live updates (core.live): who may listen to which topic, and which writes signal.

The signal is the whole contract between a write and every open page, so each write view is asserted
to send one. A missing signal does not fail loudly anywhere else: the page just goes quiet until its
60s backstop poll, which nobody would notice in review and everybody would notice in use.

Two tests go through a real WebSocket (channels.testing) to prove the parts fit — the consumer's
refusal, and a view's write arriving on a subscribed socket. Those are transactional because the
consumer reads the database from a worker thread, which cannot see an uncommitted test transaction.
"""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpRequest
from django.test import Client

from config.routing import websocket_urlpatterns
from core import live
from core.consumers import REFUSED
from den_hurtige import access as dh_access
from den_hurtige import channels
from den_hurtige.channels import Channel
from den_hurtige.models import QuickPost
from opslagstavle import access as board_access
from opslagstavle.models import Category, Notice, NoticeComment
from residents.models import Resident, Role

FEED_URL = "/intern/den-hurtige/"
BOARD = "/intern/opslagstavle/"


@pytest.fixture(autouse=True)
def _open_rollouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both features open to everyone, as shipped — pinned so a re-gate does not fail these."""
    monkeypatch.setattr(dh_access, "ACCESS_ROLES", None)
    monkeypatch.setattr(board_access, "ACCESS_ROLES", None)


@pytest.fixture
def beboer(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="beboer@gahk.dk")


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    """Every (topic, data) published, recorded instead of sent.

    The test transaction never commits, so on_commit is made immediate — in core.live's namespace
    only, leaving every other on_commit in the code under test as it is. That publishing really
    waits for the commit is test_a_signal_waits_for_its_transaction's job.
    """
    sent: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(live, "_send", lambda topic, data: sent.append((topic, data)))
    monkeypatch.setattr(live, "transaction", SimpleNamespace(on_commit=lambda fn: fn()))
    return sent


def as_request(user: Resident | AnonymousUser) -> HttpRequest:
    request = HttpRequest()
    request.user = user
    request.session = SessionStore()
    return request


# --- who may listen ----------------------------------------------------------------------------


def test_a_resident_may_listen_to_a_channel_and_the_board(beboer: Resident) -> None:
    request = as_request(beboer)

    assert live.may_subscribe(request, "den-hurtige.generelt")
    assert live.may_subscribe(request, "opslagstavle")


@pytest.mark.parametrize(
    "topic",
    [
        "den-hurtige.findes-ikke",  # no such channel
        "den-hurtige",  # no channel at all
        "opslagstavle.noget",  # the board has one topic
        "ukendt",  # no feature registered it
        "den-hurtige.GENERELT",  # outside the topic alphabet
        "a." + "b" * 100,  # longer than a group name may be
    ],
)
def test_a_topic_that_does_not_exist_is_refused(beboer: Resident, topic: str) -> None:
    assert not live.may_subscribe(as_request(beboer), topic)


def test_nobody_logged_out_may_listen(db: None) -> None:
    assert not live.may_subscribe(as_request(AnonymousUser()), "den-hurtige.generelt")
    assert not live.may_subscribe(as_request(AnonymousUser()), "opslagstavle")


def test_a_restricted_channel_is_refused_like_its_page(
    monkeypatch: pytest.MonkeyPatch, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """The channel's own roles stack on the feature gate, exactly as views._channel_or_404 has it —
    a socket must not hear about messages in a channel whose page would 404."""
    secret = Channel(slug="internt", name="Internt", icon="lock", description="", roles=(Role.INSPEKTION,))
    monkeypatch.setattr(channels, "BY_SLUG", {**channels.BY_SLUG, "internt": secret})
    inspektion = make_resident(email="ins@gahk.dk", roles=(Role.INSPEKTION,))

    assert not live.may_subscribe(as_request(beboer), "den-hurtige.internt")
    assert live.may_subscribe(as_request(inspektion), "den-hurtige.internt")


def test_the_rollout_gates_close_the_topics_too(monkeypatch: pytest.MonkeyPatch, beboer: Resident) -> None:
    monkeypatch.setattr(dh_access, "ACCESS_ROLES", (Role.ADMINISTRATOR,))
    monkeypatch.setattr(board_access, "ACCESS_ROLES", (Role.ADMINISTRATOR,))

    assert not live.may_subscribe(as_request(beboer), "den-hurtige.generelt")
    assert not live.may_subscribe(as_request(beboer), "opslagstavle")


def test_a_signal_waits_for_its_transaction(
    db: None, monkeypatch: pytest.MonkeyPatch, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    """A page re-fetches the moment it hears a signal. Sent before the commit, it would read the
    old state back and then sit there looking current."""
    sent: list[str] = []
    monkeypatch.setattr(live, "_send", lambda topic, data: sent.append(topic))

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        live.publish("opslagstavle", "changed", 1)
        assert sent == []

    assert len(callbacks) == 1
    callbacks[0]()
    assert sent == ["opslagstavle"]


def test_a_failing_channel_layer_does_not_fail_the_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """The write already happened; a 500 now would only make the author post it again."""

    class Broken:
        async def group_send(self, group: str, message: dict) -> None:
            raise ConnectionError("layer down")

    monkeypatch.setattr(live, "get_channel_layer", Broken)

    live._send("opslagstavle", {"kind": "changed", "pk": 1})  # does not raise


def test_a_topic_that_cannot_be_a_group_cannot_be_published() -> None:
    with pytest.raises(ValueError):
        live.publish("ikke et emne", "changed", 1)


# --- Den Hurtige: every write signals its message -----------------------------------------------


def test_a_new_message_signals_its_channel(client: Client, beboer: Resident, signals: list) -> None:
    client.force_login(beboer)

    client.post(FEED_URL + "opret", {"content": "Kaffe?", "duration": 60, "kanal": "tv-rezz"})

    post = QuickPost.objects.get()
    assert signals == [("den-hurtige.tv-rezz", {"kind": "created", "pk": post.pk})]


def test_a_reply_a_reaction_and_a_tombstone_signal_their_message(
    client: Client, beboer: Resident, signals: list
) -> None:
    post = QuickPost.objects.create(author=beboer, content="Kaffe?")
    client.force_login(beboer)

    client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "Ja"})
    client.post(f"{FEED_URL}{post.pk}/reaktion", {"emoji": "👍"})
    QuickPost.objects.filter(pk=post.pk).update(created_at=post.created_at.replace(year=2000))
    client.post(f"{FEED_URL}{post.pk}/slet")  # past the grace period: a tombstone, so "changed"

    changed = ("den-hurtige.generelt", {"kind": "changed", "pk": post.pk})
    assert signals == [changed, changed, changed]


def test_a_message_deleted_outright_signals_the_pk_it_had(
    client: Client, beboer: Resident, signals: list
) -> None:
    """delete() clears the instance's pk, so the signal must have taken it first — or every page
    would be told that message None was deleted."""
    post = QuickPost.objects.create(author=beboer, content="Ups")
    pk = post.pk
    client.force_login(beboer)

    client.post(f"{FEED_URL}{pk}/slet")

    assert not QuickPost.objects.exists()
    assert signals == [("den-hurtige.generelt", {"kind": "deleted", "pk": pk})]


def test_a_refused_write_signals_nothing(client: Client, beboer: Resident, signals: list) -> None:
    client.force_login(beboer)

    client.post(FEED_URL + "opret", {"content": "Ingen varighed"})

    assert signals == []


# --- opslagstavlen: every write signals its notice ----------------------------------------------


def test_a_new_notice_signals_with_its_category(client: Client, beboer: Resident, signals: list) -> None:
    """The category is what lets a board filtered to another one ignore it."""
    client.force_login(beboer)

    client.post(BOARD + "opret", {"category": Category.NYT, "body": "Hej"})

    notice = Notice.objects.get()
    assert signals == [("opslagstavle", {"kind": "created", "pk": notice.pk, "category": Category.NYT})]


def test_notice_writes_each_signal_their_notice(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident], signals: list
) -> None:
    notice = Notice.objects.create(author=beboer, body="Hej", category=Category.NYT)
    client.force_login(beboer)

    client.post(f"{BOARD}{notice.pk}/rediger", {"category": Category.NYT, "body": "Hej igen"})
    client.post(f"{BOARD}{notice.pk}/reaktion", {"emoji": "👍"})
    client.post(f"{BOARD}{notice.pk}/kommentar", {"body": "Svar"})
    comment = NoticeComment.objects.get()
    client.post(f"{BOARD}kommentar/{comment.pk}/slet")
    client.force_login(make_resident(email="ins@gahk.dk", roles=(Role.INSPEKTION,)))
    client.post(f"{BOARD}{notice.pk}/fastgoer")

    changed = ("opslagstavle", {"kind": "changed", "pk": notice.pk})
    assert signals == [changed] * 5


def test_a_deleted_notice_signals_the_pk_it_had(client: Client, beboer: Resident, signals: list) -> None:
    notice = Notice.objects.create(author=beboer, body="Hej", category=Category.NYT)
    pk = notice.pk
    client.force_login(beboer)

    client.post(f"{BOARD}{pk}/slet")

    assert signals == [("opslagstavle", {"kind": "deleted", "pk": pk})]


# --- what a signal re-fetches -------------------------------------------------------------------


def test_live_cards_draws_the_asked_for_cards_and_drops_the_gone(client: Client, beboer: Resident) -> None:
    """A card missing from the answer is how a board learns it was deleted while it was not
    listening — so a gone or junk id is left out, never an error."""
    kept = Notice.objects.create(author=beboer, body="Bliver", category=Category.NYT)
    client.force_login(beboer)

    response = client.get(BOARD + "kort", {"id": [kept.pk, 999999, "junk"]})

    body = response.content.decode()
    assert response.status_code == 200
    assert f'id="opslag-{kept.pk}"' in body
    assert "opslag-999999" not in body
    assert "<html" not in body


def test_live_fragments_are_quiet_for_a_logged_out_page(client: Client, beboer: Resident) -> None:
    """204, not a redirect: fetch would follow it and hand the board a login page to read cards out
    of — and a card missing from that answer reads as deleted."""
    notice = Notice.objects.create(author=beboer, body="Hej", category=Category.NYT)

    assert client.get(BOARD + "kort", {"id": notice.pk}).status_code == 204
    assert client.get(f"{BOARD}{notice.pk}/indhold").status_code == 204


def test_live_detail_is_the_post_and_its_comments_and_404s_when_gone(
    client: Client, beboer: Resident
) -> None:
    notice = Notice.objects.create(author=beboer, body="Hej", category=Category.NYT)
    NoticeComment.objects.create(notice=notice, author=beboer, body="Et svar")
    client.force_login(beboer)

    body = client.get(f"{BOARD}{notice.pk}/indhold").content.decode()

    assert 'id="js-notice-live"' in body
    assert "Et svar" in body
    assert "<html" not in body
    form = body.split('class="comment-form"')[0].rsplit("<form", 1)[1]
    assert "data-morph-skip" in form, "a live re-draw would wipe a comment being written"
    assert client.get(f"{BOARD}999999/indhold").status_code == 404


def test_live_fragments_do_not_eat_flash_messages(client: Client, beboer: Resident) -> None:
    """A signal can fire between a POST and the page its redirect lands on. A fragment that drew
    the message queue would consume "Kommentaren er slettet." before that page could show it."""
    notice = Notice.objects.create(author=beboer, body="Hej", category=Category.NYT)
    comment = NoticeComment.objects.create(notice=notice, author=beboer, body="Væk")
    client.force_login(beboer)

    client.post(f"{BOARD}kommentar/{comment.pk}/slet")
    client.get(f"{BOARD}{notice.pk}/indhold")
    client.get(BOARD + "kort", {"id": notice.pk})

    assert "Kommentaren er slettet." in client.get(f"{BOARD}{notice.pk}").content.decode()


def test_the_pages_declare_their_topics(client: Client, beboer: Resident) -> None:
    notice = Notice.objects.create(author=beboer, body="Hej", category=Category.NYT)
    client.force_login(beboer)

    assert 'data-live="den-hurtige.tv-rezz"' in client.get(FEED_URL + "tv-rezz/").content.decode()
    assert 'data-live="opslagstavle"' in client.get(BOARD).content.decode()
    assert 'data-live="opslagstavle"' in client.get(f"{BOARD}{notice.pk}").content.decode()


def test_the_feed_polls_fast_only_without_a_live_connection(client: Client, beboer: Resident) -> None:
    client.force_login(beboer)
    post = QuickPost.objects.create(author=beboer, content="Kaffe")

    feed = client.get(FEED_URL).content.decode()
    panel = client.get(f"{FEED_URL}{post.pk}/traad", HTTP_HX_REQUEST="true").content.decode()

    trigger = "gahk-live, every 5s [!document.documentElement.hasAttribute('data-live-connected')], every 60s"
    assert f'hx-trigger="{trigger}"' in feed
    assert f'hx-trigger="{trigger}"' in panel


# --- through a real socket ----------------------------------------------------------------------


def _communicator(path: str, user: Resident | AnonymousUser) -> WebsocketCommunicator:
    communicator = WebsocketCommunicator(URLRouter(websocket_urlpatterns), path)
    communicator.scope["user"] = user
    communicator.scope["session"] = SessionStore()
    return communicator


@pytest.mark.django_db(transaction=True)
def test_a_refused_socket_is_closed_with_the_refusal_code() -> None:
    """Accepted and then closed with 4403, so the browser can tell "never" from "not now" and stop
    retrying (live.ts). A rejected handshake would look like a network failure."""

    async def run() -> dict[str, Any]:
        communicator = _communicator("/ws/live/den-hurtige.generelt/", AnonymousUser())
        connected, _ = await communicator.connect()
        assert connected
        closed = await communicator.receive_output()
        await communicator.wait()
        return closed

    assert async_to_sync(run)() == {"type": "websocket.close", "code": REFUSED}


@pytest.mark.django_db(transaction=True)
def test_a_new_message_reaches_a_listening_socket(make_resident: Callable[..., Resident]) -> None:
    """End to end: the composer's POST, through publish and the channel layer, out of the socket."""
    author = make_resident(email="a@gahk.dk")
    client = Client()
    client.force_login(author)

    async def run() -> dict[str, Any]:
        communicator = _communicator("/ws/live/den-hurtige.generelt/", author)
        connected, _ = await communicator.connect()
        assert connected
        await sync_to_async(client.post)(FEED_URL + "opret", {"content": "Kaffe?", "duration": 60})
        signal = await communicator.receive_json_from(timeout=5)
        await communicator.disconnect()
        return signal

    signal = async_to_sync(run)()

    assert signal == {"kind": "created", "pk": QuickPost.objects.get().pk}
