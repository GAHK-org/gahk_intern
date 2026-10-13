"""The shared Web Push transport: the payload, the delivery loop, the one subscribe endpoint, the
diagnostic command and the VAPID startup checks.

Everything here is *transport*, exercised without touching the network by stubbing `core.push._send`
(the single place that calls pywebpush). Who is told what — categories, preferences, vibration,
grouping — is notifications.services' business and lives in test_notifications.py; each feature's
own test module asserts its audience.

Push used to be per-feature topics (a `wants_<feature>` column per device and a subscribe endpoint
per feature). That is gone: a device subscribes ONCE, at /intern/notifikationer/enhed, and what it
is told follows the resident's settings. The tests that pinned per-topic consent went with it; what
replaced them is below — one row per device, upserted on the endpoint, scoped to the session.
"""

import base64
import datetime
import json
from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from pywebpush import WebPushException

from core import push
from core.checks import check_vapid_public_key
from core.models import PushDeliveryDay, PushSubscription
from residents.models import Resident

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc"

pytestmark = pytest.mark.django_db


@pytest.fixture
def subscribe_url() -> str:
    """The ONE subscribe endpoint. Reversed, not hardcoded: the per-feature `<feature>/abonner`
    paths it replaced are exactly the kind of literal that outlives a refactor."""
    return reverse("notifications:subscription")


@pytest.fixture
def vapid(settings: object) -> None:
    """Push configured, as in production. Without both keys `send_many` is a deliberate no-op."""
    settings.VAPID_PUBLIC_KEY = "test-public-key"  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = "test-private-key"  # type: ignore[attr-defined]
    settings.VAPID_ADMIN_EMAIL = "drift@gahk.dk"  # type: ignore[attr-defined]


def subscribe(user: Resident, endpoint: str) -> PushSubscription:
    """Register a fake device. One row per device, for everything — there are no topic flags."""
    return PushSubscription.objects.create(
        user=user,
        endpoint=endpoint,
        auth="a" * 22,
        p256dh="p" * 87,
        user_agent="pytest",
    )


def _body(**extra: object) -> str:
    """The JSON frontend/src/push.ts posts. `extra` overrides top-level keys."""
    payload: dict[str, object] = {
        "status_type": "subscribe",
        "subscription": {"endpoint": ENDPOINT, "keys": {"auth": "a" * 22, "p256dh": "p" * 87}},
        "user_agent": "pytest",
    }
    payload.update(extra)
    return json.dumps(payload)


def _post(client: Client, url: str, body: str) -> Any:  # noqa: ANN401 - the test client's response type is stub-only
    return client.post(url, data=body, content_type="application/json")


# --- payload ------------------------------------------------------------------------------------


def test_payload_carries_every_key_the_service_worker_reads() -> None:
    """app/templates/sw.js reads these by name. `open` is the notifications app's open-link (marks
    the row seen, then redirects); `url` stays as the fallback for a payload without one."""
    payload = push._payload(
        "Anders", "kaffe?", "/intern/den-hurtige/", open_url="/intern/notifikationer/aabn/x/", tag="t"
    )

    assert set(payload) == {"head", "body", "icon", "url", "open", "tag", "vibrate", "importance"}
    assert payload["head"] == "Anders"
    assert payload["url"] == "/intern/den-hurtige/"
    assert payload["open"] == "/intern/notifikationer/aabn/x/"
    assert payload["tag"] == "t"
    assert str(payload["icon"]).endswith("icons/icon-192x192.png")


def test_payload_defaults_stand_alone_vibrate_and_normal_importance() -> None:
    """No tag = the banner stacks rather than replaces; vibration and NORMAL are the defaults a
    caller that knows nothing about the resident's settings gets."""
    payload = push._payload("h", "b", "/intern/")

    assert payload["open"] == ""
    assert payload["tag"] == ""
    assert payload["vibrate"] is True
    assert payload["importance"] == 2


def test_preview_collapses_whitespace_and_clips() -> None:
    """A lock screen shows a line or two; a long message must be cut with an ellipsis, not mid-air."""
    assert push.preview("  hej\n\n  med   dig ") == "hej med dig"
    clipped = push.preview("x" * 500)
    assert len(clipped) == push.BODY_PREVIEW_CHARS
    assert clipped.endswith("…")


# --- delivery loop ------------------------------------------------------------------------------


