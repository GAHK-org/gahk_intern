"""People and their accounts (residents.Person): someone the house knows may have no login, and a
login may have no person."""

from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse

from access.api import real_roles
from core.models import Room
from residents.models import Person, Residency, Resident, active_period, next_period

pytestmark = pytest.mark.django_db


def _indstilling(make_resident: Callable[..., Resident]) -> Client:
    client = Client()
    client.force_login(make_resident(email="ind@gahk.dk", roles=("indstilling",)))
    return client


def test_a_person_without_a_login_is_on_the_alumneliste(make_resident: Callable[..., Resident]) -> None:
    year, month = active_period()
    alumne = Person.objects.create(first_name="Gammel", last_name="Alumne", study="Historie, KU")
    room = Room.objects.create(legacy_index=1, number=11, floor="stuen", side="mod gaden")
    Residency.objects.create(resident=alumne, room=room, year=year, month=month)
    html = _indstilling(make_resident).get(reverse("directory")).content.decode()
    assert "Gammel Alumne" in html and "Historie, KU" in html
    assert f"/beboer/{alumne.pk}/rediger" in html  # the edit link is the person's


def test_a_fylgje_with_no_login_holds_the_stamtree_together(make_resident: Callable[..., Resident]) -> None:
    elder = Person.objects.create(first_name="Ældre", last_name="Uden Login")
    make_resident(email="yngre@gahk.dk", first_name="Yngre", sponsor=elder)
    client = Client()
    client.force_login(make_resident(email="laeser@gahk.dk"))
    html = client.get(reverse("stamtree")).content.decode()
    assert html.index("Ældre Uden Login") < html.index("Yngre Beboer")


def test_adding_someone_without_an_e_mail_makes_a_person_and_no_login(
    make_resident: Callable[..., Resident],
) -> None:
    client = _indstilling(make_resident)
    room = Room.objects.create(legacy_index=2, number=12, floor="stuen", side="mod gaden")
    client.post(
        reverse("next_month_list"),
        {"action": "add_new", "first_name": "Uden", "last_name": "Mail", "email": "", "room": room.pk},
    )
    person = Person.objects.get(first_name="Uden")
    assert person.login is None
    assert Residency.objects.filter(resident=person, room=room).exists()
    assert next_period(active_period()) in {(r.year, r.month) for r in person.residencies.all()}


def test_an_account_keeps_a_copy_of_its_persons_name(make_resident: Callable[..., Resident]) -> None:
    anna = make_resident(email="anna@gahk.dk", first_name="Anna", last_name="Berg")
    person = anna.person
    person.last_name = "Holm"
    person.save()
    anna.refresh_from_db()
    assert anna.full_name == "Anna Holm"


def test_a_login_with_no_person_is_never_a_resident(make_resident: Callable[..., Resident]) -> None:
    script = make_resident(email="import@gahk.dk", person=None)
    assert script.person is None and script.birthday is None and not script.residencies.exists()


@pytest.mark.real_stages
def test_a_persons_months_decide_their_accounts_stage(make_resident: Callable[..., Resident]) -> None:
    year, month = active_period()
    anna = make_resident(email="anna@gahk.dk")
    room = Room.objects.create(legacy_index=3, number=13, floor="stuen", side="mod gaden")
    Residency.objects.create(resident=anna.person, room=room, year=year, month=month)
    assert "resident" in real_roles(Resident.objects.get(pk=anna.pk))
