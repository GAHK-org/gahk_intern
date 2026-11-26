"""What core leaves in the audit trail: a device's push subscription, and the reference data (rooms,
embedsgrupper, rengøringer). Delivery counters, heartbeats and job logs leave nothing; the statistics
(stats) neither."""

import json
from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse
from pywebpush import WebPushException

from audit import trail
from audit.models import AuditChange
from core import heartbeat, push
from core.models import PushDeliveryDay, PushSubscription, Room, Workgroup
from residents.models import Resident, Role
from stats.services import ingest_outbox_visits
from tests.audit_support import actions, changes, events

pytestmark = pytest.mark.django_db

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc"


def _post(client: Client, status_type: str, user_agent: str = "Firefox") -> None:
    body = {
        "status_type": status_type,
        "subscription": {"endpoint": ENDPOINT, "keys": {"auth": "a" * 22, "p256dh": "p" * 87}},
        "user_agent": user_agent,
    }
    client.post(reverse("notifications:subscription"), json.dumps(body), content_type="application/json")


def test_subscribing_and_unsubscribing_a_device_is_recorded_without_its_keys(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    anna = make_resident(email="anna@gahk.dk", first_name="Anna", last_name="Berg")
    client.force_login(anna)

    _post(client, "subscribe")
    _post(client, "subscribe")  # pressed again on the same device: nothing changed
    _post(client, "subscribe", user_agent="Firefox 2")
    _post(client, "unsubscribe")

    assert actions("core") == [
        "core.push_subscription.subscribed",
        "core.push_subscription.subscribed",
        "core.push_subscription.unsubscribed",
    ]
    subscribed, updated, unsubscribed = events("core.push_subscription")
    assert subscribed.actor_id == anna.pk
    created = changes(subscribed)
    assert created["user"] == [None, anna.pk]
    assert created["user_agent"] == [None, "Firefox"]
    # That it has an address and keys, never what they are.
    assert {created[f] == [None, None] for f in ("endpoint", "auth", "p256dh")} == {True}
    assert set(
        AuditChange.objects.filter(field__in=("endpoint", "auth", "p256dh")).values_list(
            "redacted", flat=True
        )
    ) == {True}
    assert changes(updated) == {"user_agent": ["Firefox", "Firefox 2"]}
    gone = unsubscribed.entities.get(role="subject")
    assert (gone.operation, gone.name) == ("deleted", "Anna Berg: Firefox 2")


def test_an_administrator_cancelling_a_device_is_recorded(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    admin = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    owner = make_resident(email="anna@gahk.dk")
    device = PushSubscription.objects.create(user=owner, endpoint=ENDPOINT, auth="a", p256dh="p")
    client.force_login(admin)

    client.post(reverse("siteadmin:notification_device_cancel", args=[device.pk]))

    [cancelled] = events("core.push_subscription.cancelled")
    assert cancelled.actor_id == admin.pk
    assert cancelled.entities.get(role="subject").operation == "deleted"
    assert cancelled.entities.get(role="owner").entity_id == owner.pk


def test_a_device_the_push_service_forgot_is_removed_by_nobody(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident]
) -> None:
    device = PushSubscription.objects.create(
        user=make_resident(email="anna@gahk.dk"), endpoint=ENDPOINT, auth="a", p256dh="p"
    )
    gone = WebPushException("gone", response=type("Response", (), {"status_code": 410})())

    def fake_send(subscription: PushSubscription, body: str, ttl: int = push.TTL_SECONDS) -> None:
        raise gone

    monkeypatch.setattr(push, "_send", fake_send)

    push._dispatch(PushSubscription.objects.all(), {"title": "t"})

    [expired] = events("core.push_subscription.expired")
    assert expired.actor_id is None
    assert expired.details == {"status": 410}
    assert expired.entities.get(role="subject").entity_id == device.pk
    # Today's delivery counter moved too, and is not in the trail.
    assert PushDeliveryDay.objects.get().removed == 1
    assert actions() == ["core.push_subscription.expired"]


def test_a_delivered_push_moves_only_bookkeeping(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident]
) -> None:
    PushSubscription.objects.create(
        user=make_resident(email="anna@gahk.dk"), endpoint=ENDPOINT, auth="a", p256dh="p"
    )
    monkeypatch.setattr(push, "_send", lambda subscription, body, ttl=push.TTL_SECONDS: None)

    with trail.context("worker"):
        assert push._dispatch(PushSubscription.objects.all(), {"title": "t"}) == 1

    assert actions() == []


def test_reference_data_is_recorded_when_edited(make_resident: Callable[..., Resident]) -> None:
    group = Workgroup.objects.create(name="Køkkengruppen", size=4)
    with trail.event("test.workgroup.resized", actor=None, subject=group):
        group.size = 5
        group.save()
    with trail.event("test.room.noted", actor=None):
        Room.objects.create(legacy_index=1, number=101, floor="1. sal", side="mod gaden")

    resized, noted = events("test.")
    assert changes(resized) == {"size": [4, 5]}
    assert noted.entities.get(kind="core.room").name == "Værelse 101"


def test_heartbeats_and_visit_statistics_leave_no_trail() -> None:
    with trail.context("worker"):
        heartbeat.record("default", "worker-1")
        ingest_outbox_visits([{"day": "2026-11-01", "path": "*", "views": 10, "visitors": 4}])

    assert actions() == []