def _raiser(status: int | None) -> WebPushException:
    """A WebPushException carrying `status`, or one with response=None as pywebpush raises for a
    connection-level failure — the case that makes a bare `exc.response.status_code` blow up."""
    if status is None:
        return WebPushException("connection refused")
    return WebPushException("failed", response=type("Response", (), {"status_code": status})())


@pytest.mark.parametrize("status", [404, 410])
def test_dispatch_drops_a_gone_subscription_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident], status: int
) -> None:
    """A 404/410 endpoint must be deleted, and must not cost the later recipients their
    notification. FCM answers 404 for an endpoint it no longer knows, so both count as gone."""
    dead = subscribe(make_resident(email="a@gahk.dk"), "https://push.example/dead")
    alive = subscribe(make_resident(email="b@gahk.dk"), "https://push.example/alive")
    seen: list[str] = []

    def fake_send(subscription: PushSubscription, body: str, ttl: int = push.TTL_SECONDS) -> None:
        seen.append(subscription.endpoint)
        if subscription.endpoint.endswith("dead"):
            raise _raiser(status)

    monkeypatch.setattr(push, "_send", fake_send)

    sent = push._dispatch(PushSubscription.objects.order_by("pk"), {"head": "h", "body": "b"})

    assert seen == ["https://push.example/dead", "https://push.example/alive"]
    assert sent == 1
    assert not PushSubscription.objects.filter(pk=dead.pk).exists()
    assert PushSubscription.objects.filter(pk=alive.pk).exists()


@pytest.mark.parametrize("status", [503, None])
def test_dispatch_keeps_a_subscription_that_failed_transiently_and_counts_the_failure(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident], status: int | None
) -> None:
    """A server error or a dropped connection is not evidence the endpoint is gone — keep the row so
    the next notification retries it, but count the failure: the admin page shows a device that
    fails every time as the one to cancel."""
    kept = subscribe(make_resident(email="a@gahk.dk"), "https://push.example/flaky")

    def fake_send(subscription: PushSubscription, body: str, ttl: int = push.TTL_SECONDS) -> None:
        raise _raiser(status)

    monkeypatch.setattr(push, "_send", fake_send)

    assert push._dispatch(PushSubscription.objects.all(), {"head": "h"}) == 0
    assert push._dispatch(PushSubscription.objects.all(), {"head": "h"}) == 0

    kept.refresh_from_db()
    assert kept.failure_count == 2
    assert kept.last_success_at is None


def test_a_success_records_when_and_resets_the_failure_count(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident]
) -> None:
    """`failure_count` is CONSECUTIVE failures. A device that recovered must not keep looking dead
    on the admin page because of last week's outage."""
    device = subscribe(make_resident(email="a@gahk.dk"), "https://push.example/one")
    PushSubscription.objects.filter(pk=device.pk).update(failure_count=5)
    monkeypatch.setattr(push, "_send", lambda sub, body, ttl=push.TTL_SECONDS: None)

    assert push._dispatch(PushSubscription.objects.all(), {"head": "h"}) == 1

    device.refresh_from_db()
    assert device.failure_count == 0
    assert device.last_success_at is not None


def test_dispatch_sends_the_payload_as_json_with_the_given_ttl(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident]
) -> None:
    """sw.js parses the body with event.data.json(), so it must arrive as a JSON string. The TTL is
    the notification's own (a Den Hurtige message's is its expiry), handed through to the push
    service so a stale push is dropped there rather than shown late."""
    subscribe(make_resident(email="a@gahk.dk"), "https://push.example/one")
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(push, "_send", lambda sub, body, ttl=push.TTL_SECONDS: calls.append((body, ttl)))

    push._dispatch(PushSubscription.objects.all(), {"head": "Hej", "url": "/intern/"}, ttl=900)

    assert len(calls) == 1
    assert json.loads(calls[0][0]) == {"head": "Hej", "url": "/intern/"}
    assert calls[0][1] == 900


def test_dispatch_defaults_to_the_fallback_ttl(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident]
) -> None:
    subscribe(make_resident(email="a@gahk.dk"), "https://push.example/one")
    ttls: list[int] = []
    monkeypatch.setattr(push, "_send", lambda sub, body, ttl=-1: ttls.append(ttl))

    push._dispatch(PushSubscription.objects.all(), {"head": "h"})

    assert ttls == [push.TTL_SECONDS]


