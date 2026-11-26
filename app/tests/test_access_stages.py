"""Roles from the facts: stages from the alumneliste, an embedsgruppe's role from the list in effect,
grants with dates, the kitchen staff, and what they open. Marked real_stages: an account never on a list has no access
here, as in production."""

import datetime
from collections.abc import Callable
from itertools import count

import pytest
from django.core import mail
from django.test import Client

from access.api import ADMINISTRATOR, KITCHEN, RESIDENT, Stage, holders, real_roles, roles_of_many, sync
from access.models import Grant
from access.stages import from_months
from core.clock import current_date
from core.models import Room, Workgroup
from events.models import CalendarFeedToken
from notifications import services as notifications
from notifications.models import Notification
from residents.models import Account, Residency, Role, active_period

pytestmark = [pytest.mark.django_db, pytest.mark.real_stages]

_numbers = count(400)


def _month(offset: int) -> tuple[int, int]:
    """The month `offset` months from the calendar month (negative: ago)."""
    today = current_date()
    index = today.year * 12 + today.month - 1 + offset
    return index // 12, index % 12 + 1


def _on_list(account: Account, *offsets: int, workgroup: str = "") -> Account:
    for offset in offsets:
        n = next(_numbers)
        room = Room.objects.create(legacy_index=n, number=n, floor="stuen", side="mod gaden")
        year, month = _month(offset)
        group = Workgroup.objects.get_or_create(name=workgroup)[0] if workgroup else None
        Residency.objects.create(resident=account.house, room=room, workgroup=group, year=year, month=month)
    return account


def _fresh(account: Account) -> Account:
    """The account read again, so real_roles reads its groups as they are now."""
    return Account.objects.get(pk=account.pk)


@pytest.fixture
def house(make_resident: Callable[..., Account]) -> dict[str, Account]:
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


def _client(user: Account) -> Client:
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


def test_each_account_gets_its_stage(house: dict[str, Account]) -> None:
    roles = roles_of_many(a.pk for a in house.values())
    assert roles[house["resident"].pk] == {Stage.RESIDENT}
    assert roles[house["moving_in"].pk] == {Stage.MOVING_IN}
    assert roles[house["under_1"].pk] == {Stage.MOVED_OUT_UNDER_1_MONTH}
    assert roles[house["1_to_3"].pk] == {Stage.MOVED_OUT_1_TO_3_MONTHS}
    assert roles[house["3_to_12"].pk] == {Stage.MOVED_OUT_3_TO_12_MONTHS}
    assert roles[house["over_a_year"].pk] == {Stage.MOVED_OUT_OVER_A_YEAR}
    assert roles[house["never"].pk] == frozenset()
    assert roles[house["kitchen"].pk] == {KITCHEN}


def test_the_groups_hold_what_the_rule_says(house: dict[str, Account]) -> None:
    """The sync wrote every account's stage into its groups when its months were saved."""
    expected = roles_of_many(a.pk for a in house.values())
    for account in house.values():
        assert real_roles(_fresh(account)) == expected[account.pk], account.email


def test_an_embedsgruppe_gives_its_role_while_on_the_list_in_effect(
    make_resident: Callable[..., Account],
) -> None:
    ak = _on_list(make_resident(email="ak@gahk.dk"), 0, workgroup="AK-gruppen")
    assert real_roles(_fresh(ak)) == {RESIDENT, Role.AK}
    assert ak.has_role(Role.AK) and not ak.has_role(Role.AK, _month(-1))
    Residency.objects.filter(resident=ak.house).update(workgroup=None)
    sync([ak.pk])
    assert real_roles(_fresh(ak)) == {RESIDENT}


def test_a_superuser_is_a_resident_and_an_administrator(make_resident: Callable[..., Account]) -> None:
    superuser = make_resident(email="super@gahk.dk", is_superuser=True)
    assert real_roles(superuser) == {RESIDENT, ADMINISTRATOR}


def test_an_administrator_grant_opens_everything_whatever_the_months(house: dict[str, Account]) -> None:
    gone = house["over_a_year"]
    Grant.objects.create(account=gone, role=ADMINISTRATOR, reason="Netværksgruppen", starts_on=current_date())
    assert real_roles(_fresh(gone)) == {Stage.MOVED_OUT_OVER_A_YEAR, ADMINISTRATOR}
    assert gone in holders("access.den_hurtige_use")
    assert _fresh(gone).is_staff


