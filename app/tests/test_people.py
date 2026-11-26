"""People, residents and accounts (residents.models): a person may have no login and never have lived
here; a login may have no person."""

from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse

from core.models import Room
from residents.models import Account, Person, Residency, Resident, active_period, next_period

pytestmark = pytest.mark.django_db


def _indstilling(make_resident: Callable[..., Account]) -> Client:
    client = Client()
    client.force_login(make_resident(email="ind@gahk.dk", roles=("indstilling",)))
    return client


def _alumne(first: str, last: str, **facts: object) -> Resident:
    """Someone who lived here and has no login."""
    return Resident.objects.create(person=Person.objects.create(first_name=first, last_name=last), **facts)


def test_a_resident_without_a_login_is_on_the_alumneliste(make_resident: Callable[..., Account]) -> None:
    year, month = active_period()
    alumne = _alumne("Gammel", "Alumne", study="Historie, KU")
    room = Room.objects.create(legacy_index=1, number=11, floor="stuen", side="mod gaden")
    Residency.objects.create(resident=alumne, room=room, year=year, month=month)
    html = _indstilling(make_resident).get(reverse("directory")).content.decode()
    assert "Gammel Alumne" in html and "Historie, KU" in html
    assert f"/beboer/{alumne.pk}/rediger" in html  # the edit link is the resident's


def test_the_stamtree_holds_residents_who_never_had_a_login(make_resident: Callable[..., Account]) -> None:
    elder = _alumne("Ældre", "Fra 1975")
    make_resident(email="yngre@gahk.dk", first_name="Yngre", sponsor=elder)
    client = Client()
    client.force_login(make_resident(email="laeser@gahk.dk"))
    html = client.get(reverse("stamtree")).content.decode()
    assert html.index("Ældre Fra 1975") < html.index("Yngre Beboer")


def test_people_who_never_lived_here_are_not_in_the_stamtree(make_resident: Callable[..., Account]) -> None:
    make_resident(email="kok@gahk.dk", first_name="Kirsten", last_name="Kok", is_kitchen=True)
    client = Client()
    client.force_login(make_resident(email="laeser@gahk.dk"))
    assert "Kirsten Kok" not in client.get(reverse("stamtree")).content.decode()


def test_adding_someone_without_an_e_mail_makes_no_login(make_resident: Callable[..., Account]) -> None:
    client = _indstilling(make_resident)
    room = Room.objects.create(legacy_index=2, number=12, floor="stuen", side="mod gaden")
    client.post(
        reverse("next_month_list"),
        {"action": "add_new", "first_name": "Uden", "last_name": "Mail", "email": "", "room": room.pk},
    )
    person = Person.objects.get(first_name="Uden")
    assert person.login is None and person.house is not None
    year, month = next_period(active_period())
    assert Residency.objects.filter(resident=person.house, room=room, year=year, month=month).exists()


def test_an_account_keeps_a_copy_of_its_persons_name(make_resident: Callable[..., Account]) -> None:
    anna = make_resident(email="anna@gahk.dk", first_name="Anna", last_name="Berg")
    person = anna.person
    person.last_name = "Holm"
    person.save()
    anna.refresh_from_db()
    assert anna.full_name == "Anna Holm"


def test_an_account_reads_its_persons_and_residents_facts(make_resident: Callable[..., Account]) -> None:
    anna = make_resident(email="anna@gahk.dk", phone="12345678", study="Fysik, KU")
    assert (anna.phone, anna.study) == ("12345678", "Fysik, KU")
    script = make_resident(email="import@gahk.dk", person=None)
    assert (script.phone, script.study, script.house) == ("", "", None)
    assert not script.residencies.exists()


def test_the_edit_page_edits_a_resident_without_a_login(make_resident: Callable[..., Account]) -> None:
    alumne = _alumne("Gammel", "Alumne")
    client = _indstilling(make_resident)
    assert "har intet login" in client.get(reverse("edit_resident", args=[alumne.pk])).content.decode()
    client.post(
        reverse("edit_resident", args=[alumne.pk]),
        {"person-first_name": "Gamle", "person-last_name": "Alumne", "resident-study": "Jura"},
    )
    alumne.refresh_from_db()
    alumne.person.refresh_from_db()
    assert (alumne.person.first_name, alumne.study) == ("Gamle", "Jura")