def test_dispatch_counts_outcomes_into_todays_delivery_day(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident]
) -> None:
    """The admin statistics: delivered, failed (kept for retry) and removed (gone, deleted) are
    separate numbers, and two fan-outs on the same day ADD UP rather than overwrite."""
    subscribe(make_resident(email="a@gahk.dk"), "https://push.example/ok")
    subscribe(make_resident(email="b@gahk.dk"), "https://push.example/flaky")
    subscribe(make_resident(email="c@gahk.dk"), "https://push.example/gone")

    def fake_send(subscription: PushSubscription, body: str, ttl: int = push.TTL_SECONDS) -> None:
        if subscription.endpoint.endswith("flaky"):
            raise _raiser(503)
        if subscription.endpoint.endswith("gone"):
            raise _raiser(410)

    monkeypatch.setattr(push, "_send", fake_send)

    push._dispatch(PushSubscription.objects.all(), {"head": "h"})
    push._dispatch(PushSubscription.objects.all(), {"head": "h"})  # the gone one is no longer there

    day = PushDeliveryDay.objects.get()
    assert day.date == timezone.localdate()
    assert (day.delivered, day.failed, day.removed) == (2, 2, 1)


def test_a_dispatch_to_nobody_writes_no_delivery_day() -> None:
    """A row of zeros would be noise in the table and a write on every no-op fan-out."""
    assert push._dispatch(PushSubscription.objects.all(), {"head": "h"}) == 0
    assert not PushDeliveryDay.objects.exists()


# --- send_many: inline vs background ------------------------------------------------------------
#
# `send_many` hands the fan-out to a daemon thread by default, which is right in a request and
# silently wrong anywhere the interpreter is about to exit. The failure these guard against produces
# no error at all -- just fewer notifications than there were devices, a different number every run.


def test_send_many_delivers_inline_when_background_is_off(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident], vapid: None
) -> None:
    """A management command must have delivered before `handle()` returns: a daemon thread is
    killed at interpreter shutdown, mid-fan-out."""
    subscribe(make_resident(email="a@gahk.dk"), "https://push.example/a")
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(push, "_send", lambda sub, body, ttl=0: seen.append((sub.endpoint, ttl)))

    def fail(fn: object) -> None:
        raise AssertionError("background=False must not touch the thread path")

    monkeypatch.setattr(push, "_run_in_background", fail)

    push.send_many([(PushSubscription.objects.all(), {"head": "h"}, 60)], background=False)

    assert seen == [("https://push.example/a", 60)]


def test_send_many_runs_every_delivery_in_one_background_thread(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident], vapid: None
) -> None:
    """One notification can need two payloads (vibrating and silent). A thread per payload would be
    a thread per variant per post; they share ONE, and nothing is sent on the request thread."""
    subscribe(make_resident(email="a@gahk.dk"), "https://push.example/a")
    subscribe(make_resident(email="b@gahk.dk"), "https://push.example/b")
    queued: list[Callable[[], object]] = []
    monkeypatch.setattr(push, "_run_in_background", queued.append)
    dispatched: list[tuple[list[str], dict, int]] = []

    def fake_dispatch(subscriptions: object, payload: dict, ttl: int = 0) -> int:
        endpoints = sorted(subscriptions.values_list("endpoint", flat=True))  # type: ignore[attr-defined]
        dispatched.append((endpoints, payload, ttl))
        return len(endpoints)

    monkeypatch.setattr(push, "_dispatch", fake_dispatch)

    push.send_many(
        [
            (PushSubscription.objects.filter(endpoint__endswith="a"), {"vibrate": True}, 100),
            (PushSubscription.objects.filter(endpoint__endswith="b"), {"vibrate": False}, 100),
        ]
    )

    assert len(queued) == 1
    assert dispatched == [], "nothing may be sent before the thread runs"
    queued[0]()
    assert dispatched == [
        (["https://push.example/a"], {"vibrate": True}, 100),
        (["https://push.example/b"], {"vibrate": False}, 100),
    ]


def test_send_many_is_a_no_op_when_push_is_not_configured(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident], settings: object
) -> None:
    """The normal dev state: no VAPID keys. Features still work; nothing tries to push."""
    settings.VAPID_PUBLIC_KEY = ""  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = ""  # type: ignore[attr-defined]
    subscribe(make_resident(email="a@gahk.dk"), "https://push.example/a")
    monkeypatch.setattr(push, "_run_in_background", lambda fn: pytest.fail("no thread without keys"))
    monkeypatch.setattr(push, "_send", lambda *a, **k: pytest.fail("no send without keys"))

    push.send_many([(PushSubscription.objects.all(), {"head": "h"}, 60)])
    push.send_many([(PushSubscription.objects.all(), {"head": "h"}, 60)], background=False)


