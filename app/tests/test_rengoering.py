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

from core.models import Room
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
from rengoering.services import RotaClash, load_areas, next_for, plan_rota, save_rota, visible_tasks
from residents.models import Residency, Resident, Role, RoleAssignment, active_period

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


# ---- rotas (phase 2) ----

ROTAS = "/intern/rengoering/lister/"


def live_here(*people: Resident) -> None:
    """Put residents on the active month's alumneliste, which is who a rota may be made of."""
    year, month = active_period()
    for i, person in enumerate(people, start=1):
        room, _ = Room.objects.get_or_create(
            legacy_index=i, number=i, defaults={"floor": "stuen", "side": "mod gaden"}
        )
        Residency.objects.create(resident=person, room=room, year=year, month=month)


@pytest.fixture
def trio(make_resident: Callable[..., Resident]) -> list[Resident]:
    people = [
        make_resident(email=f"{n.lower()}@gahk.dk", first_name=n) for n in ("Wilma", "Amalie", "Isabella")
    ]
    live_here(*people)
    return people


def test_plan_rotates_people_and_marks_every_fifth_sunday(
    areas: list[CleaningArea], trio: list[Resident]
) -> None:
    stuen = CleaningArea.objects.get(slug="sal-stuen")
    planned = plan_rota(SUNDAY, 26, 5, {stuen: trio})
    assert [a.resident for a in planned[:4]] == [trio[0], trio[1], trio[2], trio[0]]
    stars = [a.date for a in planned if a.is_star_week]
    # E26's (*)-uger, as on the printed list
    assert stars[:3] == [datetime.date(2026, 11, 29), datetime.date(2027, 1, 3), datetime.date(2027, 2, 7)]
    assert len(stars) == 5
    assert not any(a.is_star_week for a in plan_rota(SUNDAY, 10, 0, {stuen: trio}))


def test_save_rota_refuses_a_taken_sunday_as_a_whole(areas: list[CleaningArea], trio: list[Resident]) -> None:
    stuen = CleaningArea.objects.get(slug="sal-stuen")
    hall = CleaningArea.objects.get(slug="hall-og-vindfang")
    save_rota("E26", plan_rota(SUNDAY, 4, 5, {stuen: trio}), None)
    overlapping = plan_rota(SUNDAY + datetime.timedelta(weeks=3), 4, 5, {stuen: trio, hall: trio})
    with pytest.raises(RotaClash):
        save_rota("F27", overlapping, None)
    assert not Rota.objects.filter(name="F27").exists()  # nothing half-saved
    assert CleaningAssignment.objects.count() == 4


def test_create_rota_previews_then_saves(
    areas: list[CleaningArea], trio: list[Resident], inspektor: Resident
) -> None:
    stuen = CleaningArea.objects.get(slug="sal-stuen")
    data = {"name": "E26", "start_date": "2026-11-01", "weeks": "6", "star_every": "5"}
    data |= {f"area_{stuen.pk}_{i}": str(p.pk) for i, p in enumerate(trio)}
    c = as_user(inspektor)
    preview = c.post(ROTAS + "opret", data)
    assert preview.status_code == 200 and "Forhåndsvisning" in preview.content.decode()
    assert not Rota.objects.exists()
    saved = c.post(ROTAS + "opret", data | {"save": "1"})
    rota = Rota.objects.get(name="E26")
    assert saved.url == f"{ROTAS}{rota.pk}"
    assert rota.assignments.count() == 6
    assert list(rota.assignments.filter(is_star_week=True).values_list("date", flat=True)) == [
        datetime.date(2026, 11, 29)
    ]
    assert c.get(saved.url).status_code == 200


def test_create_rota_wants_a_sunday(
    areas: list[CleaningArea], trio: list[Resident], inspektor: Resident
) -> None:
    stuen = CleaningArea.objects.get(slug="sal-stuen")
    data = {
        "name": "E26",
        "start_date": "2026-11-02",
        "weeks": "6",
        "star_every": "5",
        f"area_{stuen.pk}_0": trio[0].pk,
    }
    r = as_user(inspektor).post(ROTAS + "opret", data | {"save": "1"})
    assert "Vælg en søndag" in r.content.decode()
    assert not Rota.objects.exists()


