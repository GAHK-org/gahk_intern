"""Søndagsrengøring — rotas, the alumne's sign-off and Inspektionen's check (spec/features/rengoering.md).

Most of what is worth testing is two kinds of rule:

  * WHO may see and do what — a beboer sees only their own assignments (others' are 404, not 403),
    and only Inspektionen of the *active* month, administrators and superusers plan and inspect;
  * the derived STATUS of an assignment, which every page reads instead of a stored column.
"""

import datetime
import io
from collections.abc import Callable
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.test import Client
from django.utils import timezone

from core.models import Room
from rengoering.areas_data import AREAS
from rengoering.importing import match_areas, match_name, parse
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
from rengoering.services import RotaClash, load_areas, next_for, plan_rota, record, save_rota, visible_tasks
from residents.models import (
    Residency,
    Resident,
    Role,
    RoleAssignment,
    active_period,
    next_period,
    prev_period,
)

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


def test_a_newcomer_on_next_months_list_can_take_over(
    areas: list[CleaningArea],
    trio: list[Resident],
    inspektor: Resident,
    make_resident: Callable[..., Resident],
) -> None:
    """Indstillingen prepares next month's alumneliste ahead of time, so the one moving in can be put
    on the departing alumne's Sundays before they arrive. Last month's alumner cannot."""
    stuen = CleaningArea.objects.get(slug="sal-stuen")
    rota = save_rota("E26", plan_rota(SUNDAY, 9, 5, {stuen: trio}), None)
    year, month = next_period()
    newcomer = make_resident(email="ny@gahk.dk", first_name="Ny")
    room = Room.objects.create(legacy_index=99, number=99, floor="stuen", side="mod gaden")
    Residency.objects.create(resident=newcomer, room=room, year=year, month=month)
    gone = make_resident(email="flyttet@gahk.dk", first_name="Flyttet")
    year, month = prev_period()
    Residency.objects.create(resident=gone, room=room, year=year, month=month)

    c = as_user(inspektor)
    page = c.get(f"{ROTAS}{rota.pk}").content.decode()
    assert "Ny Beboer" in page and "Flyttet Beboer" not in page
    data = {"old": trio[0].pk, "new": newcomer.pk, "from_date": "2026-11-01"}
    assert c.post(f"{ROTAS}{rota.pk}/erstat", data).status_code == 302
    assert rota.assignments.filter(resident=newcomer).count() == 3


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


# ---- inspection, overview, history (phase 4) ----

TJEK = "/intern/rengoering/tjek/"


def save_url(a: CleaningAssignment) -> str:
    return f"{TJEK}vagt/{a.pk}"


@pytest.fixture
def sunday(areas: list[CleaningArea], trio: list[Resident]) -> CleaningAssignment:
    """Wilma's Sunday on Sal, stuen — a past Sunday, so the walk is open."""
    stuen = CleaningArea.objects.get(slug="sal-stuen")
    rota = save_rota("E26", plan_rota(SUNDAY, 6, 5, {stuen: trio}), None)
    return rota.assignments.get(date=SUNDAY)


def fresh(a: CleaningAssignment) -> CleaningAssignment:
    return CleaningAssignment.objects.prefetch_related("inspections").get(pk=a.pk)


def test_autosave_opens_round_one_and_keeps_only_visible_tasks(
    sunday: CleaningAssignment, inspektor: Resident
) -> None:
    star = sunday.area.tasks.filter(star_only=True).first()
    some = list(visible_tasks(sunday).values_list("pk", flat=True)[:3])
    assert star and star.pk not in some
    r = as_user(inspektor).post(
        save_url(sunday),
        {"task": [*some, star.pk], "note": "  hår i afløbet ", "action": "gem"},
        headers={"x-requested-with": "XMLHttpRequest"},
    )
    assert r.json() == {"ok": True, "done": 3}  # the star task is not on this week's list
    i = Inspection.objects.get(assignment=sunday)
    assert (i.round, i.outcome, i.note, i.inspector) == (1, "", "hår i afløbet", inspektor)


def test_godkend_ticks_everything(sunday: CleaningAssignment, inspektor: Resident) -> None:
    r = as_user(inspektor).post(save_url(sunday), {"action": "godkend"})
    assert r.url == f"{TJEK}{SUNDAY.isoformat()}?aaben=sal-stuen#sal-stuen"
    i = Inspection.objects.get(assignment=sunday)
    assert i.outcome == "godkendt" and i.missing == [] and i.deadline is None
    assert i.checked_tasks.count() == visible_tasks(sunday).count()
    assert fresh(sunday).status() == AssignmentStatus.GODKENDT
    # approved is closed: a stray autosave is refused, not a new round
    r = as_user(inspektor).post(
        save_url(sunday), {"action": "gem"}, headers={"x-requested-with": "XMLHttpRequest"}
    )
    assert r.json()["ok"] is False and Inspection.objects.count() == 1