def test_a_grant_holds_from_its_first_day_through_its_last(house: dict[str, Account]) -> None:
    gone = house["over_a_year"]
    today = current_date()
    grant = Grant.objects.create(
        account=gone,
        role=Role.REGNSKAB,
        reason="Gør regnskabet færdigt",
        starts_on=today + datetime.timedelta(days=1),
    )
    assert real_roles(_fresh(gone)) == {Stage.MOVED_OUT_OVER_A_YEAR}
    grant.starts_on = grant.ends_on = today
    grant.save()
    assert real_roles(_fresh(gone)) == {Stage.MOVED_OUT_OVER_A_YEAR, Role.REGNSKAB}
    assert gone in holders("access.accounting_manage")
    grant.starts_on = grant.ends_on = today - datetime.timedelta(days=1)
    grant.save()
    assert gone not in holders("access.accounting_manage")


def test_the_nightly_sync_catches_a_grant_that_ran_out(house: dict[str, Account]) -> None:
    """Nothing is saved when a day passes: the sync just after midnight takes the role away."""
    gone = house["over_a_year"]
    yesterday = current_date() - datetime.timedelta(days=1)
    Grant.objects.create(account=gone, role=Role.REGNSKAB, reason="x", starts_on=yesterday)
    Grant.objects.filter(account=gone).update(ends_on=yesterday)  # as if a day had passed
    assert Role.REGNSKAB in real_roles(_fresh(gone))
    assert sync() >= 1
    assert Role.REGNSKAB not in real_roles(_fresh(gone))


# ---- what the stages open --------------------------------------------------------------------------


def test_moving_in_has_all_but_the_photo_album_and_gahkflix(house: dict[str, Account]) -> None:
    c = _client(house["moving_in"])
    assert c.get("/alumneliste/").status_code == 200
    assert c.get("/fotoalbum/").status_code == 403


def test_one_to_three_months_out_may_look_at_ak_but_not_add(house: dict[str, Account]) -> None:
    c = _client(house["1_to_3"])
    assert c.get("/ak/").status_code == 200
    assert c.post("/ak/tilfoej", {}).status_code == 403
    assert c.get("/reparationer/").status_code == 403
    assert c.get("/soegvaerelse/").status_code == 403


def test_three_to_twelve_months_out_keeps_the_social_side(house: dict[str, Account]) -> None:
    c = _client(house["3_to_12"])
    assert c.get("/opslagstavle/").status_code == 200
    assert c.get("/alumneliste/").status_code == 403
    response = c.get("/")
    assert response.status_code == 302 and response["Location"] == "/den-hurtige/"


def test_over_a_year_out_keeps_the_stamtree(house: dict[str, Account]) -> None:
    c = _client(house["over_a_year"])
    assert c.get("/stamtree/").status_code == 200
    assert c.get("/den-hurtige/").status_code == 403
    assert c.get("/", follow=True).redirect_chain[-1][0] == "/stamtree/"


def test_never_on_a_list_has_no_access(house: dict[str, Account]) -> None:
    c = _client(house["never"])
    response = c.get("/")
    assert response.status_code == 403 and "Ingen adgang" in response.content.decode()
    assert c.get("/admin/password-change").status_code == 200


def test_nobody_is_told_about_a_page_they_cannot_open(house: dict[str, Account]) -> None:
    ids = [house["resident"].pk, house["3_to_12"].pk, house["over_a_year"].pk]
    notifications.notify(ids, category="general", title="Ny i AK", url="/ak/", background=False)
    told = set(Notification.objects.values_list("recipient_id", flat=True))
    assert told == {house["resident"].pk}


def test_the_calendar_feed_empties_with_begivenheder(house: dict[str, Account]) -> None:
    assert not house["over_a_year"].has_perm("access.events_view")
    token = CalendarFeedToken.objects.create(resident=house["over_a_year"])
    response = Client().get(f"/kalender/{token.token}.ics")
    assert response.status_code == 200 and b"VEVENT" not in response.content


def test_an_administrator_previews_a_stage(
    house: dict[str, Account], make_resident: Callable[..., Account]
) -> None:
    admin = _client(make_resident(email="admin@gahk.dk", is_superuser=True))
    admin.post("/admin/preview/set", {"mode": "stage", "stage": Stage.MOVED_OUT_OVER_A_YEAR})
    assert admin.get("/alumneliste/").status_code == 403
    assert admin.get("/stamtree/").status_code == 200
    admin.post("/admin/preview/set", {"mode": "stage", "stage": ""})
    assert admin.get("/").status_code == 403


# ---- the role page ----------------------------------------------------------------------------------


