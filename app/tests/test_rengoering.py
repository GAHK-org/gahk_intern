"""Søndagsrengøring — rotas, the alumne's sign-off and Inspektionen's check (spec/features/rengoering.md).

Most of what is worth testing is two kinds of rule:

  * WHO may see and do what — a beboer sees only their own assignments (others' are 404, not 403),
    and only Inspektionen of the *active* month, administrators and superusers plan and inspect;
  * the derived STATUS of an assignment, which every page reads instead of a stored column.
"""

import datetime
from collections.abc import Callable

import pytest
from django.test import Client
from django.utils import timezone

from rengoering.areas_data import AREAS
from rengoering.models import (
    AssignmentStatus,
    CleaningArea,
    CleaningAssignment,
    CleaningTask,
    Inspection,
    Rota,
    at_deadline_hour,
    default_fix_deadline,
)
from rengoering.services import load_areas, visible_tasks
from residents.models import Resident, Role, RoleAssignment, active_period

pytestmark = pytest.mark.django_db

SUNDAY = datetime.date(2026, 11, 1)


def local(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)))


@pytest.fixture
def beboer(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="beboer@gahk.dk", first_name="Bo", last_name="Beboer")


@pytest.fixture
def inspektor(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="insp@gahk.dk", first_name="Ina", roles=(Role.INSPEKTION,))


@pytest.fixture
def areas(db: None) -> list[CleaningArea]:
    load_areas()
    return list(CleaningArea.objects.all())


@pytest.fixture
def rota(db: None) -> Rota:
    return Rota.objects.create(name="E26", start_date=SUNDAY)


def make_assignment(
    rota: Rota, area: CleaningArea, resident: Resident | None, day: datetime.date = SUNDAY, **kw: object
) -> CleaningAssignment:
    return CleaningAssignment.objects.create(rota=rota, area=area, resident=resident, date=day, **kw)


def as_user(user: Resident) -> Client:
    c = Client()
    c.force_login(user)
    return c


# ---- seeding ----


def test_load_areas_seeds_every_area_and_task_once(db: None) -> None:
    expected = sum(len(texts) for _, _, _, zones in AREAS for _, groups in zones for _, texts in groups)
    assert load_areas() == (9, expected)
    assert CleaningTask.objects.filter(is_active=True).count() == expected
    assert load_areas() == (0, 0)  # idempotent: existing areas are Inspektionen's now
    stuen = CleaningArea.objects.get(slug="sal-stuen")
    assert "Stuen, Gang, Toilet" in stuen.aliases()
    star = CleaningTask.objects.get(area=stuen, text="Gangspejlet pudset og paneler aftørret")
    assert star.star_only  # the "*" prefix became the flag


def test_load_areas_keeps_edits_unless_reset(areas: list[CleaningArea]) -> None:
    area = areas[0]
    area.tasks.update(text="redigeret")
    load_areas()
    assert set(area.tasks.values_list("text", flat=True)) == {"redigeret"}
    load_areas(reset=True)
    assert not area.tasks.filter(is_active=True, text="redigeret").exists()
    assert area.tasks.filter(is_active=False, text="redigeret").exists()  # deactivated, not deleted


def test_star_tasks_only_in_star_weeks(areas: list[CleaningArea], rota: Rota, beboer: Resident) -> None:
    area = CleaningArea.objects.get(slug="varme-trappe")
    normal = make_assignment(rota, area, beboer)
    star = make_assignment(rota, area, beboer, SUNDAY + datetime.timedelta(weeks=4), is_star_week=True)
    assert not visible_tasks(normal).filter(star_only=True).exists()
    assert visible_tasks(star).filter(star_only=True).exists()
    assert visible_tasks(star).count() > visible_tasks(normal).count()


# ---- deadlines + derived status ----


def test_fix_deadline_is_22_the_next_day_even_after_midnight() -> None:
    assert default_fix_deadline(local(SUNDAY, 22, 30)) == local(SUNDAY + datetime.timedelta(days=1), 22)
    # a round that ran past midnight still belongs to Sunday evening
    monday = SUNDAY + datetime.timedelta(days=1)
    assert default_fix_deadline(local(monday, 0, 40)) == local(monday, 22)
    assert default_fix_deadline(local(monday, 9)) == local(monday + datetime.timedelta(days=1), 22)


def test_status_walks_through_the_whole_round(
    areas: list[CleaningArea], rota: Rota, beboer: Resident, inspektor: Resident
) -> None:
    a = make_assignment(rota, areas[0], beboer)
    saturday = local(SUNDAY - datetime.timedelta(days=1), 12)
    assert a.status(saturday) == AssignmentStatus.KOMMENDE
    assert a.status(local(SUNDAY, 18)) == AssignmentStatus.AFVENTER
    assert a.status(local(SUNDAY, 22, 1)) == AssignmentStatus.IKKE_TAGET
    a.signed_off_at = local(SUNDAY, 19)
    assert a.status(local(SUNDAY, 23)) == AssignmentStatus.TAGET

    first = Inspection.objects.create(
        assignment=a,
        inspector=inspektor,
        outcome=Inspection.Outcome.MANGLER,
        decided_at=local(SUNDAY, 22, 10),
        deadline=default_fix_deadline(local(SUNDAY, 22, 10)),
    )
    monday = SUNDAY + datetime.timedelta(days=1)
    assert a.status(local(monday, 12)) == AssignmentStatus.MANGLER
    assert a.status(local(monday, 22, 1)) == AssignmentStatus.OVERSKREDET
    first.fixed_at = local(monday, 20)
    first.save()
    assert a.status(local(monday, 23)) == AssignmentStatus.UDBEDRET
    Inspection.objects.create(
        assignment=a, round=2, outcome=Inspection.Outcome.GODKENDT, decided_at=local(monday, 23)
    )
    assert a.status(local(monday, 23, 30)) == AssignmentStatus.GODKENDT


def test_an_undecided_round_does_not_change_the_status(
    areas: list[CleaningArea], rota: Rota, beboer: Resident
) -> None:
    a = make_assignment(rota, areas[0], beboer, signed_off_at=local(SUNDAY, 19))
    Inspection.objects.create(assignment=a)  # ticks in progress
    assert a.status(local(SUNDAY, 22, 5)) == AssignmentStatus.TAGET
    assert a.current_round() is not None and a.latest_decided() is None


def test_deadline_helper_is_local_22() -> None:
    assert timezone.localtime(at_deadline_hour(SUNDAY)).hour == 22


# ---- admin: areas and tasks are Inspektionen's ----

AREA_ADMIN = "/django-admin/rengoering/cleaningarea/"


def test_area_admin_is_for_inspektionen_only(
    areas: list[CleaningArea], beboer: Resident, inspektor: Resident, make_resident: Callable[..., Resident]
) -> None:
    assert as_user(inspektor).get(AREA_ADMIN).status_code == 200
    assert as_user(inspektor).get(f"{AREA_ADMIN}{areas[0].pk}/change/").status_code == 200
    # a beboer is not staff at all; a previous month's Inspektion has is_staff but no active role
    assert as_user(beboer).get(AREA_ADMIN).status_code == 302
    old = make_resident(email="old@gahk.dk", is_staff=True)
    year, month = active_period()
    prev = (year, month - 1) if month > 1 else (year - 1, 12)
    RoleAssignment.objects.create(resident=old, role=Role.INSPEKTION, year=prev[0], month=prev[1])
    assert as_user(old).get(AREA_ADMIN).status_code == 403
