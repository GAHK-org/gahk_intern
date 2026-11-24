"""Stages from the alumneliste, grants, and what they open (access.stages, access.roles, the Adgang
page). Marked real_stages: an account never on a list has no access here, as in production."""

import datetime
from collections.abc import Callable
from itertools import count

import pytest
from django.core import mail
from django.test import Client

from access.api import KITCHEN, RESIDENT, Stage, holders, holding, real_roles, roles_of_many, user_can
from access.models import Grant
from access.stages import from_months
from core.clock import current_date
from core.models import Room
from events.models import CalendarFeedToken
from notifications import services as notifications
from notifications.models import Notification
from residents.models import Residency, Resident, Role, RoleAssignment, active_period

pytestmark = [pytest.mark.django_db, pytest.mark.real_stages]

_numbers = count(400)


def _month(offset: int) -> tuple[int, int]:
    """The month `offset` months from the calendar month (negative: ago)."""
    today = current_date()
    index = today.year * 12 + today.month - 1 + offset
    return index // 12, index % 12 + 1


def _on_list(resident: Resident, *offsets: int) -> Resident:
    for offset in offsets:
        n = next(_numbers)
        room = Room.objects.create(legacy_index=n, number=n, floor="stuen", side="mod gaden")
        year, month = _month(offset)
        Residency.objects.create(resident=resident, room=room, year=year, month=month)
    return resident


@pytest.fixture
def house(make_resident: Callable[..., Resident]) -> dict[str, Resident]:
    """One account in each stage, with this month's list in effect."""
    accounts = {
        "resident": _on_list(make_resident(email="bor@gahk.dk"), -2, -1, 0),
        "moving_in": _on_list(make_resident(email="ny@gahk.dk"), 1),
        "under_1": _on_list(make_resident(email="u1@gahk.dk"), -3, -2, -1),
        "1_to_3": _on_list(make_resident(email="u3@gahk.dk"), -3),
        "3_to_12": _on_list(make_resident(email="u12@gahk.dk"), -9),
        "over_a_year": _on_list(make_resident(email="gl@gahk.dk"), -30),
        "never": make_resident(email="aldrig@gahk.dk"),
        "kitchen": make_resident(email="kok@gahk.dk", is_kitchen=True),
    }
    assert active_period() == _month(0)
    return accounts


def _client(user: Resident) -> Client:
    client = Client()
    client.force_login(user)
    return client


# ---- the rule --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("last_past", "last", "stage"),
    [
        (100, 100, Stage.RESIDENT),
        (100, 101, Stage.RESIDENT),
        (None, 101, Stage.MOVING_IN),
        (90, 101, Stage.MOVING_IN),  # moving back in
        (99, 99, Stage.MOVED_OUT_UNDER_1_MONTH),
        (98, 98, Stage.MOVED_OUT_1_TO_3_MONTHS),
        (97, 97, Stage.MOVED_OUT_1_TO_3_MONTHS),
        (96, 96, Stage.MOVED_OUT_3_TO_12_MONTHS),
        (88, 88, Stage.MOVED_OUT_3_TO_12_MONTHS),
        (87, 87, Stage.MOVED_OUT_OVER_A_YEAR),
        (None, None, None),
    ],
)
def test_the_months_on_the_list_decide_the_stage(
    last_past: int | None, last: int | None, stage: Stage | None
) -> None:
    assert from_months(100, last_past, last) == stage


def test_each_account_gets_its_stage(house: dict[str, Resident]) -> None:
    roles = roles_of_many(a.pk for a in house.values())
    assert roles[house["resident"].pk] == {Stage.RESIDENT}
    assert roles[house["moving_in"].pk] == {Stage.MOVING_IN}
    assert roles[house["under_1"].pk] == {Stage.MOVED_OUT_UNDER_1_MONTH}
    assert roles[house["1_to_3"].pk] == {Stage.MOVED_OUT_1_TO_3_MONTHS}
    assert roles[house["3_to_12"].pk] == {Stage.MOVED_OUT_3_TO_12_MONTHS}
    assert roles[house["over_a_year"].pk] == {Stage.MOVED_OUT_OVER_A_YEAR}
    assert roles[house["never"].pk] == frozenset()
    assert roles[house["kitchen"].pk] == {KITCHEN}


