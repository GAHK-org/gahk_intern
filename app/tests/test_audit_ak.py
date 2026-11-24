"""The audit trail of AK (ak.auditing): the ledger's lines, the schedule, and the monthly charge."""

from collections.abc import Callable

import pytest
from django.core.management import call_command
from django.test import Client

from ak.models import AkEntry, AkMonthlyCharge
from ak.services import charge_period
from audit.models import AuditEntity
from core.models import Room
from residents.models import Residency, Resident, Role

from .audit_support import actions, changes, events

pytestmark = pytest.mark.django_db


def _client(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def test_lines_written_and_deleted_by_hand_say_who_did_it(make_resident: Callable[..., Resident]) -> None:
    anna = make_resident(email="anna@gahk.dk", first_name="Anna")
    officer = make_resident(email="ak@gahk.dk", roles=[Role.AK])
    _client(anna).post("/ak/tilfoej", {"krydser": "2", "reason": "Ryddede kælderen"})
    _client(officer).post(f"/ak/log/{anna.pk}/add", {"delta": "-1", "reason": "Rettelse"})
    entry = AkEntry.objects.get(delta=-1)
    _client(officer).post(f"/ak/log/{anna.pk}/delete/{entry.pk}")

    assert actions("ak.") == ["ak.labour.logged", "ak.entry.added", "ak.entry.deleted"]
    logged = events("ak.labour.logged").get()
    assert logged.actor_id == anna.pk
    assert changes(logged)["delta"] == [None, 2]
    added = events("ak.entry.added").get()
    assert added.actor_id == officer.pk
    assert added.entities.get(role="resident").entity_id == anna.pk
    deleted = events("ak.entry.deleted").get()
    assert deleted.actor_id == officer.pk
    assert changes(deleted)["reason"] == ["Rettelse", None]


def test_saving_the_schedule_records_only_the_months_that_changed(
    make_resident: Callable[..., Resident],
) -> None:
    officer = make_resident(email="ak@gahk.dk", roles=[Role.AK])
    payload = {f"active_{m}": "1" for m in range(1, 13)} | {f"krydser_{m}": "2" for m in range(1, 13)}
    client = _client(officer)
    client.post("/ak/admin/gem-maaneder", payload)  # as seeded: nothing to record
    client.post("/ak/admin/gem-maaneder", payload | {"krydser_3": "4"})

    saved = events("ak.schedule.saved").get()
    assert saved.actor_id == officer.pk
    entity = saved.entities.get(kind="ak.monthly_charge")
    assert entity.name == "AK-træk i marts"
    assert changes(saved, "ak.monthly_charge") == {"krydser": [2, 4]}


def test_the_monthly_charge_is_the_systems_with_every_line(make_resident: Callable[..., Resident]) -> None:
    y, m = charge_period()
    room = Room.objects.create(legacy_index=1, number=1, floor="stuen", side="mod gaden")
    members = [make_resident(email=f"m{i}@gahk.dk") for i in range(2)]
    for member in members:
        Residency.objects.create(resident=member, room=room, year=y, month=m)
    AkMonthlyCharge.objects.filter(month=m).delete()  # the command makes the month's default row

    call_command("ak_monthly_assessment")

    assert actions("ak.") == ["ak.schedule.month_added", "ak.month.charged"]
    charged = events("ak.month.charged").get()
    assert charged.actor_id is None
    assert charged.details == {"year": y, "month": m, "written": 2, "removed": 0}
    assert charged.entities.filter(kind="ak.entry", operation="created").count() == 2
    # Which month was charged last is the feature's own marker, exempt from the trail.
    assert set(AuditEntity.objects.values_list("kind", flat=True)) == {"ak.entry", "ak.monthly_charge"}


def test_the_page_load_backstop_charges_as_the_system_not_the_visitor(
    make_resident: Callable[..., Resident],
) -> None:
    anna = make_resident(email="anna@gahk.dk")
    y, m = charge_period()
    room = Room.objects.create(legacy_index=1, number=1, floor="stuen", side="mod gaden")
    Residency.objects.create(resident=anna, room=room, year=y, month=m)

    _client(anna).get("/ak/")

    charged = events("ak.month.charged").get()
    assert charged.actor_id is None
    assert charged.entities.get(kind="ak.entry").operation == "created"
