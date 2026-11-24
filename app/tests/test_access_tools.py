"""The access page (access.explain, /access/person/<pk>, /access/mine) and "Se side som" for one
person. Marked real_stages, so the stages are what the months say."""

import datetime
from collections.abc import Callable
from html import unescape
from itertools import count

import pytest
from django.test import Client

from access.explain import explain
from access.models import Grant
from core.clock import current_date
from core.danish import genitive
from core.models import Room, Workgroup
from residents.models import Residency, Resident, Role

pytestmark = [pytest.mark.django_db, pytest.mark.real_stages]

_numbers = count(900)


def _on_list(resident: Resident, *offsets: int, workgroup: str | None = None) -> Resident:
    today = current_date()
    group = Workgroup.objects.get_or_create(name=workgroup)[0] if workgroup else None
    for offset in offsets:
        n = next(_numbers)
        room = Room.objects.create(legacy_index=n, number=n, floor="stuen", side="mod gaden")
        index = today.year * 12 + today.month - 1 + offset
        Residency.objects.create(
            resident=resident.person, room=room, workgroup=group, year=index // 12, month=index % 12 + 1
        )
    return resident


def _client(user: Resident) -> Client:
    client = Client()
    client.force_login(user)
    return client


def test_genitive() -> None:
    assert genitive("Anna") == "Annas" and genitive("Lars") == "Lars'" and genitive("") == "s"


def test_each_role_says_where_it_comes_from(make_resident: Callable[..., Resident]) -> None:
    anna = _on_list(make_resident(email="anna@gahk.dk"), 0, workgroup="AK-gruppen")
    Grant.objects.create(
        resident=anna, role=Role.REGNSKAB, reason="Hjælper kassereren", starts_on=current_date()
    )
    reasons = {reason.role: reason.why for reason in explain(anna).roles}
    assert reasons["resident"].startswith("På alumnelisten for")
    assert reasons[Role.AK].startswith("AK-gruppen på alumnelisten")
    assert "Hjælper kassereren" in reasons[Role.REGNSKAB]


def test_the_features_say_which_role_gives_them(make_resident: Callable[..., Resident]) -> None:
    bo = _on_list(make_resident(email="bo@gahk.dk"), 0, workgroup="Ølkælderen")
    features = {feature.label: feature for feature in explain(bo).features}
    assert (features["Ølkælder"].grade_label, features["Ølkælder"].given_by) == ("Styre", "Ølkælderen")
    assert (features["AK"].grade_label, features["AK"].given_by) == ("Bruge", "Beboer")
    assert features["Regnskab"].grade_label == "Ingen"


def test_moving_out_shows_the_stages_to_come(make_resident: Callable[..., Resident]) -> None:
    cy = _on_list(make_resident(email="cy@gahk.dk"), -1, 0)  # last month on the list is this one
    _on_list(make_resident(email="next@gahk.dk"), 1)  # next month's list is made, without Cy
    texts = [change.text for change in explain(cy).changes]
    assert texts[:4] == [
        "Fraflyttet for under 1 måned siden.",
        "Fraflyttet for 1–3 måneder siden.",
        "Fraflyttet for 3–12 måneder siden.",
        "Fraflyttet for over et år siden.",
    ]


def test_a_list_made_ahead_and_a_grant_ending_are_coming(make_resident: Callable[..., Resident]) -> None:
    dina = _on_list(make_resident(email="dina@gahk.dk"), 0)
    _on_list(dina, 1, workgroup="Fotogruppen")
    ends = current_date() + datetime.timedelta(days=10)
    Grant.objects.create(
        resident=dina, role=Role.PR, reason="Kampagne", starts_on=current_date(), ends_on=ends
    )
    texts = {change.text for change in explain(dina).changes}
    assert "Fotogruppen på listen." in texts
    assert not any(text.startswith("Fraflyttet") for text in texts)  # no list yet beyond next month
    assert "PR ophører." in texts


def test_the_indstilling_opens_anyones_page(make_resident: Callable[..., Resident]) -> None:
    ind = _on_list(make_resident(email="ind@gahk.dk"), 0, workgroup="Indstillingen")
    eva = _on_list(make_resident(email="eva@gahk.dk"), 0)
    page = _client(ind).get(f"/access/person/{eva.pk}").content.decode()
    assert "Adgang for Test Beboer" in page and "Hvad rollerne giver" in page
    assert _client(eva).get(f"/access/person/{ind.pk}").status_code == 403


def test_everyone_opens_their_own_even_with_no_access(make_resident: Callable[..., Resident]) -> None:
    never = make_resident(email="aldrig@gahk.dk")
    page = _client(never).get("/access/mine")
    assert page.status_code == 200 and "Ingen roller: ingen adgang" in page.content.decode()
    kitchen = make_resident(email="kok@kok.dk", is_kitchen=True)
    assert "Køkkenet" in _client(kitchen).get("/access/mine").content.decode()


def test_an_administrator_sees_the_site_with_ones_roles(make_resident: Callable[..., Resident]) -> None:
    admin = _on_list(make_resident(email="admin@gahk.dk", first_name="Ada", is_superuser=True), 0)
    gone = _on_list(make_resident(email="gone@gahk.dk", first_name="Lars", last_name="Holm"), -30)
    client = _client(admin)
    assert "Se siden med Lars' roller" in unescape(client.get(f"/access/person/{gone.pk}").content.decode())
    client.post("/admin/preview/set", {"mode": "person", "person": gone.pk})
    assert client.get("/alumneliste/").status_code == 403
    page = client.get("/stamtree/").content.decode()
    assert "Lars Holms roller" in page
    client.post("/admin/preview/set", {"mode": "clear"})
    assert client.get("/alumneliste/").status_code == 200


def test_a_resident_cannot_preview_a_person(make_resident: Callable[..., Resident]) -> None:
    fie = _on_list(make_resident(email="fie@gahk.dk"), 0)
    gone = _on_list(make_resident(email="gone@gahk.dk"), -30)
    client = _client(fie)
    client.post("/admin/preview/set", {"mode": "person", "person": gone.pk})
    assert client.get("/alumneliste/").status_code == 200
