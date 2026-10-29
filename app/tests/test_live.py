"""Live updates (core.live): who may listen to which topic, and which writes signal.

The signal is the whole contract between a write and every open page, so each write view is asserted
to send one. A missing signal does not fail loudly anywhere else: the page just goes quiet until its
60s backstop poll, which nobody would notice in review and everybody would notice in use.

A few tests go through a real WebSocket conversation with the consumer to prove the parts fit — the consumer's
refusal, and a view's write arriving on a subscribed socket. Those are transactional because the
consumer reads the database from a worker thread, which cannot see an uncommitted test transaction.
"""

import datetime
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from asgiref.testing import ApplicationCommunicator
from channels.routing import URLRouter
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpRequest
from django.test import Client
from django.utils import timezone

from config.routing import websocket_urlpatterns
from core import live
from core.consumers import REFUSED
from den_hurtige import access as dh_access
from den_hurtige import channels
from den_hurtige.channels import Channel
from den_hurtige.models import QuickComment, QuickPost
from events import access as events_access
from events.models import Event, EventInvite, Visibility
from notifications import categories
from notifications import services as notification_services
from notifications.models import Notification
from opslagstavle import access as board_access
from opslagstavle.models import Category, Notice, NoticeComment
from reparationer.models import RepairTask
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

    def record(batch: list[tuple[str, dict[str, object]]]) -> None:
        for topic, data in batch:
            assert data.pop("topic") == topic, "a signal must name its own topic; the page filters on it"
            sent.append((topic, data))

    monkeypatch.setattr(live, "_send", record)
    monkeypatch.setattr(live, "transaction", SimpleNamespace(on_commit=lambda fn: fn()))
    return sent


def features(sent: list[tuple[str, dict[str, object]]]) -> list[tuple[str, dict[str, object]]]:
    """What a feature's own topics heard. A reply or a reaction also notifies somebody, and the
    bell's signal for that is test_the_bell's business, not the feature's."""
    return [(topic, data) for topic, data in sent if not topic.startswith("notifikationer.")]


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
    monkeypatch.setattr(live, "_send", lambda batch: sent.extend(topic for topic, _ in batch))

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

    live._send([("opslagstavle", {"kind": "changed", "pk": 1})])  # does not raise


def test_a_topic_that_cannot_be_a_group_is_dropped_not_raised(
    signals: list, caplog: pytest.LogCaptureFixture
) -> None:
    """publish runs AFTER the caller's write: raising here would 500 a message that was already
    saved, and the author would post it again. It is a programming error, so it is logged loudly —
    and caught by the next test, not in production."""
    live.publish_many(["ikke et emne", "opslagstavle"], "changed", 1)

    assert signals == [("opslagstavle", {"kind": "changed", "pk": 1})], "the good topic still goes"
    assert "ikke et emne" in caplog.text


def test_every_channel_slug_makes_a_valid_topic() -> None:
    """Where a bad topic is meant to be caught: a channel whose slug cannot be a topic (an æ, an
    uppercase letter, an underscore) would have its signals dropped with only a log line to say so."""
    bad = [c.slug for c in channels.CHANNELS if not live.valid_topic(f"den-hurtige.{c.slug}")]
    assert bad == []