def test_mangler_freezes_what_was_missing_then_a_follow_up_approves(
    sunday: CleaningAssignment, inspektor: Resident, trio: list[Resident]
) -> None:
    tasks = list(visible_tasks(sunday))
    ticked = [t.pk for t in tasks[1:]]
    c = as_user(inspektor)
    c.post(save_url(sunday), {"action": "mangler", "task": ticked, "note": "Toiletpapir", "deadline": ""})
    first = Inspection.objects.get(assignment=sunday)
    assert first.missing == [{"zone": tasks[0].zone, "text": tasks[0].text}]
    assert timezone.localtime(first.deadline).hour == 22  # type: ignore[arg-type]
    # the task list is edited afterwards; history still says what was missing
    CleaningTask.objects.filter(pk=tasks[0].pk).update(text="omdøbt")
    first.refresh_from_db()
    assert first.missing[0]["text"] == tasks[0].text
    # the alumne reports it fixed, Inspektionen re-checks: round 2, starting from round 1's ticks
    as_user(trio[0]).post(vagt(sunday, "udbedret"))
    page = c.get(f"{TJEK}{SUNDAY.isoformat()}").content.decode()
    assert "Opfølgning" in page
    c.post(save_url(sunday), {"action": "godkend"})
    rounds = list(Inspection.objects.filter(assignment=sunday).order_by("round"))
    assert [(i.round, i.outcome) for i in rounds] == [(1, "mangler"), (2, "godkendt")]
    assert fresh(sunday).status() == AssignmentStatus.GODKENDT


def test_mangler_takes_an_explicit_deadline(sunday: CleaningAssignment, inspektor: Resident) -> None:
    as_user(inspektor).post(save_url(sunday), {"action": "mangler", "deadline": "2026-11-03T18:00"})
    i = Inspection.objects.get(assignment=sunday)
    assert i.deadline == local(datetime.date(2026, 11, 3), 18)


def test_a_mis_tapped_decision_can_be_undone(sunday: CleaningAssignment, inspektor: Resident) -> None:
    c = as_user(inspektor)
    c.post(save_url(sunday), {"action": "godkend"})
    c.post(save_url(sunday), {"action": "fortryd"})
    i = Inspection.objects.get(assignment=sunday)
    assert (i.outcome, i.decided_at) == ("", None)
    assert fresh(sunday).status() == AssignmentStatus.IKKE_TAGET  # back to how it was before


def test_inspection_page_hides_star_tasks_outside_star_weeks(
    sunday: CleaningAssignment, inspektor: Resident
) -> None:
    star_text = sunday.area.tasks.filter(star_only=True).first().text  # type: ignore[union-attr]
    page = as_user(inspektor).get(f"{TJEK}{SUNDAY.isoformat()}").content.decode()
    assert "Sal, stuen" in page and "Wilma" in page and star_text not in page
    star_sunday = SUNDAY + datetime.timedelta(weeks=4)
    assert CleaningAssignment.objects.get(date=star_sunday).is_star_week
    assert star_text in as_user(inspektor).get(f"{TJEK}{star_sunday.isoformat()}").content.decode()


def test_tjek_defaults_to_the_latest_sunday_not_in_the_future(
    sunday: CleaningAssignment, inspektor: Resident
) -> None:
    r = as_user(inspektor).get(TJEK)
    latest = max(
        d for d in CleaningAssignment.objects.values_list("date", flat=True) if d <= timezone.localdate()
    )
    assert r.url == f"{TJEK}{latest.isoformat()}"
    assert as_user(inspektor).get(f"{TJEK}ikke-en-dato").status_code == 404


def test_history_counts_repeat_offences(
    sunday: CleaningAssignment, inspektor: Resident, trio: list[Resident]
) -> None:
    c = as_user(inspektor)
    c.post(save_url(sunday), {"action": "mangler", "deadline": "2026-11-02T22:00"})  # never fixed
    page = c.get("/intern/rengoering/historik/").content.decode()
    assert "Wilma" in page and "Amalie" not in page  # only Sundays that have been
    r = record([fresh(sunday)], now=local(SUNDAY + datetime.timedelta(days=2), 12))  # deadline has passed
    assert (r.total, r.with_mangler, r.late, r.not_signed_off, r.approved_first_time) == (1, 1, 1, 1, 0)
    assert c.get(f"/intern/rengoering/historik/beboer/{trio[0].pk}").status_code == 200
    assert c.get("/intern/rengoering/historik/omraade/sal-stuen").status_code == 200
    assert "Wilma" in c.get(f"/intern/rengoering/oversigt/{SUNDAY.isoformat()}").content.decode()