def test_send_many_with_nothing_to_send_starts_no_thread(
    monkeypatch: pytest.MonkeyPatch, vapid: None
) -> None:
    monkeypatch.setattr(push, "_run_in_background", lambda fn: pytest.fail("no thread for nothing"))

    push.send_many([])


# --- ttl_until ----------------------------------------------------------------------------------


def test_ttl_until_counts_the_seconds_left() -> None:
    remaining = push.ttl_until(timezone.now() + datetime.timedelta(hours=2))

    assert 7190 <= remaining <= 7200


def test_ttl_until_is_zero_for_the_past() -> None:
    """Zero is "do not push": an expired message must not arrive on a lock screen."""
    assert push.ttl_until(timezone.now() - datetime.timedelta(minutes=1)) == 0


def test_ttl_until_is_clamped_to_what_the_push_service_accepts() -> None:
    """FCM rejects a TTL above four weeks outright — the whole push, not just the TTL."""
    assert push.ttl_until(timezone.now() + datetime.timedelta(days=365)) == push.MAX_TTL_SECONDS


# --- the subscribe endpoint ---------------------------------------------------------------------


def test_subscribe_requires_login(client: Client, subscribe_url: str) -> None:
    response = _post(client, subscribe_url, _body())

    assert response.status_code == 302
    assert "/intern/admin/login" in response["Location"]
    assert not PushSubscription.objects.exists()


def test_the_endpoint_is_post_only(
    client: Client, make_resident: Callable[..., Resident], subscribe_url: str
) -> None:
    client.force_login(make_resident(email="a@gahk.dk"))

    assert client.get(subscribe_url).status_code == 405


def test_subscribe_binds_the_device_to_the_logged_in_user(
    client: Client, make_resident: Callable[..., Resident], subscribe_url: str
) -> None:
    """The owner comes from the session, never the request body — django-webpush's endpoint took
    its audience from the payload, so anyone could have joined the dorm's notification list."""
    user = make_resident(email="a@gahk.dk")
    client.force_login(user)

    response = _post(client, subscribe_url, _body(user="somebody-else"))

    assert response.status_code == 201
    subscription = PushSubscription.objects.get()
    assert subscription.user_id == user.pk
    assert subscription.endpoint == ENDPOINT
    assert subscription.auth == "a" * 22
    assert subscription.p256dh == "p" * 87
    assert subscription.user_agent == "pytest"


def test_resubscribing_the_same_device_updates_rather_than_duplicates(
    client: Client, make_resident: Callable[..., Resident], subscribe_url: str
) -> None:
    """The endpoint is the device identity. django-webpush keyed on every field, so a browser that
    merely changed its user-agent string silently produced a second row and a double notification.
    And a second resident logging in on a shared browser MOVES the row: it must stop pushing to the
    previous owner."""
    first = make_resident(email="a@gahk.dk")
    second = make_resident(email="b@gahk.dk")

    client.force_login(first)
    _post(client, subscribe_url, _body())
    client.force_login(second)
    _post(client, subscribe_url, _body(user_agent="pytest/2"))

    subscription = PushSubscription.objects.get()  # exactly one row, not two
    assert subscription.user_id == second.pk
    assert subscription.user_agent == "pytest/2"


def test_resubscribing_clears_the_failure_count(
    client: Client, make_resident: Callable[..., Resident], subscribe_url: str
) -> None:
    """Pressing the button again is the resident saying "this device works" — the admin page must
    not keep showing the old run of failures against fresh keys."""
    user = make_resident(email="a@gahk.dk")
    device = subscribe(user, ENDPOINT)
    PushSubscription.objects.filter(pk=device.pk).update(failure_count=7)
    client.force_login(user)

    _post(client, subscribe_url, _body())

    device.refresh_from_db()
    assert device.failure_count == 0


def test_a_stale_topic_key_is_ignored(
    client: Client, make_resident: Callable[..., Resident], subscribe_url: str
) -> None:
    """A browser running a cached copy of the per-feature push.ts still sends `topic`. A device now
    subscribes once for everything, which is what that build was asking for anyway — so it must
    keep working rather than 400, whatever topic it names."""
    client.force_login(make_resident(email="a@gahk.dk"))

    response = _post(client, subscribe_url, _body(topic="opslagstavle"))

    assert response.status_code == 201
    assert PushSubscription.objects.count() == 1