def test_the_role_page_gives_from_today_and_ends_yesterday(
    house: dict[str, Account], make_resident: Callable[..., Account]
) -> None:
    admin = _client(make_resident(email="admin@gahk.dk", is_superuser=True))
    gone = house["1_to_3"]
    admin.post("/admin/roles", {"resident": gone.pk, "role": Role.OELKAELDER, "action": "add"})
    grant = Grant.objects.get(account=gone)
    assert grant.starts_on == current_date() and grant.ends_on is None
    assert grant.granted_by.email == "admin@gahk.dk"
    assert _fresh(gone).has_perm("access.oelkaelder_manage")
    Grant.objects.filter(pk=grant.pk).update(starts_on=current_date() - datetime.timedelta(days=3))
    admin.post("/admin/roles", {"resident": gone.pk, "role": Role.OELKAELDER, "action": "remove"})
    grant.refresh_from_db()
    assert grant.ends_on == current_date() - datetime.timedelta(days=1)
    assert not _fresh(gone).has_perm("access.oelkaelder_manage")


def test_the_role_page_shows_a_list_role_it_cannot_remove(
    make_resident: Callable[..., Account],
) -> None:
    _on_list(make_resident(email="ak@gahk.dk", first_name="Aksel"), 0, workgroup="AK-gruppen")
    admin = _client(make_resident(email="admin@gahk.dk", is_superuser=True))
    page = admin.get("/admin/roles").content.decode()
    assert 'title="Fra alumnelisten">ak<' in page


# ---- the kitchen staff and the Adgang page -------------------------------------------------------------


def _with_role(make_resident: Callable[..., Account], email: str, role: str) -> Client:
    return _client(_on_list(make_resident(email=email, roles=(role,)), 0))


def test_the_kitchen_staff_are_people_with_no_resident(house: dict[str, Account]) -> None:
    kitchen = house["kitchen"]
    assert kitchen.person is not None and kitchen.house is None
    assert real_roles(kitchen) == {KITCHEN}


def test_koekkengruppen_adds_one_of_the_kitchen_staff(
    house: dict[str, Account], make_resident: Callable[..., Account]
) -> None:
    c = _with_role(make_resident, "kg@gahk.dk", Role.KOKKENGRUPPE)
    assert "Køkkenpersonale" in c.get("/access/").content.decode()
    c.post("/access/kitchen-staff/add", {"first_name": "Mette", "last_name": "Kok", "email": "mette@kok.dk"})
    mette = Account.objects.get(email="mette@kok.dk")
    assert real_roles(mette) == {KITCHEN}
    assert mette.person.first_name == "Mette" and mette.house is None
    assert not mette.has_usable_password()
    assert mail.outbox[-1].to == ["mette@kok.dk"]
    assert _client(mette).get("/reparationer/").status_code == 200


def test_koekkengruppen_ends_the_kitchens_access_but_no_other(
    house: dict[str, Account], make_resident: Callable[..., Account]
) -> None:
    c = _with_role(make_resident, "kg@gahk.dk", Role.KOKKENGRUPPE)
    kitchen = Grant.objects.get(account=house["kitchen"])
    other = Grant.objects.create(
        account=house["over_a_year"],
        role=Role.REGNSKAB,
        reason="Gør regnskabet færdigt",
        starts_on=current_date() - datetime.timedelta(days=5),
    )
    assert c.post(f"/access/grants/{other.pk}/end").status_code == 403
    c.post(f"/access/grants/{kitchen.pk}/end")
    assert real_roles(_fresh(house["kitchen"])) == frozenset()
    assert "Givet i hånden" not in c.get("/access/").content.decode()


def test_the_indstilling_sees_and_ends_grants_but_not_administrator(
    house: dict[str, Account], make_resident: Callable[..., Account]
) -> None:
    c = _with_role(make_resident, "ind@gahk.dk", Role.INDSTILLING)
    gone = house["over_a_year"]
    other = Grant.objects.create(
        account=gone, role=Role.REGNSKAB, reason="Gør regnskabet færdigt", starts_on=current_date()
    )
    admin = Grant.objects.create(account=gone, role=ADMINISTRATOR, reason="Netværk", starts_on=current_date())
    page = c.get("/access/").content.decode()
    assert "Gør regnskabet færdigt" in page and "Netværk" not in page
    assert c.post(f"/access/grants/{admin.pk}/end").status_code == 403
    c.post(f"/access/grants/{other.pk}/end")
    assert not Grant.objects.filter(pk=other.pk).exists()  # began today: removed, not ended
    assert Role.REGNSKAB not in real_roles(_fresh(gone))


def test_a_plain_resident_has_no_adgang_page(house: dict[str, Account]) -> None:
    c = _client(house["resident"])
    assert c.get("/access/").status_code == 403
    assert c.post("/access/kitchen-staff/add", {}).status_code == 403