def test_a_re_check_before_the_deadline_is_not_late(sunday: CleaningAssignment, inspektor: Resident) -> None:
    c = as_user(inspektor)
    c.post(save_url(sunday), {"action": "mangler", "deadline": "2099-01-01T22:00"})
    c.post(save_url(sunday), {"action": "godkend"})  # re-checked without the alumne pressing "udbedret"
    assert record([fresh(sunday)]).late == 0


@pytest.mark.parametrize(
    "url",
    [
        TJEK,
        TJEK + "2026-11-01",
        "/intern/rengoering/oversigt/",
        "/intern/rengoering/historik/",
        "/intern/rengoering/historik/omraade/sal-stuen",
        "/intern/rengoering/historik/beboer/{me}",
    ],
)
def test_inspection_pages_are_for_inspektionen_only(
    url: str, sunday: CleaningAssignment, trio: list[Resident]
) -> None:
    wilma = trio[0]
    assert as_user(wilma).get(url.format(me=wilma.pk)).status_code == 403  # not even her own history


def test_a_beboer_cannot_inspect_even_their_own_sunday(
    sunday: CleaningAssignment, trio: list[Resident]
) -> None:
    assert as_user(trio[0]).post(save_url(sunday), {"action": "godkend"}).status_code == 403
    assert not Inspection.objects.exists()


def test_last_months_inspektion_cannot_inspect(
    sunday: CleaningAssignment, make_resident: Callable[..., Resident]
) -> None:
    old = make_resident(email="gammel@gahk.dk")
    year, month = active_period()
    prev = (year, month - 1) if month > 1 else (year - 1, 12)
    RoleAssignment.objects.create(resident=old, role=Role.INSPEKTION, year=prev[0], month=prev[1])
    assert as_user(old).post(save_url(sunday), {"action": "godkend"}).status_code == 403
    assert as_user(old).get(TJEK).status_code == 403


@pytest.fixture
def outgoing(make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch) -> Resident:
    """September's Inspektion, with October — the month after the generalforsamling — active."""
    monkeypatch.setattr("residents.permissions.active_period", lambda: (2026, 10))
    old = make_resident(email="gammel@gahk.dk", first_name="Gorm")
    RoleAssignment.objects.create(resident=old, role=Role.INSPEKTION, year=2026, month=9)
    return old


def test_the_outgoing_inspektion_still_inspects_in_the_handover_month(
    sunday: CleaningAssignment, outgoing: Resident
) -> None:
    c = as_user(outgoing)
    assert c.get(TJEK + "2026-11-01").status_code == 200
    for url in [
        "/intern/rengoering/oversigt/",
        "/intern/rengoering/historik/",
        f"/intern/rengoering/vagt/{sunday.pk}",
    ]:
        assert c.get(url).status_code == 200, url
    assert c.post(save_url(sunday), {"action": "godkend"}).status_code == 302
    assert fresh(sunday).status() == AssignmentStatus.GODKENDT
    nav = c.get(TJEK + "2026-11-01").content.decode()
    assert "Rengøringstjek" in nav and "Rengøringslister" not in nav


def test_the_outgoing_inspektion_does_not_plan_rotas(sunday: CleaningAssignment, outgoing: Resident) -> None:
    c = as_user(outgoing)
    assert c.get(ROTAS).status_code == 403
    assert c.get(f"/intern/rengoering/vagt/{sunday.pk}/rediger").status_code == 403