def test_unsubscribe_removes_the_device(
    client: Client, make_resident: Callable[..., Resident], subscribe_url: str
) -> None:
    """No topics left to keep the row for: unsubscribe is a delete."""
    user = make_resident(email="a@gahk.dk")
    subscribe(user, ENDPOINT)
    client.force_login(user)

    response = _post(client, subscribe_url, _body(status_type="unsubscribe"))

    assert response.status_code == 202
    assert not PushSubscription.objects.exists()


def test_unsubscribe_cannot_remove_another_residents_device(
    client: Client, make_resident: Callable[..., Resident], subscribe_url: str
) -> None:
    """Endpoints are guessable-ish strings that travel in push traffic; deleting must be scoped to
    the requesting user so a replayed endpoint cannot silence someone else's phone."""
    owner = make_resident(email="a@gahk.dk")
    subscribe(owner, ENDPOINT)
    client.force_login(make_resident(email="b@gahk.dk"))

    response = _post(client, subscribe_url, _body(status_type="unsubscribe"))

    assert response.status_code == 202  # nothing to report to the caller
    assert PushSubscription.objects.filter(user=owner).exists()  # but the owner's device survives


@pytest.mark.parametrize(
    ("owner", "expected"),
    [("me", "mine"), ("someone", "other"), (None, "none")],
)
def test_status_says_whose_the_endpoint_is(
    client: Client,
    make_resident: Callable[..., Resident],
    subscribe_url: str,
    owner: str | None,
    expected: str,
) -> None:
    """The browser only knows it holds *a* subscription, not that the server still has it — an
    administrator may have cancelled it, or another resident may have taken over the browser. The
    settings page asks, and shows "not subscribed" rather than lying."""
    me = make_resident(email="a@gahk.dk")
    if owner == "me":
        subscribe(me, ENDPOINT)
    elif owner == "someone":
        subscribe(make_resident(email="b@gahk.dk"), ENDPOINT)
    client.force_login(me)

    response = _post(client, subscribe_url, _body(status_type="status"))

    assert response.status_code == 200
    assert response.json() == {"state": expected}


def test_status_and_unsubscribe_need_no_keys(
    client: Client, make_resident: Callable[..., Resident], subscribe_url: str
) -> None:
    """Both only identify the device by its endpoint; requiring keys would break the status probe
    of a browser that has already thrown its subscription away."""
    user = make_resident(email="a@gahk.dk")
    subscribe(user, ENDPOINT)
    client.force_login(user)
    keyless = {"endpoint": ENDPOINT}

    assert _post(client, subscribe_url, _body(status_type="status", subscription=keyless)).json() == {
        "state": "mine"
    }
    assert (
        _post(client, subscribe_url, _body(status_type="unsubscribe", subscription=keyless)).status_code
        == 202
    )
    assert not PushSubscription.objects.exists()


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        json.dumps(["a", "list"]),
        json.dumps({"status_type": "nonsense"}),
        _body(subscription={"endpoint": ENDPOINT}),  # subscribe without its encryption keys
        _body(subscription={"endpoint": "not a url", "keys": {"auth": "a", "p256dh": "p"}}),
        _body(subscription={"endpoint": ENDPOINT, "keys": {"auth": "a" * 101, "p256dh": "p"}}),
    ],
)
def test_subscribe_rejects_malformed_payloads(
    client: Client, make_resident: Callable[..., Resident], subscribe_url: str, body: str
) -> None:
    """Untrusted input on a login-gated endpoint: anything malformed is a 400, never a 500 and
    never a half-written row."""
    client.force_login(make_resident(email="a@gahk.dk"))

    response = _post(client, subscribe_url, body)

    assert response.status_code == 400
    assert not PushSubscription.objects.exists()


# --- send_test_push -----------------------------------------------------------------------------


def test_send_test_push_reports_each_device(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident], vapid: None
) -> None:
    """The diagnostic command bypasses `_dispatch` on purpose (its job is to hide errors), and
    ignores the resident's notification settings — it tests the device, not a category."""
    user = make_resident(email="anton@gahk.dk")
    subscribe(user, "https://push.example/ok")
    subscribe(user, "https://push.example/broken")
    bodies: list[dict] = []

    def fake_send(subscription: PushSubscription, body: str, ttl: int = push.TTL_SECONDS) -> None:
        bodies.append(json.loads(body))
        if subscription.endpoint.endswith("broken"):
            raise _raiser(410)

    monkeypatch.setattr(push, "_send", fake_send)
    out = StringIO()

    with pytest.raises(CommandError, match="1 of 2"):
        call_command("send_test_push", "ANTON@gahk.dk", stdout=out)

    assert "accepted by the push service" in out.getvalue()
    assert "FAILED (410)" in out.getvalue()
    assert bodies[0]["url"] == "/intern/notifikationer/"
    # Diagnostic, not delivery: a dead endpoint is reported, not cleaned up.
    assert PushSubscription.objects.count() == 2


