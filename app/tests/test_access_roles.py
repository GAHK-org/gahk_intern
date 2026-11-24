"""Roles read from the list, and roles given by hand with dates (access.roles, the role page)."""

import datetime
from collections.abc import Callable

import pytest
from django.test import Client

from access.api import holding, real_roles
from access.models import Grant
from core.clock import current_date
from core.models import Room, Workgroup
from residents.models import AdminAccessGrant, Residency, Resident, Role, active_period, next_period

pytestmark = pytest.mark.django_db


def _place(resident: Resident, period: tuple[int, int], workgroup: str | None, number: int) -> None:
    room = Room.objects.create(legacy_index=number, number=number, floor="stuen", side="mod gaden")
    group = Workgroup.objects.get_or_create(name=workgroup)[0] if workgroup else None
    Residency.objects.create(
        resident=resident.person, room=room, workgroup=group, year=period[0], month=period[1]
    )


def test_an_embedsgruppe_gives_its_role_in_the_month_in_effect_only(
    make_resident: Callable[..., Resident],
) -> None:
    now = active_period()
    ahead = next_period(now)
    anna = make_resident(email="anna@gahk.dk")
    _place(anna, now, "AK-gruppen", 801)
    _place(anna, ahead, "Fotogruppen", 802)  # next month's list, prepared ahead
    assert Role.AK in real_roles(anna) and Role.FOTO not in real_roles(anna)
    assert anna.has_role(Role.FOTO, ahead) and not anna.has_role(Role.AK, ahead)
    assert anna in holding({Role.AK}) and anna not in holding({Role.FOTO})


def test_a_chore_group_gives_no_role(make_resident: Callable[..., Resident]) -> None:
    bo = make_resident(email="bo@gahk.dk")
    _place(bo, active_period(), "Haven", 803)
    assert real_roles(bo) & set(Role.values) == set()


def test_a_grant_holds_through_its_last_day(make_resident: Callable[..., Resident]) -> None:
    cy = make_resident(email="cy@gahk.dk")
    today = current_date()
    Grant.objects.create(
        resident=cy, role=Role.REGNSKAB, reason="Afslutter regnskabet", starts_on=today, ends_on=today
    )
    assert Role.REGNSKAB in real_roles(Resident.objects.get(pk=cy.pk))
    yesterday = today - datetime.timedelta(days=1)
    Grant.objects.filter(resident=cy).update(starts_on=yesterday, ends_on=yesterday)
    assert Role.REGNSKAB not in real_roles(Resident.objects.get(pk=cy.pk))


def _admin_client(make_resident: Callable[..., Resident]) -> tuple[Client, Resident]:
    admin = make_resident(email="admin@gahk.dk", is_superuser=True)
    client = Client()
    client.force_login(admin)
    return client, admin


def test_the_role_page_gives_a_role_until_removed(make_resident: Callable[..., Resident]) -> None:
    client, admin = _admin_client(make_resident)
    dina = make_resident(email="dina@gahk.dk")
    client.post("/admin/roles", {"resident": dina.pk, "role": Role.FOTO, "action": "add"})
    grant = Grant.objects.get(resident=dina)
    assert (grant.role, grant.ends_on, grant.granted_by) == (Role.FOTO, None, admin)
    assert dina.has_role(Role.FOTO, next_period(active_period()))  # no rolling forward needed

    client.post("/admin/roles", {"resident": dina.pk, "role": Role.FOTO, "action": "remove"})
    assert not Grant.objects.filter(resident=dina).exists()  # given today: removed outright
    assert Role.FOTO not in real_roles(Resident.objects.get(pk=dina.pk))


def test_removing_a_role_held_for_a_while_ends_it_yesterday(make_resident: Callable[..., Resident]) -> None:
    client, _admin = _admin_client(make_resident)
    eva = make_resident(email="eva@gahk.dk")
    since = current_date() - datetime.timedelta(days=40)
    Grant.objects.create(resident=eva, role=Role.PR, reason="x", starts_on=since)
    client.post("/admin/roles", {"resident": eva.pk, "role": Role.PR, "action": "remove"})
    assert Grant.objects.get(resident=eva).ends_on == current_date() - datetime.timedelta(days=1)
    assert eva.has_role(Role.PR, (since.year, since.month))  # the history stays


def test_the_role_page_shows_a_list_role_it_cannot_remove(make_resident: Callable[..., Resident]) -> None:
    client, _admin = _admin_client(make_resident)
    finn = make_resident(email="finn@gahk.dk")
    _place(finn, active_period(), "AK-gruppen", 804)
    html = client.get("/admin/roles").content.decode()
    assert 'title="Fra alumnelisten">ak<' in html
    assert html.count("<h1>Roller / embedsgrupper</h1>") == 1 and "<html" in html
    client.post("/admin/roles", {"resident": finn.pk, "role": Role.AK, "action": "remove"})
    assert Role.AK in real_roles(Resident.objects.get(pk=finn.pk))


def test_giving_administrator_opens_the_two_person_approval(make_resident: Callable[..., Resident]) -> None:
    client, admin = _admin_client(make_resident)
    gert = make_resident(email="gert@gahk.dk")
    client.post("/admin/roles", {"resident": gert.pk, "role": Role.ADMINISTRATOR, "action": "add"})
    approval = AdminAccessGrant.objects.get(resident=gert)
    assert (approval.status, approval.requested_by) == (AdminAccessGrant.Status.PENDING, admin)
    assert Resident.objects.get(pk=gert.pk).is_staff is True