def test_replace_resident_skips_inspected_sundays(
    areas: list[CleaningArea],
    trio: list[Resident],
    inspektor: Resident,
    make_resident: Callable[..., Resident],
) -> None:
    stuen = CleaningArea.objects.get(slug="sal-stuen")
    rota = save_rota("E26", plan_rota(SUNDAY, 9, 5, {stuen: trio}), None)
    wilma = trio[0]
    first = rota.assignments.get(date=SUNDAY)
    Inspection.objects.create(assignment=first, outcome=Inspection.Outcome.GODKENDT)
    newcomer = make_resident(email="ny@gahk.dk", first_name="Ny")
    live_here(newcomer)
    r = as_user(inspektor).post(
        f"{ROTAS}{rota.pk}/erstat", {"old": wilma.pk, "new": newcomer.pk, "from_date": "2026-11-01"}
    )
    assert r.status_code == 302
    assert rota.assignments.filter(resident=wilma).get() == first  # history kept
    assert rota.assignments.filter(resident=newcomer).count() == 2


def test_rota_can_only_be_deleted_before_it_is_used(
    areas: list[CleaningArea], trio: list[Resident], inspektor: Resident
) -> None:
    stuen = CleaningArea.objects.get(slug="sal-stuen")
    rota = save_rota("E26", plan_rota(SUNDAY, 3, 5, {stuen: trio}), None)
    rota.assignments.filter(date=SUNDAY).update(signed_off_at=timezone.now())
    as_user(inspektor).post(f"{ROTAS}{rota.pk}/slet")
    assert Rota.objects.filter(pk=rota.pk).exists()
    rota.assignments.update(signed_off_at=None)
    as_user(inspektor).post(f"{ROTAS}{rota.pk}/slet")
    assert not Rota.objects.filter(pk=rota.pk).exists()


def test_edit_assignment_swaps_and_clears_sign_off(
    areas: list[CleaningArea], trio: list[Resident], inspektor: Resident
) -> None:
    stuen = CleaningArea.objects.get(slug="sal-stuen")
    rota = save_rota("E26", plan_rota(SUNDAY, 3, 5, {stuen: trio}), None)
    a = rota.assignments.get(date=SUNDAY)
    a.signed_off_at = timezone.now()
    a.save()
    r = as_user(inspektor).post(
        f"/intern/rengoering/vagt/{a.pk}/rediger",
        {"resident": trio[2].pk, "is_star_week": "on", "comment": "byttet", "clear_sign_off": "on"},
    )
    assert r.status_code == 302
    a.refresh_from_db()
    assert (a.resident, a.is_star_week, a.comment, a.signed_off_at) == (trio[2], True, "byttet", None)


@pytest.mark.parametrize(
    "url",
    [
        ROTAS,
        ROTAS + "opret",
        ROTAS + "{rota}",
        ROTAS + "{rota}/erstat",
        "/intern/rengoering/vagt/{a}/rediger",
    ],
)
def test_rota_pages_are_for_inspektionen_only(
    url: str,
    areas: list[CleaningArea],
    trio: list[Resident],
    make_resident: Callable[..., Resident],
) -> None:
    stuen = CleaningArea.objects.get(slug="sal-stuen")
    rota = save_rota("E26", plan_rota(SUNDAY, 3, 5, {stuen: trio}), None)
    url = url.format(rota=rota.pk, a=rota.assignments.first().pk)  # type: ignore[union-attr]
    # Wilma is ON this rota and still may not plan it
    assert as_user(trio[0]).get(url).status_code in (403, 404, 405)
    assert as_user(trio[0]).post(url).status_code in (403, 404)
    admin = make_resident(email="adm@gahk.dk", roles=(Role.ADMINISTRATOR,))
    superuser = make_resident(email="su@gahk.dk", is_superuser=True)
    for manager in (admin, superuser):
        assert as_user(manager).get(url).status_code in (200, 405)
    assert Client().get(url).status_code == 302  # anonymous → login


# ---- the alumne's side (phase 3) ----

MINE = "/intern/rengoering/"


@pytest.fixture
def today_rota(areas: list[CleaningArea], trio: list[Resident]) -> Rota:
    """A rota whose first Sunday is today — whatever weekday the suite runs on, so sign-off is open."""
    stuen = CleaningArea.objects.get(slug="sal-stuen")
    return save_rota("E26", plan_rota(timezone.localdate(), 6, 5, {stuen: trio}), None)


def vagt(a: CleaningAssignment, action: str = "") -> str:
    return f"/intern/rengoering/vagt/{a.pk}" + (f"/{action}" if action else "")