def test_send_test_push_succeeds_without_a_topic(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident], vapid: None
) -> None:
    """There is one subscription per device now, so the command takes only the email."""
    subscribe(make_resident(email="anton@gahk.dk"), "https://push.example/ok")
    monkeypatch.setattr(push, "_send", lambda sub, body, ttl=push.TTL_SECONDS: None)
    out = StringIO()

    call_command("send_test_push", "anton@gahk.dk", stdout=out)

    assert "All devices accepted" in out.getvalue()


def test_send_test_push_no_longer_takes_a_topic(make_resident: Callable[..., Resident], vapid: None) -> None:
    subscribe(make_resident(email="anton@gahk.dk"), "https://push.example/ok")

    with pytest.raises(CommandError):
        call_command("send_test_push", "anton@gahk.dk", "--topic", "den_hurtige")


def test_send_test_push_explains_a_resident_without_devices(
    make_resident: Callable[..., Resident], vapid: None
) -> None:
    make_resident(email="anton@gahk.dk")

    with pytest.raises(CommandError, match="no subscribed devices"):
        call_command("send_test_push", "anton@gahk.dk", stdout=StringIO())


def test_send_test_push_refuses_without_vapid_keys(
    make_resident: Callable[..., Resident], settings: object
) -> None:
    settings.VAPID_PUBLIC_KEY = ""  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = ""  # type: ignore[attr-defined]
    subscribe(make_resident(email="anton@gahk.dk"), "https://push.example/ok")

    with pytest.raises(CommandError, match="VAPID"):
        call_command("send_test_push", "anton@gahk.dk", stdout=StringIO())


# --- VAPID configuration ------------------------------------------------------------------------


def _vapid_pair() -> tuple[str, str]:
    """A throwaway P-256 pair in the raw base64url form the app expects. Generated rather than
    hardcoded so the tests never carry key material, real or otherwise."""
    key = ec.generate_private_key(ec.SECP256R1())
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()  # noqa: E731
    point = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64(point), b64(key.private_numbers().private_value.to_bytes(32, "big"))


GOOD_PUBLIC, GOOD_PRIVATE = _vapid_pair()
OTHER_PUBLIC, OTHER_PRIVATE = _vapid_pair()
# 65 bytes with the right 0x04 tag, but the coordinates are not on the curve. Passes a pure shape
# check and is then rejected by the browser's push service as an opaque failure.
OFF_CURVE = base64.urlsafe_b64encode(b"\x04" + b"\x01" * 64).rstrip(b"=").decode()
# The body of a public_key.pem — SPKI DER, not the raw point. The most tempting wrong value.
SPKI_DER = (
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgoXFqQdn2xSbAJlFjidkDqZ50LpS"
    "7q7l91RkEPpL3e6yazihJmXw6JUEGTQROvV0MowPIRtZUXkRfP1XIf_R8g=="
)


@pytest.mark.parametrize(
    ("public_key", "private_key", "expected_id"),
    [
        (GOOD_PUBLIC, GOOD_PRIVATE, None),
        ("", "", None),  # unset: push disabled on purpose, not an error
        ("not base64!!", GOOD_PRIVATE, "core.E001"),
        (SPKI_DER, GOOD_PRIVATE, "core.E002"),
        (GOOD_PUBLIC, "", "core.E003"),
        (OFF_CURVE, GOOD_PRIVATE, "core.E004"),
        (GOOD_PUBLIC, "tooshort", "core.E005"),
        # Regenerating the pair and updating only one env var — silent, and fatal to every push.
        (GOOD_PUBLIC, OTHER_PRIVATE, "core.E006"),
    ],
)
def test_vapid_configuration_is_validated_at_startup(
    settings: object, public_key: str, private_key: str, expected_id: str | None
) -> None:
    """A wrong key pair is invisible server-side — the page renders, the button appears, and only
    the browser refuses, as a generic AbortError indistinguishable from being offline. The IDs are
    `core.E00x`: they were `den_hurtige.E00x` before push became shared infrastructure."""
    settings.VAPID_PUBLIC_KEY = public_key  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = private_key  # type: ignore[attr-defined]

    errors = check_vapid_public_key(None)

    assert [e.id for e in errors] == ([expected_id] if expected_id else [])