def test_sql_and_python_agree_on_who_holds_what(house: dict[str, Resident]) -> None:
    """holders() is the same rule in SQL: checked for every role over every account."""
    python = roles_of_many(a.pk for a in house.values())
    for role in {*(s.value for s in Stage), KITCHEN}:
        expected = {pk for pk, roles in python.items() if role in roles}
        assert set(holding({role}).values_list("pk", flat=True)) == expected, role


def test_a_group_or_superuser_makes_someone_a_resident(
    house: dict[str, Resident], make_resident: Callable[..., Resident]
) -> None:
    year, month = active_period()
    RoleAssignment.objects.create(resident=house["over_a_year"], role=Role.REGNSKAB, year=year, month=month)
    superuser = make_resident(email="super@gahk.dk", is_superuser=True)
    assert real_roles(Resident.objects.get(pk=house["over_a_year"].pk)) == {RESIDENT, Role.REGNSKAB}
    assert Role.ADMINISTRATOR in real_roles(superuser) and RESIDENT in real_roles(superuser)
    assert house["over_a_year"] in holders("dashboard.view")


def test_a_grant_holds_from_its_first_day_through_its_last(house: dict[str, Resident]) -> None:
    gone = house["over_a_year"]
    today = current_date()
    grant = Grant.objects.create(
        resident=gone,
        role=RESIDENT,
        reason="Gør regnskabet færdigt",
        starts_on=today + datetime.timedelta(days=1),
    )
    assert real_roles(Resident.objects.get(pk=gone.pk)) == {Stage.MOVED_OUT_OVER_A_YEAR}
    grant.starts_on = today
    grant.ends_on = today
    grant.save()
    assert real_roles(Resident.objects.get(pk=gone.pk)) == {Stage.MOVED_OUT_OVER_A_YEAR, RESIDENT}
    assert gone in holders("ak.use")
    grant.starts_on = grant.ends_on = today - datetime.timedelta(days=1)
    grant.save()
    assert gone not in holders("ak.use")


# ---- what the stages open --------------------------------------------------------------------------


def test_moving_in_has_all_but_the_photo_album_and_gahkflix(house: dict[str, Resident]) -> None:
    c = _client(house["moving_in"])
    assert c.get("/alumneliste/").status_code == 200
    assert c.get("/fotoalbum/").status_code == 403


def test_one_to_three_months_out_may_look_at_ak_but_not_add(house: dict[str, Resident]) -> None:
    c = _client(house["1_to_3"])
    assert c.get("/ak/").status_code == 200
    assert c.post("/ak/tilfoej", {}).status_code == 403
    assert c.get("/reparationer/").status_code == 403
    assert c.get("/soegvaerelse/").status_code == 403


def test_three_to_twelve_months_out_keeps_the_social_side(house: dict[str, Resident]) -> None:
    c = _client(house["3_to_12"])
    assert c.get("/opslagstavle/").status_code == 200
    assert c.get("/alumneliste/").status_code == 403
    response = c.get("/")
    assert response.status_code == 302 and response["Location"] == "/den-hurtige/"


def test_over_a_year_out_keeps_the_stamtree(house: dict[str, Resident]) -> None:
    c = _client(house["over_a_year"])
    assert c.get("/stamtree/").status_code == 200
    assert c.get("/den-hurtige/").status_code == 403
    assert c.get("/", follow=True).redirect_chain[-1][0] == "/stamtree/"


def test_never_on_a_list_has_no_access(house: dict[str, Resident]) -> None:
    c = _client(house["never"])
    response = c.get("/")
    assert response.status_code == 403 and "Ingen adgang" in response.content.decode()
    assert c.get("/admin/password-change").status_code == 200


def test_nobody_is_told_about_a_page_they_cannot_open(house: dict[str, Resident]) -> None:
    ids = [house["resident"].pk, house["3_to_12"].pk, house["over_a_year"].pk]
    notifications.notify(ids, category="general", title="Ny i AK", url="/ak/", background=False)
    told = set(Notification.objects.values_list("recipient_id", flat=True))
    assert told == {house["resident"].pk}