@pytest.mark.parametrize("active", [(2026, 5), (2026, 11)])
def test_the_exception_ends_with_the_handover_month(
    active: tuple[int, int],
    sunday: CleaningAssignment,
    make_resident: Callable[..., Resident],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("residents.permissions.active_period", lambda: active)
    old = make_resident(email="gammel@gahk.dk")
    prev = (active[0], active[1] - 1)  # April's or October's Inspektion
    RoleAssignment.objects.create(resident=old, role=Role.INSPEKTION, year=prev[0], month=prev[1])
    assert as_user(old).get(TJEK).status_code == 403


# The buttons, not the sidebar's entries for the same pages.
TJEK_BUTTON = 'class="btn" href="/intern/rengoering/tjek/"'
LISTER_BUTTON = 'class="btn btn-secondary" href="/intern/rengoering/lister/"'


def test_min_rengoering_links_inspektionen_to_their_pages(
    sunday: CleaningAssignment, inspektor: Resident, trio: list[Resident]
) -> None:
    page = as_user(inspektor).get("/intern/rengoering/").content.decode()
    assert TJEK_BUTTON in page and LISTER_BUTTON in page
    page = as_user(trio[0]).get("/intern/rengoering/").content.decode()
    assert TJEK_BUTTON not in page and LISTER_BUTTON not in page


def test_min_rengoering_links_the_outgoing_inspektion_to_the_walk_only(
    sunday: CleaningAssignment, outgoing: Resident
) -> None:
    page = as_user(outgoing).get("/intern/rengoering/").content.decode()
    assert TJEK_BUTTON in page and LISTER_BUTTON not in page


# ---- import (phase 5) ----

# The shape `pdftotext -layout` makes of the printed list — fake names, real layout. The last area
# is a køkkenrengøring, which this feature skips.
LAYOUT = """Stuen, Gang, Toilet
Dato         Hvem       Stjerne   Taget   Kommentar   Godkendt
01/11/2026   Wilma
08/11/2026   Amalie
15/11/2026   Isabella
22/11/2026   Wilma P
29/11/2026   Anton      *
06/12/2026
Varme trappe + Musik
Dato         Hvem             Stjerne   Taget   Kommentar   Godkendt
01/11/2026   Tobias Hermann
08/11/2026   Kim
Kolde køkken
Dato         Hvem       Stjerne   Taget   Kommentar   Godkendt
01/11/2026   Wilma
"""


def test_parse_layout_text_reads_dates_names_and_stars() -> None:
    parsed = parse(LAYOUT)
    assert parsed.errors == []
    stuen = [r for r in parsed.rows if r.header == "Stuen, Gang, Toilet"]
    assert [r.name for r in stuen] == ["Wilma", "Amalie", "Isabella", "Wilma P", "Anton", ""]
    assert [r.date for r in stuen if r.star] == [datetime.date(2026, 11, 29)]
    assert {r.header for r in parsed.rows} == {"Stuen, Gang, Toilet", "Varme trappe + Musik", "Kolde køkken"}


def test_parse_csv() -> None:
    parsed = parse("område;dato;navn;stjerne\nSal, stuen;01/11/2026;Wilma;\nSal, stuen;2026-11-29;Amalie;*\n")
    assert [(r.header, r.date, r.name, r.star) for r in parsed.rows] == [
        ("Sal, stuen", datetime.date(2026, 11, 1), "Wilma", False),
        ("Sal, stuen", datetime.date(2026, 11, 29), "Amalie", True),
    ]
    assert parse("område;dato;navn\nSal, stuen;31/02/2026;Wilma\n").errors  # no such date


def test_area_headers_match_names_and_aliases(areas: list[CleaningArea]) -> None:
    found = match_areas(["Stuen, Gang, Toilet", "varme trappe + musik", "Læsesalen", "Kolde køkken"], areas)
    assert {h: a.slug if a else None for h, a in found.items()} == {
        "Stuen, Gang, Toilet": "sal-stuen",
        "varme trappe + musik": "varme-trappe",
        "Læsesalen": "laesesalen",
        "Kolde køkken": None,
    }


@pytest.fixture
def house(make_resident: Callable[..., Resident]) -> dict[str, Resident]:
    people = {
        "wilma": ("Wilma", "Holm"),
        "william_p": ("William", "Pedersen"),
        "william_s": ("William", "Svendsen"),
        "tobias_h": ("Tobias", "Hermann"),
        "tobias_k": ("Tobias", "Krog"),
        "anton_c": ("Anton Claes", "Berg"),
        "anton_m": ("Anton", "Mosquera Diaz"),
        "anton_x": ("Anton", "Xu"),
        "gustav": ("Gustav", "Bach Skovsager"),
    }
    return {
        key: make_resident(email=f"{key}@gahk.dk", first_name=first, last_name=last)
        for key, (first, last) in people.items()
    }


@pytest.mark.parametrize(
    ("name", "expected", "state"),
    [
        ("Wilma", "wilma", "match"),  # the only Wilma
        ("william p", "william_p", "match"),  # initial of the last name, any case
        ("Tobias Hermann", "tobias_h", "match"),  # full name
        ("Anton Claes", "anton_c", "match"),  # a two-word first name
        ("Anton Mosquera", "anton_m", "match"),  # start of a two-word last name
        ("Gustav Skovsager", "gustav", "match"),  # a later word of the last name
        ("William", None, "flere"),  # two Williams: choose by hand
        ("Anton", None, "flere"),
        ("Kim", None, "ingen"),
    ],
)
def test_match_name(name: str, expected: str | None, state: str, house: dict[str, Resident]) -> None:
    m = match_name(name, list(house.values()))
    assert m.state == state
    assert m.resident == (house[expected] if expected else None)
    if state == "flere":
        assert len(m.candidates) >= 2


IMPORT = "/intern/rengoering/lister/importer"


def test_import_matches_names_then_saves_after_they_are_settled(
    areas: list[CleaningArea], house: dict[str, Resident], inspektor: Resident
) -> None:
    live_here(*house.values())
    c = as_user(inspektor)
    r = c.post(IMPORT, {"step": "laes", "name": "E26", "text": LAYOUT})
    assert r.status_code == 302
    page = c.get(IMPORT).content.decode()
    assert "Kolde køkken" in page  # reported as skipped
    assert "flere mulige" in page and "ikke fundet" in page  # Anton, Kim
    assert "Wilma P" in page and "Tobias Hermann" in page

    names = ["Wilma", "Amalie", "Isabella", "Wilma P", "Anton", "Tobias Hermann", "Kim"]
    picks = {
        "Wilma": house["wilma"].pk,
        "Amalie": "ingen",
        "Isabella": "ingen",
        "Wilma P": house["wilma"].pk,
        "Anton": house["anton_x"].pk,
        "Tobias Hermann": house["tobias_h"].pk,
    }
    # Kim left unchosen → nothing is saved
    data = {"step": "gem"} | {f"navn_{i}": str(picks.get(n, "")) for i, n in enumerate(names)}
    r = c.post(IMPORT, data)
    assert r.status_code == 200 and not Rota.objects.exists()
    data[f"navn_{names.index('Kim')}"] = "ingen"
    r = c.post(IMPORT, data)
    rota = Rota.objects.get(name="E26")
    assert r.url == f"{ROTAS}{rota.pk}"
    assert rota.assignments.count() == 8  # 6 + 2; the køkken row skipped
    star = rota.assignments.get(is_star_week=True)
    assert (star.date, star.resident) == (datetime.date(2026, 11, 29), house["anton_x"])
    assert rota.assignments.get(area__slug="sal-stuen", date=datetime.date(2026, 12, 6)).resident is None
    assert c.get(IMPORT).status_code == 200  # the session is cleared: back at step 1


def test_import_is_for_inspektionen_only(areas: list[CleaningArea], beboer: Resident) -> None:
    c = as_user(beboer)
    assert c.get(IMPORT).status_code == 403
    assert c.post(IMPORT, {"step": "laes", "name": "E26", "text": LAYOUT}).status_code == 403


def test_import_command_refuses_unresolved_names_and_takes_a_map(
    areas: list[CleaningArea], house: dict[str, Resident], tmp_path: Path
) -> None:
    live_here(*house.values())
    src = tmp_path / "liste.txt"
    src.write_text(LAYOUT, encoding="utf-8")
    with pytest.raises(CommandError):
        call_command(
            "import_cleaning_rota", str(src), "--name", "E26", stdout=io.StringIO(), stderr=io.StringIO()
        )
    assert not Rota.objects.exists()
    call_command(
        "import_cleaning_rota",
        str(src),
        "--name",
        "E26",
        "--dry-run",
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert not Rota.objects.exists()
    call_command(
        "import_cleaning_rota",
        str(src),
        "--name",
        "E26",
        "--map",
        "Amalie=",
        "--map",
        "Isabella=",
        "--map",
        "Anton=anton_x@gahk.dk",
        "--map",
        "Kim=",
        "--map",
        "Wilma P=wilma@gahk.dk",
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert Rota.objects.get(name="E26").assignments.count() == 8


def test_seed_demo_puts_every_state_on_screen(db: None) -> None:
    call_command("seed_demo", "--fresh", "--force", "--residents", "40", verbosity=0, stdout=io.StringIO())
    rota = Rota.objects.get(name="Demo")
    now = timezone.now()
    statuses = {
        a.status(now) for a in rota.assignments.prefetch_related("inspections").select_related("area")
    }
    assert {AssignmentStatus.GODKENDT, AssignmentStatus.MANGLER, AssignmentStatus.KOMMENDE} <= statuses
    demo = Resident.objects.get(email="beboer@gahk.dk")
    assert next_for(demo, now).status(now) == AssignmentStatus.MANGLER  # type: ignore[union-attr]
    assert "Mangler er udbedret" in as_user(demo).get("/intern/").content.decode()
