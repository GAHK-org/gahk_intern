"""The runtime half of the domain boundaries: domain events (core.domain_events) and the cross-domain
reverse-accessor check (core.boundaries). The static half is `tach check`; see docs/domain-boundaries.md.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from django.db import transaction
from django.test import Client

from ak.models import AkEntry, AkMonthlyCharge
from ak.services import charge_period
from core import boundaries, domain_events
from core.models import Room
from residents.api import MemberAreaVisited
from residents.models import Residency


@dataclass(frozen=True)
class Pinged(domain_events.DomainEvent):
    n: int


@pytest.fixture
def received() -> Iterator[list[int]]:
    """A handler for `Pinged` that records what it got, unregistered again afterwards."""
    got: list[int] = []

    def handler(event: Pinged) -> None:
        got.append(event.n)

    domain_events.subscribe(Pinged)(handler)
    yield got
    domain_events._handlers.pop(Pinged, None)


# --- core.domain_events ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_handlers_run_only_once_the_publisher_commits(
    received: list[int], django_capture_on_commit_callbacks: Callable
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        domain_events.publish(Pinged(1))
        assert received == []  # not inside the publisher's transaction
    assert received == [1]


@pytest.mark.django_db
def test_a_rolled_back_publish_reaches_nobody(
    received: list[int], django_capture_on_commit_callbacks: Callable
) -> None:
    """The reason handlers wait for the commit: never act on a change that did not happen.

    A rolled-back savepoint rather than `transaction=True`: the latter flushes the database after the
    test, taking the rows migrations seed (workgroups, the AK schedule) with it for every later test.
    """
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError), transaction.atomic():
            domain_events.publish(Pinged(1))
            raise RuntimeError

    assert received == []


@pytest.mark.django_db
def test_a_failing_handler_does_not_break_the_publisher(
    received: list[int], django_capture_on_commit_callbacks: Callable
) -> None:
    def broken(event: Pinged) -> None:
        raise ValueError

    domain_events.subscribe(Pinged)(broken)
    with django_capture_on_commit_callbacks(execute=True):
        domain_events.publish(Pinged(2))  # no exception escapes

    assert received == [2]  # and the other handlers still ran


def test_subscribing_twice_registers_once(received: list[int]) -> None:
    handler = domain_events.handlers_for(Pinged)[0]
    domain_events.subscribe(Pinged)(handler)  # e.g. a handlers module imported a second time
    assert len(domain_events.handlers_for(Pinged)) == 1


# --- the first real subscription: AK listens to identity ------------------------------------------


def test_ak_is_subscribed_to_member_area_visits() -> None:
    from ak.handlers import book_monthly_charge_backstop

    assert book_monthly_charge_backstop in domain_events.handlers_for(MemberAreaVisited)


@pytest.mark.django_db
def test_opening_the_dashboard_charges_the_month_that_just_ended(
    make_resident: Callable, django_capture_on_commit_callbacks: Callable
) -> None:
    """What residents.views.dashboard used to do by importing ak.services, now done by publishing an
    event that AK handles. Same outcome, and identity no longer depends on AK."""
    year, month = charge_period()
    AkMonthlyCharge.objects.update_or_create(month=month, defaults={"active": True, "krydser": 2})
    member = make_resident(email="dashboard@gahk.dk")
    room = Room.objects.create(legacy_index=901, number=901, floor="stuen", side="mod gaden")
    Residency.objects.create(resident=member, room=room, year=year, month=month)
    client = Client()
    client.force_login(member)

    with django_capture_on_commit_callbacks(execute=True):
        assert client.get("/intern/").status_code == 200

    assert AkEntry.objects.get(resident=member, kind=AkEntry.Kind.MONTHLY, year=year, month=month).delta == -2


# --- core.boundaries ------------------------------------------------------------------------------


def test_no_new_cross_domain_reverse_accessors() -> None:
    assert boundaries.check_cross_domain_relations(None) == []


def test_a_new_cross_domain_reverse_accessor_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulated by forgetting a baseline entry: to the check, that is exactly what a new FK with a
    related_name into another domain looks like."""
    monkeypatch.setattr(boundaries, "BASELINE", boundaries.BASELINE - {"ak.AkEntry.resident"})

    [error] = boundaries.check_cross_domain_relations(None)

    assert error.id == "core.E010"
    assert "ak.AkEntry.resident -> residents.Resident" in error.msg


def test_the_baseline_can_only_shrink(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry that no longer has an accessor (it was fixed) must be deleted, or the fix could be
    traded for a new violation without the list ever getting shorter."""
    monkeypatch.setattr(boundaries, "BASELINE", boundaries.BASELINE | {"opslagstavle.Notice.event"})

    [error] = boundaries.check_cross_domain_relations(None)

    assert error.id == "core.E011"
    assert "opslagstavle.Notice.event" in error.msg


def test_events_has_no_accessor_onto_the_board() -> None:
    """The relation that started this: an Event must not reach opslagstavle's table."""
    from events.models import Event

    assert not hasattr(Event, "notices")


# --- extension points: how a lower layer shows a feature's content --------------------------------


def test_events_puts_its_card_on_the_dashboard_through_residents_api() -> None:
    """The dashboard is identity's page; begivenheder's card reaches it by registration, because
    identity may not import a feature."""
    from events.handlers import agenda_card
    from residents import api as residents_api

    assert agenda_card in residents_api._dashboard_card_providers


@pytest.mark.django_db
def test_the_dashboard_card_answers_to_the_events_rollout_gate(
    make_resident: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behind the gate the card stays and says so, with no agenda and no link to the calendar."""
    from events import access as events_access
    from residents.api import Role

    monkeypatch.setattr(events_access, "ACCESS_ROLES", (Role.INSPEKTION,))
    client = Client()
    client.force_login(make_resident(email="gated@gahk.dk"))

    body = client.get("/intern/").content.decode()

    assert "Begivenheder på GAHK" in body
    assert "Begivenheder er ikke åbnet for dig endnu." in body
    assert "Åbn kalenderen" not in body


def test_den_hurtige_registers_a_notification_category_per_room() -> None:
    """notifications is platform and no longer imports den_hurtige.channels to build these."""
    from den_hurtige import channels
    from notifications import categories

    registered = {c.key: c for c in categories.all_categories()}

    for channel in channels.CHANNELS:
        room = registered[channels.notification_category(channel.slug)]
        assert (room.group, room.in_app, room.anchor) == ("chat", False, f"rum-{channel.slug}")


def test_notifications_has_no_accessor_onto_residents() -> None:
    from residents.api import Resident

    for accessor in ("notifications", "notification_preferences", "notification_settings"):
        assert not hasattr(Resident, accessor)