def test_one_notification_signals_every_inbox_in_one_hand_over(
    monkeypatch: pytest.MonkeyPatch, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """A house-wide notification reaches ~100 inboxes. One synchronous round trip to the channel
    layer for each, one after another on the author's request, is what this batches away."""
    others = [make_resident(email=f"r{i}@gahk.dk") for i in range(3)]
    batches: list[list[str]] = []
    monkeypatch.setattr(live, "_send", lambda batch: batches.append([topic for topic, _ in batch]))
    monkeypatch.setattr(live, "transaction", SimpleNamespace(on_commit=lambda fn: fn()))

    notification_services.notify(
        [beboer.pk, *(o.pk for o in others)], category=categories.GENERAL, title="Hej", url="/"
    )

    assert len(batches) == 1
    assert sorted(batches[0]) == sorted(f"notifikationer.{r.pk}" for r in [beboer, *others])


# --- Den Hurtige: every write signals its message -----------------------------------------------


def test_a_new_message_signals_its_channel(client: Client, beboer: Resident, signals: list) -> None:
    client.force_login(beboer)

    client.post(FEED_URL + "opret", {"content": "Kaffe?", "duration": 60, "kanal": "tv-rezz"})

    post = QuickPost.objects.get()
    assert features(signals) == [("den-hurtige.tv-rezz", {"kind": "created", "pk": post.pk})]


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
    assert features(signals) == [changed, changed, changed]


def test_a_reaction_on_a_reply_signals_the_message_it_answers(
    client: Client, beboer: Resident, signals: list
) -> None:
    """Every signal names a MESSAGE (den_hurtige.live): the thread panel open on it is keyed by
    the message, and that is what has to refresh when one of its replies is reacted to."""
    post = QuickPost.objects.create(author=beboer, content="Kaffe?")
    reply = QuickComment.objects.create(post=post, author=beboer, content="Ja")
    client.force_login(beboer)

    client.post(f"{FEED_URL}svar/{reply.pk}/reaktion", {"emoji": "👍"})

    assert features(signals) == [("den-hurtige.generelt", {"kind": "changed", "pk": post.pk})]


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
    assert features(signals) == [("den-hurtige.generelt", {"kind": "deleted", "pk": pk})]


def test_a_refused_write_signals_nothing(client: Client, beboer: Resident, signals: list) -> None:
    client.force_login(beboer)

    client.post(FEED_URL + "opret", {"content": "Ingen varighed"})

    assert features(signals) == []


# --- opslagstavlen: every write signals its notice ----------------------------------------------


def test_a_new_notice_signals_with_its_category(client: Client, beboer: Resident, signals: list) -> None:
    """The category is what lets a board filtered to another one ignore it."""
    client.force_login(beboer)

    client.post(BOARD + "opret", {"category": Category.NYT, "body": "Hej"})

    notice = Notice.objects.get()
    assert features(signals) == [
        ("opslagstavle", {"kind": "created", "pk": notice.pk, "category": Category.NYT})
    ]


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
    assert features(signals) == [changed] * 5


def test_a_deleted_notice_signals_the_pk_it_had(client: Client, beboer: Resident, signals: list) -> None:
    notice = Notice.objects.create(author=beboer, body="Hej", category=Category.NYT)
    pk = notice.pk
    client.force_login(beboer)

    client.post(f"{BOARD}{pk}/slet")

    assert features(signals) == [("opslagstavle", {"kind": "deleted", "pk": pk})]


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

    trigger = "gahk-live, every 5s [!this.closest('[data-live-on]')], every 60s"
    assert f'hx-trigger="{trigger}"' in feed
    assert f'hx-trigger="{trigger}"' in panel


# --- through a real socket ----------------------------------------------------------------------


class Socket(ApplicationCommunicator):  # type: ignore[misc]
    """A browser's end of one socket, talking ASGI to the real router and consumer.

    Not channels.testing.WebsocketCommunicator, which this used to be: importing anything from
    channels.testing runs that package's __init__, which imports Daphne for its live-server test
    case. Daphne is not a dependency since uvicorn replaced it (DEPLOY.md §4), so the import only
    worked on a machine with a stale virtualenv — and failed collection in CI. This is the sliver of
    it these tests use, on asgiref's communicator, which is what Channels' own is built on.
    """

    def __init__(self, topics: list[str], user: Resident | AnonymousUser) -> None:
        query = "&".join(f"t={topic}" for topic in topics)
        scope = {
            "type": "websocket",
            "path": "/ws/live/",
            "query_string": query.encode(),
            "headers": [],
            "subprotocols": [],
            "user": user,
            "session": SessionStore(),
        }
        super().__init__(URLRouter(websocket_urlpatterns), scope)

    async def connect(self, timeout: float = 5) -> tuple[bool, object]:
        """(True, subprotocol) once accepted; (False, close code) if refused at the handshake."""
        await self.send_input({"type": "websocket.connect"})
        response = await self.receive_output(timeout)
        if response["type"] == "websocket.accept":
            return True, response.get("subprotocol")
        return False, response.get("code", 1000)

    async def receive_json_from(self, timeout: float = 5) -> Any:  # noqa: ANN401
        response = await self.receive_output(timeout)
        assert response["type"] == "websocket.send", response
        return json.loads(response["text"])

    async def disconnect(self, code: int = 1000, timeout: float = 5) -> None:
        await self.send_input({"type": "websocket.disconnect", "code": code})
        await self.wait(timeout)


@pytest.mark.django_db(transaction=True)
def test_a_refused_socket_is_closed_with_the_refusal_code() -> None:
    """Accepted and then closed with 4403, so the browser can tell "never" from "not now" and stop
    retrying (live.ts). A rejected handshake would look like a network failure."""

    async def run() -> dict[str, Any]:
        communicator = Socket(["den-hurtige.generelt"], AnonymousUser())
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
        communicator = Socket(["den-hurtige.generelt"], author)
        connected, _ = await communicator.connect()
        assert connected
        assert (await communicator.receive_json_from())["kind"] == "subscribed"
        await sync_to_async(client.post)(FEED_URL + "opret", {"content": "Kaffe?", "duration": 60})
        signal = await communicator.receive_json_from(timeout=5)
        await communicator.disconnect()
        return signal

    signal = async_to_sync(run)()

    assert signal == {"topic": "den-hurtige.generelt", "kind": "created", "pk": QuickPost.objects.get().pk}


@pytest.mark.django_db(transaction=True)
def test_one_socket_joins_every_topic_it_may_and_skips_the_rest(
    make_resident: Callable[..., Resident],
) -> None:
    """A page asks for several topics on one socket. One it may not hear (somebody else's inbox) is
    left out, while the others stay live — and nothing published there reaches this socket.

    The first message says which topics were joined. That is what lets a page whose OWN topic was
    the one skipped keep its fallback poll (live.ts marks data-live-on only on joined elements)
    instead of reading "socket open" as "I am live" and waiting a minute per update."""
    me = make_resident(email="a@gahk.dk")
    other = make_resident(email="b@gahk.dk")

    async def run() -> tuple[dict[str, Any], bool, dict[str, Any]]:
        communicator = Socket(["opslagstavle", f"notifikationer.{other.pk}"], me)
        connected, _ = await communicator.connect()
        assert connected
        joined = await communicator.receive_json_from()
        await sync_to_async(live._send)([(f"notifikationer.{other.pk}", {"topic": "x"})])
        quiet = await communicator.receive_nothing(timeout=0.2)
        await sync_to_async(live._send)(
            [("opslagstavle", {"topic": "opslagstavle", "kind": "changed", "pk": 1})]
        )
        heard = await communicator.receive_json_from(timeout=5)
        await communicator.disconnect()
        return heard, quiet, joined

    heard, quiet, joined = async_to_sync(run)()

    assert joined == {"kind": "subscribed", "topics": ["opslagstavle"]}
    assert quiet, "a signal for somebody else's inbox reached this socket"
    assert heard == {"topic": "opslagstavle", "kind": "changed", "pk": 1}


# --- the bell -----------------------------------------------------------------------------------


def test_only_your_own_inbox_may_be_heard(beboer: Resident, make_resident: Callable[..., Resident]) -> None:
    other = make_resident(email="anden@gahk.dk")

    assert live.may_subscribe(as_request(beboer), f"notifikationer.{beboer.pk}")
    assert not live.may_subscribe(as_request(beboer), f"notifikationer.{other.pk}")
    assert not live.may_subscribe(as_request(beboer), "notifikationer")


def test_the_bell_hears_a_notification_arrive_and_be_read(
    beboer: Resident, make_resident: Callable[..., Resident], signals: list
) -> None:
    """Arriving and being read both move the badge — reading on the phone clears the laptop's."""
    other = make_resident(email="anden@gahk.dk")

    notification_services.notify([beboer.pk, other.pk], category=categories.GENERAL, title="Hej", url="/")
    arrived = sorted(topic for topic, _ in signals)
    signals.clear()
    notification_services.mark_seen(Notification.objects.filter(recipient=beboer))

    assert arrived == sorted([f"notifikationer.{beboer.pk}", f"notifikationer.{other.pk}"])
    assert signals == [(f"notifikationer.{beboer.pk}", {"kind": "changed", "pk": None})]


def test_marking_nothing_seen_signals_nothing(beboer: Resident, signals: list) -> None:
    notification_services.mark_seen(Notification.objects.filter(recipient=beboer))

    assert signals == []


def test_the_bell_declares_its_topic_and_its_badge_can_be_reread(client: Client, beboer: Resident) -> None:
    client.force_login(beboer)
    page = client.get(BOARD).content.decode()
    notification_services.notify([beboer.pk], category=categories.GENERAL, title="Hej", url="/")
    badge = client.get("/intern/notifikationer/taeller").content.decode()

    assert f'data-live="notifikationer.{beboer.pk}"' in page
    assert 'id="js-notif-badge"' in badge
    assert ">1<" in badge
    assert "<html" not in badge
    client.logout()
    assert client.get("/intern/notifikationer/taeller").status_code == 204


# --- the repair board ---------------------------------------------------------------------------

REPAIRS = "/intern/reparationer/"


def test_every_repair_write_signals_the_board(
    client: Client, make_resident: Callable[..., Resident], signals: list
) -> None:
    client.force_login(make_resident(email="rep@gahk.dk", roles=(Role.REPPER,)))
    client.post(REPAIRS + "opret", {"title": "Utæt vandhane", "location": "", "description": ""})
    task = RepairTask.objects.get()
    pk = task.pk
    client.post(f"{REPAIRS}{pk}/status", {"status": RepairTask.Status.I_GANG})
    client.post(f"{REPAIRS}{pk}/ansvarlig", {"responsible": RepairTask.Responsible.REPPER})
    client.post(f"{REPAIRS}{pk}/kommentar", {"body": "Bestilt reservedel"})
    client.post(f"{REPAIRS}{pk}/slet")

    changed = ("reparationer", {"kind": "changed", "pk": pk})
    assert features(signals) == [
        ("reparationer", {"kind": "created", "pk": pk}),
        changed,
        changed,
        changed,
        ("reparationer", {"kind": "deleted", "pk": pk}),
    ]


def test_the_board_answers_htmx_with_the_board_alone(client: Client, beboer: Resident) -> None:
    """What a live signal re-reads: the same view and filters, without the page or its messages."""
    task = RepairTask.objects.create(reported_by=beboer, title="Utæt vandhane")
    client.force_login(beboer)

    page = client.get(REPAIRS).content.decode()
    board = client.get(REPAIRS, HTTP_HX_REQUEST="true").content.decode()

    assert 'data-live="reparationer" data-live-region' in page
    assert f'id="kanban-task-{task.pk}"' in board, "without ids the morph cannot move a card"
    assert 'id="js-kanban"' in board
    assert "<html" not in board
    assert live.may_subscribe(as_request(beboer), "reparationer")


# --- an event page ------------------------------------------------------------------------------

EVENTS = "/intern/begivenheder/"


def make_event(organiser: Resident, **extra: object) -> Event:
    starts = (timezone.now() + datetime.timedelta(days=3)).replace(second=0, microsecond=0)
    return Event.objects.create(organiser=organiser, title="Fællesspisning", starts_at=starts, **extra)


@pytest.fixture
def _open_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events_access, "ACCESS_ROLES", None)


