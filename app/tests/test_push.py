"""The shared Web Push stack: the delivery loop, the subscribe endpoint, per-topic consent, and the
VAPID startup checks.

Moved out of test_den_hurtige.py when push moved to core. The split follows the code: what is here
is *transport* and *consent*, exercised without touching the network by stubbing `core.push._send`
(the single place that calls pywebpush). Who gets notified about what stays with each feature's own
test module, because that is policy.

Per-topic consent is the reason this refactor happened, so it is tested hardest: a browser has
exactly one push endpoint for both features, and the ways that can go wrong (subscribing to one
topic clearing the other, or unsubscribing from one killing both) are silent from the server's side
and undiagnosable from the user's.
"""

import base64
import json
from collections.abc import Callable

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import Client
from pywebpush import WebPushException

from core import push
from core.checks import check_vapid_public_key
from core.models import PushSubscription
from den_hurtige import access as den_hurtige_access
from opslagstavle import access as opslagstavle_access
from residents.models import Resident, Role

DEN_HURTIGE_SUBSCRIBE = "/intern/den-hurtige/abonner"
FEED_URL = "/intern/den-hurtige/"

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def rollout_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift BOTH features' staged-rollout gates, except where a test re-enables one.

    This file is about the push layer's own semantics — per-topic consent, delivery, dead endpoints
    — not about who may reach a feature. Both subscribe views sit behind their feature's gate, so
    without this every test here would have to hand its resident a role and would then be testing
    something other than what it says.

    Both are named: opslagstavlen gained a gate of its own, and two identically named ACCESS_ROLES
    constants are exactly the setup where patching one silently leaves the other on.
    """
    monkeypatch.setattr(den_hurtige_access, "ACCESS_ROLES", None)
    monkeypatch.setattr(opslagstavle_access, "ACCESS_ROLES", None)


def subscribe(user: Resident, endpoint: str, **topics: bool) -> PushSubscription:
    """Register a fake device. Topic flags are explicit so a test never depends on the defaults."""
    return PushSubscription.objects.create(
        user=user,
        endpoint=endpoint,
        auth="a" * 22,
        p256dh="p" * 87,
        user_agent="pytest",
        wants_den_hurtige=topics.get("den_hurtige", True),
        wants_opslagstavle=topics.get("opslagstavle", False),
    )


def _subscribe_body(**extra: object) -> str:
    payload: dict[str, object] = {
        "status_type": "subscribe",
        "topic": "den_hurtige",
        "subscription": {
            "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
            "keys": {"auth": "a" * 22, "p256dh": "p" * 87},
        },
        "user_agent": "pytest",
    }
    payload.update(extra)
    return json.dumps(payload)


# --- delivery loop ------------------------------------------------------------------------------


def _raiser(status: int | None) -> WebPushException:
    """A WebPushException carrying `status`, or one with response=None as pywebpush raises for a
    connection-level failure — the case that makes a bare `exc.response.status_code` blow up."""
    if status is None:
        return WebPushException("connection refused")
    return WebPushException("failed", response=type("Response", (), {"status_code": status})())


def test_dispatch_drops_a_gone_subscription_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident]
) -> None:
    """A 410 endpoint must be deleted, and must not cost the later recipients their notification."""
    dead = subscribe(make_resident(email="a@gahk.dk"), "https://push.example/dead")
    alive = subscribe(make_resident(email="b@gahk.dk"), "https://push.example/alive")
    seen: list[str] = []

    def fake_send(subscription: PushSubscription, body: str) -> None:
        seen.append(subscription.endpoint)
        if subscription.endpoint.endswith("dead"):
            raise _raiser(410)

    monkeypatch.setattr(push, "_send", fake_send)

    sent = push._dispatch(push.subscribers("den_hurtige").order_by("pk"), {"head": "h", "body": "b"})

    assert seen == ["https://push.example/dead", "https://push.example/alive"]
    assert sent == 1
    assert not PushSubscription.objects.filter(pk=dead.pk).exists()
    assert PushSubscription.objects.filter(pk=alive.pk).exists()


@pytest.mark.parametrize("status", [503, None])
def test_dispatch_keeps_a_subscription_that_failed_transiently(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident], status: int | None
) -> None:
    """A server error or a dropped connection is not evidence the endpoint is gone — keep the row so
    the next post retries it."""
    kept = subscribe(make_resident(email="a@gahk.dk"), "https://push.example/flaky")

    def fake_send(subscription: PushSubscription, body: str) -> None:
        raise _raiser(status)

    monkeypatch.setattr(push, "_send", fake_send)

    assert push._dispatch(push.subscribers("den_hurtige"), {"head": "h", "body": "b"}) == 0
    assert PushSubscription.objects.filter(pk=kept.pk).exists()


def test_dispatch_sends_the_payload_as_json(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident]
) -> None:
    """sw.js parses the body with event.data.json(), so it must arrive as a JSON string."""
    subscribe(make_resident(email="a@gahk.dk"), "https://push.example/one")
    bodies: list[str] = []
    monkeypatch.setattr(push, "_send", lambda sub, body: bodies.append(body))

    push._dispatch(push.subscribers("den_hurtige"), {"head": "Hej", "body": "kaffe", "url": FEED_URL})

    assert json.loads(bodies[0]) == {"head": "Hej", "body": "kaffe", "url": FEED_URL}


# --- subscribe endpoint -------------------------------------------------------------------------


def test_subscribe_requires_login(client: Client) -> None:
    response = client.post(DEN_HURTIGE_SUBSCRIBE, data=_subscribe_body(), content_type="application/json")
    assert response.status_code == 302
    assert "/intern/admin/login" in response["Location"]


def test_subscribe_binds_the_device_to_the_logged_in_user(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The audience comes from the session, never the request body — django-webpush's endpoint took
    a group name from the payload, so anyone could have joined the dorm's notification list."""
    user = make_resident(email="a@gahk.dk")
    client.force_login(user)

    response = client.post(
        DEN_HURTIGE_SUBSCRIBE,
        data=_subscribe_body(user="somebody-else"),
        content_type="application/json",
    )

    assert response.status_code == 201
    subscription = PushSubscription.objects.get()
    assert subscription.user_id == user.pk
    assert subscription.endpoint == "https://fcm.googleapis.com/fcm/send/abc"