def test_a_resident_sees_only_their_own_assignments(today_rota: Rota, trio: list[Resident]) -> None:
    wilma, amalie, _ = trio
    hers = today_rota.assignments.filter(resident=wilma).first()
    other = today_rota.assignments.filter(resident=amalie).first()
    assert hers and other
    c = as_user(wilma)
    assert c.get(vagt(hers)).status_code == 200
    # someone else's: 404 for looking, signing off and "udbedret" alike
    assert c.get(vagt(other)).status_code == 404
    assert c.post(vagt(other, "taget")).status_code == 404
    assert c.post(vagt(other, "udbedret")).status_code == 404
    other.refresh_from_db()
    assert other.signed_off_at is None
    page = c.get(MINE).content.decode()
    assert "Sal, stuen" in page and amalie.first_name not in page


def test_sign_off_records_the_time_and_can_be_undone_until_inspected(
    today_rota: Rota, trio: list[Resident], inspektor: Resident
) -> None:
    a = today_rota.assignments.get(date=timezone.localdate())
    c = as_user(trio[0])
    assert c.post(vagt(a, "taget"), {"next": "mine"}).url == MINE
    a.refresh_from_db()
    assert a.signed_off_at is not None and a.signed_off_by == trio[0]
    c.post(vagt(a, "taget"), {"undo": "1"})
    a.refresh_from_db()
    assert a.signed_off_at is None
    c.post(vagt(a, "taget"))
    Inspection.objects.create(assignment=a, outcome=Inspection.Outcome.GODKENDT, decided_at=timezone.now())
    c.post(vagt(a, "taget"), {"undo": "1"})
    a.refresh_from_db()
    assert a.signed_off_at is not None  # judged: the sign-off is history now


def test_sign_off_is_not_open_before_the_day(today_rota: Rota, trio: list[Resident]) -> None:
    later = today_rota.assignments.get(date=timezone.localdate() + datetime.timedelta(weeks=3))
    assert later.resident == trio[0]
    as_user(trio[0]).post(vagt(later, "taget"))
    later.refresh_from_db()
    assert later.signed_off_at is None


def test_inspektionen_may_sign_off_on_behalf(
    today_rota: Rota, trio: list[Resident], inspektor: Resident
) -> None:
    a = today_rota.assignments.get(date=timezone.localdate())
    as_user(inspektor).post(vagt(a, "taget"))
    a.refresh_from_db()
    assert a.signed_off_by == inspektor and a.resident == trio[0]


def test_mark_fixed_only_by_the_alumne_and_only_on_mangler(
    today_rota: Rota, trio: list[Resident], inspektor: Resident
) -> None:
    a = today_rota.assignments.get(date=timezone.localdate())
    c = as_user(trio[0])
    c.post(vagt(a, "udbedret"))  # nothing to fix yet
    assert not Inspection.objects.exists()
    now = timezone.now()
    r1 = Inspection.objects.create(
        assignment=a,
        inspector=inspektor,
        outcome=Inspection.Outcome.MANGLER,
        decided_at=now,
        deadline=default_fix_deadline(now),
        missing=[{"zone": "Toilet og bad", "text": "Håndsæbe fyldt op"}],
    )
    page = c.get(MINE).content.decode()
    assert "Håndsæbe fyldt op" in page and "Mangler er udbedret" in page
    assert as_user(inspektor).post(vagt(a, "udbedret")).status_code == 404  # not the alumne
    c.post(vagt(a, "udbedret"))
    r1.refresh_from_db()
    assert r1.fixed_at is not None
    a = CleaningAssignment.objects.get(pk=a.pk)
    assert a.status() == AssignmentStatus.UDBEDRET


def test_dashboard_shows_the_next_cleaning(
    today_rota: Rota, trio: list[Resident], make_resident: Callable[..., Resident]
) -> None:
    page = as_user(trio[1]).get("/intern/").content.decode()
    assert "Sal, stuen" in page and "Min rengøring" in page
    nobody = make_resident(email="ingen@gahk.dk")
    assert as_user(nobody).get("/intern/").status_code == 200
    assert as_user(nobody).get(MINE).status_code == 200


def test_an_open_mangler_outranks_the_next_sunday(areas: list[CleaningArea], trio: list[Resident]) -> None:
    stuen = CleaningArea.objects.get(slug="sal-stuen")
    last_week = timezone.localdate() - datetime.timedelta(weeks=1)
    rota = save_rota("E26", plan_rota(last_week, 4, 0, {stuen: [trio[0]]}), None)
    old = rota.assignments.get(date=last_week)
    Inspection.objects.create(
        assignment=old,
        outcome=Inspection.Outcome.MANGLER,
        decided_at=timezone.now(),
        deadline=timezone.now() + datetime.timedelta(hours=3),
    )
    assert next_for(trio[0]) == old
    old.inspections.update(fixed_at=timezone.now())
    assert next_for(trio[0]) == rota.assignments.get(date=last_week + datetime.timedelta(weeks=1))
