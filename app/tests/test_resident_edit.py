"""Indstilling can edit a resident's core data (name, e-mail, phone, dates, studie, fylgje)."""

import datetime
from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse


@pytest.mark.django_db
def test_indstilling_can_edit_resident_core_data(make_resident: Callable) -> None:
    r = make_resident(email="x@gahk.dk", first_name="Old", last_name="Name")
    fylgje = make_resident(email="f@gahk.dk", first_name="Far", last_name="Fadder")
    ind = make_resident(email="ind@gahk.dk", roles=("indstilling",))
    c = Client()
    c.force_login(ind)

    resp = c.post(
        reverse("edit_resident", args=[r.id]),
        {
            "first_name": "Ny",
            "last_name": "Navn",
            "email": "ny@gahk.dk",
            "phone": "12345678",
            "study": "Fysik, KU",
            "birthday": "2000-01-01",
            "move_in_date": "",
            "move_out_date": "",
            "sponsor": str(fylgje.id),
            "fylgje_raw": "",
        },
    )

    assert resp.status_code == 302
    r.refresh_from_db()
    assert r.first_name == "Ny"
    assert r.email == "ny@gahk.dk"
    assert r.phone == "12345678"
    assert r.study == "Fysik, KU"
    assert r.sponsor_id == fylgje.id


@pytest.mark.django_db
def test_edit_resident_is_indstilling_gated(make_resident: Callable) -> None:
    r = make_resident(email="x@gahk.dk")
    plain = make_resident(email="p@gahk.dk")
    c = Client()
    c.force_login(plain)
    assert c.get(reverse("edit_resident", args=[r.id])).status_code == 403
    # a plain resident cannot change anyone's data
    c.post(
        reverse("edit_resident", args=[r.id]), {"first_name": "Hacked", "last_name": "x", "email": r.email}
    )
    r.refresh_from_db()
    assert r.first_name != "Hacked"


@pytest.mark.django_db
def test_edit_link_shown_only_to_indstilling(make_resident: Callable) -> None:
    from core.models import Room
    from residents.models import Residency, active_period

    cy, cm = active_period()
    room = Room.objects.create(legacy_index=1, number=5, floor="stuen", side="mod gaden")
    resident = make_resident(email="beboer@gahk.dk")
    Residency.objects.create(resident=resident, room=room, year=cy, month=cm)

    ind = make_resident(email="ind@gahk.dk", roles=("indstilling",))
    plain = make_resident(email="plain@gahk.dk")

    ci = Client()
    ci.force_login(ind)
    assert reverse("edit_resident", args=[resident.id]) in ci.get(reverse("directory")).content.decode()

    cp = Client()
    cp.force_login(plain)
    assert reverse("edit_resident", args=[resident.id]) not in cp.get(reverse("directory")).content.decode()


@pytest.mark.django_db
def test_edit_form_prefills_dates_in_iso(make_resident: Callable) -> None:
    """A `type=date` input only pre-fills a value shaped "YYYY-MM-DD"; under LANGUAGE_CODE="da"
    Django would render "01.01.2000", the browser would drop it, and saving would wipe the date."""
    r = make_resident(
        email="x@gahk.dk",
        birthday=datetime.date(2000, 1, 1),
        move_in_date=datetime.date(2021, 8, 1),
    )
    ind = make_resident(email="ind@gahk.dk", roles=("indstilling",))
    c = Client()
    c.force_login(ind)

    html = c.get(reverse("edit_resident", args=[r.id])).content.decode()
    assert 'value="2000-01-01"' in html
    assert 'value="2021-08-01"' in html