def test_resubscribing_the_same_device_updates_rather_than_duplicates(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The endpoint is the device identity. django-webpush keyed on every field, so a browser that
    merely changed its user-agent string silently produced a second row and a double notification."""
    first = make_resident(email="a@gahk.dk")
    second = make_resident(email="b@gahk.dk")

    client.force_login(first)
    client.post(DEN_HURTIGE_SUBSCRIBE, data=_subscribe_body(), content_type="application/json")
    client.force_login(second)  # same browser, different resident logs in
    client.post(
        DEN_HURTIGE_SUBSCRIBE,
        data=_subscribe_body(user_agent="pytest/2"),
        content_type="application/json",
    )

    subscription = PushSubscription.objects.get()  # exactly one row, not two
    assert subscription.user_id == second.pk  # and it no longer pushes to the previous owner
    assert subscription.user_agent == "pytest/2"


def test_unsubscribe_removes_the_device(client: Client, make_resident: Callable[..., Resident]) -> None:
    """With only one topic on, opting out of it leaves nothing to keep the row for."""
    user = make_resident(email="a@gahk.dk")
    client.force_login(user)
    client.post(DEN_HURTIGE_SUBSCRIBE, data=_subscribe_body(), content_type="application/json")

    response = client.post(
        DEN_HURTIGE_SUBSCRIBE,
        data=_subscribe_body(status_type="unsubscribe"),
        content_type="application/json",
    )

    assert response.status_code == 202
    assert response.json()["remaining_topics"] == 0  # tells push.ts it may release the browser's own
    assert not PushSubscription.objects.exists()


def test_unsubscribe_cannot_remove_another_residents_device(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Endpoints are guessable-ish strings that travel in push traffic; deleting must be scoped to
    the requesting user so a replayed endpoint cannot silence someone else's phone."""
    owner = make_resident(email="a@gahk.dk")
    attacker = make_resident(email="b@gahk.dk")
    client.force_login(owner)
    client.post(DEN_HURTIGE_SUBSCRIBE, data=_subscribe_body(), content_type="application/json")

    client.force_login(attacker)
    response = client.post(
        DEN_HURTIGE_SUBSCRIBE,
        data=_subscribe_body(status_type="unsubscribe"),
        content_type="application/json",
    )

    assert response.status_code == 202  # nothing to report to the caller
    assert PushSubscription.objects.filter(user=owner).exists()  # but the owner's device survives


@pytest.mark.parametrize(
    "body",
    ["not json", json.dumps(["a", "list"]), json.dumps({"status_type": "nonsense"})],
)
def test_subscribe_rejects_malformed_payloads(
    client: Client, make_resident: Callable[..., Resident], body: str
) -> None:
    client.force_login(make_resident(email="a@gahk.dk"))
    response = client.post(DEN_HURTIGE_SUBSCRIBE, data=body, content_type="application/json")
    assert response.status_code == 400


def test_a_payload_naming_another_topic_is_refused(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The view owns the topic (it owns the page and its access gate); the body is only a
    cross-check. Otherwise a crafted payload posted to Den Hurtige's endpoint could opt a device
    into a feature whose own gate the caller never passed."""
    client.force_login(make_resident(email="a@gahk.dk"))

    response = client.post(
        DEN_HURTIGE_SUBSCRIBE,
        data=_subscribe_body(topic="opslagstavle"),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert not PushSubscription.objects.exists()


def test_a_payload_without_a_topic_is_treated_as_den_hurtige(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """A browser running a cached copy of the pre-topics bundle sends no `topic`. That build could
    only have been subscribing to Den Hurtige, so it must keep working rather than 400."""
    client.force_login(make_resident(email="a@gahk.dk"))
    body = json.loads(_subscribe_body())
    del body["topic"]

    response = client.post(DEN_HURTIGE_SUBSCRIBE, data=json.dumps(body), content_type="application/json")

    assert response.status_code == 201
    assert PushSubscription.objects.get().wants_den_hurtige is True


# --- inline vs background delivery ----------------------------------------------------------------
#
# `send` hands the fan-out to a daemon thread by default, which is right in a request and silently
# wrong anywhere the interpreter is about to exit. These two tests are what keep the escape hatch
# honest, because the failure they guard against produces no error at all -- just fewer
# notifications than there were devices, a different number every run.


def test_send_delivers_inline_when_background_is_off(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident], settings: object
) -> None:
    """A management command must have delivered before `handle()` returns.

    The default path defers to a daemon thread via transaction.on_commit. In a command that thread
    is killed at interpreter shutdown, mid-fan-out, so a scheduled job would push to however many
    devices it happened to reach. `background=False` is the fix; this asserts it actually dispatches
    before returning rather than queueing.
    """
    settings.VAPID_PUBLIC_KEY = "test-public-key"  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = "test-private-key"  # type: ignore[attr-defined]
    settings.VAPID_ADMIN_EMAIL = "drift@gahk.dk"  # type: ignore[attr-defined]
    subscribe(make_resident(email="a@gahk.dk"), "https://push.example/a")
    seen: list[str] = []
    monkeypatch.setattr(push, "_send", lambda sub, body: seen.append(sub.endpoint))

    def fail(fn: object) -> None:
        raise AssertionError("background=False must not touch the thread path")

    monkeypatch.setattr(push, "_run_in_background", fail)

    push.send(push.subscribers("den_hurtige"), "Titel", "Tekst", "/intern/", background=False)

    assert seen == ["https://push.example/a"]


def test_send_defers_to_the_thread_by_default(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident], settings: object
) -> None:
    """The request path must stay off the request thread -- one HTTPS round-trip per device against
    gunicorn's 60s timeout is what the thread exists to avoid."""
    settings.VAPID_PUBLIC_KEY = "test-public-key"  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = "test-private-key"  # type: ignore[attr-defined]
    settings.VAPID_ADMIN_EMAIL = "drift@gahk.dk"  # type: ignore[attr-defined]
    subscribe(make_resident(email="a@gahk.dk"), "https://push.example/a")
    queued: list[object] = []

    def record(fn: object) -> None:
        queued.append(fn)

    monkeypatch.setattr(push, "_run_in_background", record)
    monkeypatch.setattr(push, "_send", lambda sub, body: pytest.fail("should not send inline"))

    push.send(push.subscribers("den_hurtige"), "Titel", "Tekst", "/intern/")

    assert len(queued) == 1


# --- per-topic consent --------------------------------------------------------------------------


def test_a_new_device_is_not_opted_into_the_board_by_default(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Consent to one feature's notifications is not consent to another's. This default is the whole
    reason the two topics are separate columns."""
    client.force_login(make_resident(email="a@gahk.dk"))
    client.post(DEN_HURTIGE_SUBSCRIBE, data=_subscribe_body(), content_type="application/json")

    subscription = PushSubscription.objects.get()
    assert subscription.wants_den_hurtige is True
    assert subscription.wants_opslagstavle is False


def test_subscribing_to_the_board_does_not_opt_into_den_hurtige(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The symmetric half of the test above, and a real bug this caught: `wants_den_hurtige` first
    shipped with `default=True` (so pre-existing rows would keep working), which meant every brand
    new board subscriber silently also got Den Hurtige's urgent buzz. Both columns default to False
    now, and pre-existing rows are backfilled by core/migrations/0005 instead."""
    client.force_login(make_resident(email="a@gahk.dk"))
    body = json.loads(_subscribe_body())
    body["topic"] = "opslagstavle"

    response = client.post(
        "/intern/opslagstavle/abonner", data=json.dumps(body), content_type="application/json"
    )

    assert response.status_code == 201
    subscription = PushSubscription.objects.get()
    assert subscription.wants_opslagstavle is True
    assert subscription.wants_den_hurtige is False, "the board opted the device into the chat too"


def test_subscribing_to_one_topic_leaves_the_other_alone(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The upsert's `defaults` must carry exactly one flag. Writing both is how subscribing to the
    noticeboard would silently switch the chat's notifications off (or on)."""
    user = make_resident(email="a@gahk.dk")
    device = subscribe(user, "https://fcm.googleapis.com/fcm/send/abc", opslagstavle=True)
    client.force_login(user)

    client.post(DEN_HURTIGE_SUBSCRIBE, data=_subscribe_body(), content_type="application/json")

    device.refresh_from_db()
    assert device.wants_den_hurtige is True
    assert device.wants_opslagstavle is True, "the other topic's consent was overwritten"


def test_unsubscribing_from_one_topic_keeps_the_other_alive(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """A device has ONE push endpoint for both features, so opting out of one must not delete the
    row. The symptom of getting this wrong — "notifications just stopped" — is invisible server-side.
    """
    user = make_resident(email="a@gahk.dk")
    device = subscribe(user, "https://fcm.googleapis.com/fcm/send/abc", opslagstavle=True)
    client.force_login(user)

    response = client.post(
        DEN_HURTIGE_SUBSCRIBE,
        data=_subscribe_body(status_type="unsubscribe"),
        content_type="application/json",
    )

    assert response.json()["remaining_topics"] == 1  # so push.ts keeps the browser subscription
    device.refresh_from_db()
    assert device.wants_den_hurtige is False
    assert device.wants_opslagstavle is True


def test_the_row_is_deleted_only_when_the_last_topic_is_turned_off(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    user = make_resident(email="a@gahk.dk")
    subscribe(user, "https://fcm.googleapis.com/fcm/send/abc", opslagstavle=False)
    client.force_login(user)

    client.post(
        DEN_HURTIGE_SUBSCRIBE,
        data=_subscribe_body(status_type="unsubscribe"),
        content_type="application/json",
    )

    assert not PushSubscription.objects.exists()


def test_subscribers_are_filtered_by_topic(make_resident: Callable[..., Resident]) -> None:
    """The fan-out reads consent, so a device that only wants the board must never appear in Den
    Hurtige's audience."""
    chat_only = make_resident(email="a@gahk.dk")
    board_only = make_resident(email="b@gahk.dk")
    both = make_resident(email="c@gahk.dk")
    subscribe(chat_only, "https://push.example/1", den_hurtige=True, opslagstavle=False)
    subscribe(board_only, "https://push.example/2", den_hurtige=False, opslagstavle=True)
    subscribe(both, "https://push.example/3", den_hurtige=True, opslagstavle=True)

    chat = set(push.subscribers("den_hurtige").values_list("user_id", flat=True))
    board = set(push.subscribers("opslagstavle").values_list("user_id", flat=True))

    assert chat == {chat_only.pk, both.pk}
    assert board == {board_only.pk, both.pk}


def test_an_unknown_topic_raises_rather_than_notifying_everyone(
    make_resident: Callable[..., Resident],
) -> None:
    """`subscribers` looks the topic up in TOPIC_FIELDS, so a typo is a KeyError at the call site
    instead of an unfiltered queryset — failing loudly beats notifying the whole dorm."""
    with pytest.raises(KeyError):
        push.subscribers("opslagtavle")  # note the typo


def test_a_plain_resident_cannot_subscribe_to_den_hurtige_during_the_rollout(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subscribe view is @login_required so every resident can reach the board's topic — so Den
    Hurtige's staged rollout has to be re-checked *inside* the view. Dropping that check is how the
    rollout would silently leak to the whole kollegium."""
    monkeypatch.setattr(den_hurtige_access, "ACCESS_ROLES", (Role.ADMINISTRATOR, Role.INSPEKTION))
    client.force_login(make_resident(email="beboer@gahk.dk"))

    response = client.post(DEN_HURTIGE_SUBSCRIBE, data=_subscribe_body(), content_type="application/json")

    assert response.status_code == 403
    assert not PushSubscription.objects.exists()


def test_an_administrator_can_still_subscribe_during_the_rollout(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(den_hurtige_access, "ACCESS_ROLES", (Role.ADMINISTRATOR, Role.INSPEKTION))
    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))

    response = client.post(DEN_HURTIGE_SUBSCRIBE, data=_subscribe_body(), content_type="application/json")

    assert response.status_code == 201


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
