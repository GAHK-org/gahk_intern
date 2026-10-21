"""Regnskab (accounting) role: gated settlement view of moved-out residents' AK + ØK status (F-010)."""

from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ak.models import AkEntry
from core.models import Room, Workgroup
from oelkaelder.models import Deposit, Shopper
from residents.models import Residency, RoleAssignment, prev_period
from residents.views import _sync_month_roles


def _setup_leaver_and_stayer(make_resident: Callable) -> None:
    """A leaver (present last month, gone this month) with AK debt + ØK balance, plus a current stayer."""
    now = timezone.localtime()
    cy, cm = now.year, now.month
    py, pm = prev_period((cy, cm))

    stayer = make_resident(email="stay@gahk.dk", first_name="Sta", last_name="Yer")
    Residency.objects.create(
        resident=stayer,
        year=cy,
        month=cm,
        room=Room.objects.create(legacy_index=2, number=4, floor="stuen", side="mod gården"),
    )

    leaver = make_resident(email="leaver@gahk.dk", first_name="Lea", last_name="Ver")
    Residency.objects.create(
        resident=leaver,
        year=py,
        month=pm,
        room=Room.objects.create(legacy_index=1, number=3, floor="stuen", side="mod gaden"),
    )
    AkEntry.objects.create(resident=leaver, delta=-4)  # owes 4 krydser
    for amount in (5000, 2000):  # two accounts → 70,00 kr total, exercises the cross-account sum
        Deposit.objects.create(shopper=Shopper.objects.create(resident=leaver), amount_ore=amount)


@pytest.mark.django_db
def test_leavers_show_ak_and_oek_status(make_resident: Callable) -> None:
    _setup_leaver_and_stayer(make_resident)
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    html = c.get(reverse("regnskab")).content.decode()

    assert "Lea Ver" in html  # the leaver is listed
    assert "Sta Yer" not in html  # a current resident is NOT a leaver
    assert "-4 krydser" in html  # AK shown as raw krydser
    assert "70,00 kr" in html  # ØK balance summed across both accounts
    assert "003" in html  # prior-month room number


@pytest.mark.django_db
def test_lookup_finds_any_resident(make_resident: Callable) -> None:
    _setup_leaver_and_stayer(make_resident)
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    # The stayer is not a leaver, so finding them proves the free lookup works.
    html = c.get(reverse("regnskab"), {"q": "Sta"}).content.decode()
    assert "Sta Yer" in html


@pytest.mark.django_db
def test_view_is_regnskab_gated(make_resident: Callable) -> None:
    plain = make_resident(email="plain@gahk.dk")
    admin = make_resident(email="admin@gahk.dk", roles=("administrator",))

    plain_c = Client()
    plain_c.force_login(plain)
    assert plain_c.get(reverse("regnskab")).status_code == 403

    admin_c = Client()
    admin_c.force_login(admin)  # administrator implies every role
    assert admin_c.get(reverse("regnskab")).status_code == 200


@pytest.mark.django_db
def test_set_economic_customer_number_updates_resident(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    target = make_resident(email="target@gahk.dk")
    c = Client()
    c.force_login(acc)

    response = c.post(
        reverse("set_economic_customer_number", args=[target.pk]),
        {"economic_customer_number": "42", "next": reverse("regnskab")},
    )

    target.refresh_from_db()
    assert target.economic_customer_number == 42
    assert response.status_code == 302
    assert response.url == reverse("regnskab")


@pytest.mark.django_db
def test_set_economic_customer_number_can_be_cleared(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    target = make_resident(email="target@gahk.dk", economic_customer_number=42)
    c = Client()
    c.force_login(acc)

    c.post(reverse("set_economic_customer_number", args=[target.pk]), {"economic_customer_number": ""})

    target.refresh_from_db()
    assert target.economic_customer_number is None


@pytest.mark.django_db
def test_set_economic_customer_number_requires_regnskab_role(make_resident: Callable) -> None:
    plain = make_resident(email="plain@gahk.dk")
    target = make_resident(email="target@gahk.dk")
    c = Client()
    c.force_login(plain)

    response = c.post(
        reverse("set_economic_customer_number", args=[target.pk]), {"economic_customer_number": "42"}
    )

    assert response.status_code == 403
    target.refresh_from_db()
    assert target.economic_customer_number is None


@pytest.mark.django_db
def test_set_economic_customer_number_rejects_get(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    target = make_resident(email="target@gahk.dk")
    c = Client()
    c.force_login(acc)

    assert c.get(reverse("set_economic_customer_number", args=[target.pk])).status_code == 405


@pytest.mark.django_db
def test_regnskab_workgroup_grants_role(make_resident: Callable) -> None:
    """Being placed in the 'Regnskab' embedsgruppe auto-grants the regnskab role for that month."""
    r = make_resident(email="d@gahk.dk")
    wg, _ = Workgroup.objects.get_or_create(name="Regnskabsgruppen")  # created by migration core.0003

    _sync_month_roles(r.id, wg, 2026, 8, is_admin=False)

    assert RoleAssignment.objects.filter(resident=r, role="regnskab", year=2026, month=8).exists()