@pytest.mark.usefixtures("_open_events")
def test_an_event_topic_is_heard_by_exactly_who_may_see_the_event(
    beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """visible_to decides, as it does for the page: a private event's signals reach its invitees
    and hosts, and nobody else learns that anything is happening on it."""
    host = make_resident(email="host@gahk.dk")
    open_event = make_event(host)
    private = make_event(host, visibility=Visibility.KUN_INVITEREDE)

    assert live.may_subscribe(as_request(beboer), f"begivenheder.{open_event.pk}")
    assert not live.may_subscribe(as_request(beboer), f"begivenheder.{private.pk}")
    assert live.may_subscribe(as_request(host), f"begivenheder.{private.pk}")
    EventInvite.objects.create(event=private, resident=beboer)
    assert live.may_subscribe(as_request(beboer), f"begivenheder.{private.pk}")
    assert not live.may_subscribe(as_request(beboer), "begivenheder.999999")


@pytest.mark.usefixtures("_open_events")
def test_answering_and_commenting_signal_the_event(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident], signals: list
) -> None:
    event = make_event(make_resident(email="host@gahk.dk"))
    client.force_login(beboer)

    client.post(f"{EVENTS}{event.pk}/svar", {"svar": "ja"})
    client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "Kan man tage børn med?"})

    changed = (f"begivenheder.{event.pk}", {"kind": "changed", "pk": event.pk})
    assert features(signals) == [changed, changed]