def test_the_calendar_feed_empties_with_begivenheder(house: dict[str, Resident]) -> None:
    assert not user_can(house["over_a_year"], "events.view")
    token = CalendarFeedToken.objects.create(resident=house["over_a_year"])
    response = Client().get(f"/kalender/{token.token}.ics")
    assert response.status_code == 200 and b"VEVENT" not in response.content


def test_an_administrator_previews_a_stage(
    house: dict[str, Resident], make_resident: Callable[..., Resident]
) -> None:
    admin = _client(make_resident(email="admin@gahk.dk", is_superuser=True))
    admin.post("/admin/preview/set", {"mode": "stage", "stage": Stage.MOVED_OUT_OVER_A_YEAR})
    assert admin.get("/alumneliste/").status_code == 403
    assert admin.get("/stamtree/").status_code == 200
    admin.post("/admin/preview/set", {"mode": "stage", "stage": ""})
    assert admin.get("/").status_code == 403


# ---- the Adgang page ---------------------------------------------------------------------------------


def _with_role(make_resident: Callable[..., Resident], email: str, role: str) -> Client:
    return _client(_on_list(make_resident(email=email, roles=(role,)), 0))


def test_koekkengruppen_adds_one_of_the_kitchen_staff(
    house: dict[str, Resident], make_resident: Callable[..., Resident]
) -> None:
    c = _with_role(make_resident, "kg@gahk.dk", Role.KOKKENGRUPPE)
    assert "Køkkenpersonale" in c.get("/access/").content.decode()
    c.post("/access/kitchen-staff/add", {"first_name": "Mette", "last_name": "Kok", "email": "mette@kok.dk"})
    mette = Resident.objects.get(email="mette@kok.dk")
    assert real_roles(mette) == {KITCHEN}
    assert not mette.has_usable_password()
    assert mail.outbox[-1].to == ["mette@kok.dk"]
    assert _client(mette).get("/den-hurtige/koekken/").status_code == 200


def test_koekkengruppen_ends_the_kitchens_access_but_no_other(
    house: dict[str, Resident], make_resident: Callable[..., Resident]
) -> None:
    c = _with_role(make_resident, "kg@gahk.dk", Role.KOKKENGRUPPE)
    kitchen = Grant.objects.get(resident=house["kitchen"])
    other = Grant.objects.create(
        resident=house["over_a_year"],
        role=RESIDENT,
        reason="Gæst",
        starts_on=current_date() - datetime.timedelta(days=5),
    )
    assert c.post(f"/access/grants/{other.pk}/end").status_code == 403
    c.post(f"/access/grants/{kitchen.pk}/end")
    assert real_roles(Resident.objects.get(pk=house["kitchen"].pk)) == frozenset()
    assert "Givet i hånden" not in c.get("/access/").content.decode()


def test_the_indstilling_gives_a_role_from_the_edit_page(
    house: dict[str, Resident], make_resident: Callable[..., Resident]
) -> None:
    c = _with_role(make_resident, "ind@gahk.dk", Role.INDSTILLING)
    gone = house["1_to_3"]
    page = c.get(f"/beboer/{gone.pk}/rediger").content.decode()
    assert "Fraflyttet for 1–3 måneder siden" in page and "Giv en rolle" in page
    c.post(
        f"/access/grants/{gone.pk}/add",
        {
            "role": Role.OELKAELDER,
            "reason": "Afleverer kassen",
            "starts_on": current_date().isoformat(),
            "ends_on": "",
        },
    )
    assert user_can(Resident.objects.get(pk=gone.pk), "oelkaelder.manage")
    assert Grant.objects.get(resident=gone).granted_by.email == "ind@gahk.dk"


def test_a_plain_resident_has_no_adgang_page(house: dict[str, Resident]) -> None:
    c = _client(house["resident"])
    assert c.get("/access/").status_code == 403
    assert c.post(f"/access/grants/{house['resident'].pk}/add", {}).status_code == 403


def test_administrator_cannot_be_given_by_hand(
    house: dict[str, Resident], make_resident: Callable[..., Resident]
) -> None:
    c = _with_role(make_resident, "ind@gahk.dk", Role.INDSTILLING)
    c.post(
        f"/access/grants/{house['resident'].pk}/add",
        {"role": Role.ADMINISTRATOR, "reason": "x", "starts_on": current_date().isoformat()},
    )
    assert not Grant.objects.filter(role=Role.ADMINISTRATOR).exists()
