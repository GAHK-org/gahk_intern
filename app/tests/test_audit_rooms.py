"""The audit trail of Søg værelse and Værelsestjek (rooms.auditing)."""

from collections.abc import Callable
from datetime import date

import pytest
from django.test import Client
from django.urls import reverse

from audit.models import AuditChange
from core.models import Room
from residents.models import Resident, Role, next_period
from rooms.kvotient import month_index
from rooms.models import KvotientApplication, KvotientPriority, RoomCondition, RoomCriterion, RoomOffer

from .audit_support import actions, changes, events

pytestmark = pytest.mark.django_db


def _room(number: int) -> Room:
    return Room.objects.create(legacy_index=number, number=number, floor="stuen", side="mod gaden")


def _client(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def test_an_application_its_edit_and_its_withdrawal_are_the_applicants(
    make_resident: Callable[..., Resident],
) -> None:
    anna = make_resident(email="anna@gahk.dk", first_name="Anna", move_in_date=date(2024, 8, 1))
    a, b = _room(101), _room(102)
    client = _client(anna)
    form = {"done_year": "2028", "done_month": "6", "orlov_months": "0", "priority": [str(a.pk), str(b.pk)]}

    client.post(reverse("soegvaerelse:soeg"), form)
    client.post(reverse("soegvaerelse:soeg"), form | {"done_month": "12"})  # the study end alone
    app = KvotientApplication.objects.get()
    client.post(reverse("soegvaerelse:delete_application", args=[app.pk]))

    assert actions("rooms.") == [
        "rooms.application.submitted",
        "rooms.application.updated",
        "rooms.application.withdrawn",
    ]
    submitted = events("rooms.application.submitted").get()
    assert submitted.actor_id == anna.pk
    assert submitted.entities.filter(kind="rooms.priority", operation="created").count() == 2
    updated = events("rooms.application.updated").get()
    assert set(changes(updated)) == {"done_studying_month", "k"}
    assert not updated.entities.filter(kind="rooms.priority").exists(), "the rooms were not chosen again"
    withdrawn = events("rooms.application.withdrawn").get()
    assert withdrawn.entities.get(role="subject").operation == "deleted"
    assert withdrawn.entities.filter(kind="rooms.priority", operation="deleted").count() == 2


def test_indstilling_deleting_an_application_records_the_reason(
    make_resident: Callable[..., Resident],
) -> None:
    ind = make_resident(email="ind@gahk.dk", roles=(Role.INDSTILLING,))
    anna = make_resident(email="anna@gahk.dk")
    app = KvotientApplication.objects.create(
        resident=anna, move_month=month_index(*next_period()), move_in_month=0, done_studying_month=0, k=1.0
    )

    _client(ind).post(reverse("soegvaerelse:delete_application", args=[app.pk]), {"reason": "Dobbelt"})

    deleted = events("rooms.application.deleted").get()
    assert (deleted.actor_id, deleted.details) == (ind.pk, {"reason": "Dobbelt"})
    assert deleted.entities.get(role="subject").entity_id == app.pk


def test_offers_a_coin_flip_and_closing_are_indstillingens(make_resident: Callable[..., Resident]) -> None:
    ind = make_resident(email="ind@gahk.dk", roles=(Role.INDSTILLING,))
    client = _client(ind)
    room = _room(7)
    client.post(reverse("soegvaerelse:create_offer"), {"room": "7"})
    client.post(reverse("soegvaerelse:create_offer"), {"room": "7"})  # already offered: nothing new
    offer = RoomOffer.objects.get()
    m = offer.month
    tied = [
        KvotientApplication.objects.create(
            resident=make_resident(email=f"t{i}@gahk.dk"),
            move_month=m,
            move_in_month=0,
            done_studying_month=0,
            k=5,
        )
        for i in range(2)
    ]
    for app in tied:
        KvotientPriority.objects.create(application=app, room=room, priority=1, month=m)
    client.post(reverse("soegvaerelse:resolve_tie", args=[offer.pk]), {"application": tied[1].pk})
    client.post(reverse("soegvaerelse:close_offer", args=[offer.pk]))

    assert actions("rooms.") == ["rooms.offer.created", "rooms.offer.tie_resolved", "rooms.offer.closed"]
    resolved = events("rooms.offer.tie_resolved").get()
    assert changes(resolved)["awarded_application"] == [None, tied[1].pk]
    assert resolved.entities.get(role="winner").entity_id == tied[1].pk
    closed = events("rooms.offer.closed").get()
    assert closed.details == {"applications_cleared": 2}
    assert closed.entities.filter(kind="rooms.application", operation="deleted").count() == 2
    assert all(e.actor_id == ind.pk for e in events("rooms."))


def test_a_room_check_is_one_event_that_names_the_report_it_replaces(
    make_resident: Callable[..., Resident],
) -> None:
    bo = make_resident(email="bo@gahk.dk", first_name="Bo")
    room = _room(12)
    RoomCriterion.objects.create(code="floor", name="Gulv", options=5)
    client = _client(bo)
    url = reverse("vaerelsestjek:besvar", args=[room.number])
    client.post(url, {"score_floor": "3", "comment_floor": "Ridset"})
    first = RoomCondition.objects.get()
    client.post(url, {"score_floor": "4", "comment_floor": ""})

    recorded = list(events("rooms.condition.recorded"))
    assert len(recorded) == 2 and all(e.actor_id == bo.pk for e in recorded)
    score = recorded[0].entities.get(kind="rooms.condition_score")
    assert (score.name, score.operation) == ("Gulv: 3", "created")
    assert changes(recorded[0], "rooms.condition_score")["comment"] == [None, "Ridset"]
    replaced = recorded[1].entities.get(role="replaced")
    assert (replaced.entity_id, replaced.operation) == (first.pk, "involved")
    # Which report is the newest is bookkeeping: no line says the old one stopped being it.
    assert not AuditChange.objects.filter(field__in=["is_current", "recorded_by_name"]).exists()


def test_ending_the_round_records_the_moves_and_the_cleared_round(
    make_resident: Callable[..., Resident],
) -> None:
    ind = make_resident(email="ind@gahk.dk", roles=(Role.INDSTILLING,))
    winner = make_resident(email="win@gahk.dk")
    room = _room(20)
    m = month_index(*next_period())
    offer = RoomOffer.objects.create(room=room, month=m)
    app = KvotientApplication.objects.create(
        resident=winner, move_month=m, move_in_month=0, done_studying_month=m + 12, k=7.0
    )
    KvotientPriority.objects.create(application=app, room=room, priority=1, month=m)

    _client(ind).post(reverse("soegvaerelse:end_round"))

    ended = events("rooms.round.ended").get()
    assert ended.actor_id == ind.pk
    assert ended.details["assigned"] == 1
    gone = {(e.kind, e.entity_id) for e in ended.entities.filter(operation="deleted")}
    assert {("rooms.offer", offer.pk), ("rooms.application", app.pk)} <= gone
    assert ended.entities.filter(kind="residents.residency", operation="created").exists()