@pytest.mark.usefixtures("_open_events")
def test_cancelling_and_deleting_signal_the_event(client: Client, beboer: Resident, signals: list) -> None:
    cancelled = make_event(beboer)
    deleted = make_event(beboer)
    pk = deleted.pk
    client.force_login(beboer)

    client.post(f"{EVENTS}{cancelled.pk}/aflys")
    client.post(f"{EVENTS}{pk}/slet")

    assert features(signals) == [
        (f"begivenheder.{cancelled.pk}", {"kind": "changed", "pk": cancelled.pk}),
        (f"begivenheder.{pk}", {"kind": "deleted", "pk": pk}),
    ]


@pytest.mark.usefixtures("_open_events")
def test_the_event_page_is_one_live_region(client: Client, beboer: Resident) -> None:
    event = make_event(beboer)
    client.force_login(beboer)

    page = client.get(f"{EVENTS}{event.pk}").content.decode()
    region = client.get(f"{EVENTS}{event.pk}/indhold").content.decode()

    assert f'data-live="begivenheder.{event.pk}" data-live-region' in page
    assert 'id="js-event-live"' in region
    assert f'id="rsvp-{event.pk}"' in region, "the answer panel is what moves most"
    assert "<html" not in region
    form = region.split('class="comment-form"')[0].rsplit("<form", 1)[1]
    assert "data-morph-skip" in form, "a live re-draw would wipe a comment being written"
    assert client.get(f"{EVENTS}999999/indhold").status_code == 404
    client.logout()
    assert client.get(f"{EVENTS}{event.pk}/indhold").status_code == 204


# --- Den Hurtige's channel picker ---------------------------------------------------------------


def test_the_channel_menu_listens_to_every_channel_it_lists(client: Client, beboer: Resident) -> None:
    QuickPost.objects.create(author=beboer, content="Fodbold?", channel="sportsmann")
    client.force_login(beboer)

    page = client.get(FEED_URL).content.decode()
    menu = client.get(FEED_URL + "kanaler", {"kanal": "generelt"}).content.decode()

    every_channel = " ".join(f"den-hurtige.{c.slug}" for c in channels.CHANNELS)
    assert f'data-live="{every_channel}"' in page
    assert 'id="js-channel-menu"' in menu
    assert '<span class="channel-count">1</span>' in menu
    assert "<html" not in menu
    assert client.get(FEED_URL + "kanaler", {"kanal": "findes-ikke"}).status_code == 204


def test_the_channel_menu_leaves_out_a_channel_this_reader_may_not_open(
    monkeypatch: pytest.MonkeyPatch, client: Client, beboer: Resident
) -> None:
    """Its topic would be refused anyway; it must not be asked for, nor its count shown."""
    secret = Channel(slug="internt", name="Internt", icon="lock", description="", roles=(Role.INSPEKTION,))
    monkeypatch.setattr(channels, "CHANNELS", (*channels.CHANNELS, secret))
    monkeypatch.setattr(channels, "BY_SLUG", {c.slug: c for c in channels.CHANNELS})
    client.force_login(beboer)

    menu = client.get(FEED_URL + "kanaler", {"kanal": "generelt"}).content.decode()

    assert "den-hurtige.internt" not in menu
    assert "Internt" not in menu


def test_kanaler_can_never_be_a_channel_slug() -> None:
    """It is a fixed URL segment now; a channel by that name would be unreachable (checks.E008)."""
    assert "kanaler" in channels.RESERVED_SLUGS


# --- review follow-ups --------------------------------------------------------------------------


def test_the_repair_board_varies_on_hx_request(client: Client, beboer: Resident) -> None:
    """One URL answers with the page or the bare board. Without Vary a browser can serve the cached
    bare board on Back: an unstyled kanban with nothing around it."""
    client.force_login(beboer)

    for response in (client.get(REPAIRS), client.get(REPAIRS, HTTP_HX_REQUEST="true")):
        assert "HX-Request" in response.headers.get("Vary", "")


def test_the_channel_menu_is_held_until_the_picker_opens(client: Client, beboer: Resident) -> None:
    """Rendered with data-live-busy, so a closed picker holds signals instead of fetching counts
    nobody can see; feed.ts lifts it on open."""
    client.force_login(beboer)

    page = client.get(FEED_URL).content.decode()

    menu = page.split('id="js-channel-menu"')[1].split(">")[0]
    assert "data-live-busy" in menu


def test_live_cards_says_what_was_posted_since_a_cursor(client: Client, beboer: Resident) -> None:
    """A board that slept through new posts was never signalled them; on reconnect it asks."""
    old = Notice.objects.create(author=beboer, body="Gammel", category=Category.NYT)
    news = Notice.objects.create(author=beboer, body="Ny", category=Category.NYT)
    other = Notice.objects.create(author=beboer, body="Anden kategori", category=Category.BEGIVENHED)
    client.force_login(beboer)

    board = client.get(BOARD).content.decode()
    everything = client.get(BOARD + "kort", {"siden": old.pk}).content.decode()
    filtered = client.get(BOARD + "kort", {"siden": old.pk, "kategori": Category.NYT}).content.decode()
    plain = client.get(BOARD + "kort", {"id": old.pk}).content.decode()

    assert f'data-newest="{other.pk}"' in board
    assert f'data-pks="{other.pk},{news.pk}"' in everything
    assert f'data-pks="{news.pk}"' in filtered
    assert "js-board-new-since" not in plain, "only a resync asks"


def test_a_card_says_its_category(client: Client, beboer: Resident) -> None:
    """How the board tells a re-fetched card has moved off a filtered view (opslagstavle-live.ts)."""
    notice = Notice.objects.create(author=beboer, body="Hej", category=Category.BEGIVENHED)
    client.force_login(beboer)

    cards = client.get(BOARD + "kort", {"id": notice.pk}).content.decode()

    assert f'id="opslag-{notice.pk}" data-category="{Category.BEGIVENHED}"' in cards
