"""The alumneliste keeps the editor's work when a submission is rejected.

Indstilling edits a whole month in one screen. Throwing the form away over one bad room number means
retyping a newcomer's name, email and fylgje, or re-doing a dozen room changes — which is how the
mistake gets made again. Every rejected path here re-renders holding what was typed.
"""

from collections.abc import Callable

import pytest
from django.test import Client

URL = "/intern/alumneliste/naeste-maaned"


@pytest.fixture
def rooms() -> list:
    from core.models import Room

    return [
        Room.objects.create(legacy_index=n, number=n, floor="stuen", side="mod gaden") for n in (91, 92, 93)
    ]


@pytest.fixture
def indstilling(make_resident: Callable) -> Client:
    c = Client()
    c.force_login(make_resident(email="ind-draft@gahk.dk", roles=("indstilling",)))
    return c


@pytest.mark.django_db
def test_save_keeps_edits_and_removals_when_rooms_clash(
    make_resident: Callable, indstilling: Client, rooms: list
) -> None:
    """Guards the existing draft-restore branch: it is load-bearing and was untested."""
    from residents.models import Residency, active_period

    y, m = active_period()
    a = make_resident(email="draft-a@gahk.dk", first_name="Anna")
    b = make_resident(email="draft-b@gahk.dk", first_name="Bodil")
    c = make_resident(email="draft-c@gahk.dk", first_name="Carla")
    for res, room in zip((a, b, c), rooms, strict=True):
        Residency.objects.create(resident=res, room=room, year=y, month=m)

    resp = indstilling.post(
        URL,
        {
            "action": "save",
            "period": "current",
            f"room_{a.id}": rooms[0].id,
            f"room_{b.id}": rooms[0].id,  # the one mistake: same room as Anna
            f"room_{c.id}": rooms[2].id,
            f"remove_{c.id}": "1",
        },
    )
    assert resp.status_code == 200  # rendered in place, not redirected away
    drafted = {r.resident.first_name: r.room_id for r in resp.context["next_rows"]}
    assert drafted == {"Anna": rooms[0].id, "Bodil": rooms[0].id}  # edit kept, Carla still removed
    assert dict(Residency.objects.filter(year=y, month=m).values_list("resident__first_name", "room_id")) == {
        "Anna": rooms[0].id,
        "Bodil": rooms[1].id,
        "Carla": rooms[2].id,
    }  # nothing written


@pytest.mark.django_db
def test_add_new_keeps_what_was_typed_when_the_room_is_taken(
    make_resident: Callable, indstilling: Client, rooms: list
) -> None:
    from residents.models import Residency, Resident, active_period

    y, m = active_period()
    Residency.objects.create(resident=make_resident(email="occupant@gahk.dk"), room=rooms[0], year=y, month=m)
    sponsor = make_resident(email="fylgje@gahk.dk", first_name="Fylgje")

    resp = indstilling.post(
        URL,
        {
            "action": "add_new",
            "period": "current",
            "first_name": "Nikoline",
            "last_name": "Nyborg",
            "email": "nikoline@gahk.dk",
            "room": rooms[0].id,  # the one mistake
            "sponsor": sponsor.id,
        },
    )
    assert resp.status_code == 200
    draft = resp.context["new_draft"]
    assert draft["first_name"] == "Nikoline"
    assert draft["last_name"] == "Nyborg"
    assert draft["email"] == "nikoline@gahk.dk"
    assert draft["sponsor"] == sponsor.id
    html = resp.content.decode()
    assert 'value="Nikoline"' in html and 'value="nikoline@gahk.dk"' in html
    assert not Resident.objects.filter(email="nikoline@gahk.dk").exists()  # nothing created


@pytest.mark.django_db
def test_add_new_keeps_the_draft_on_a_duplicate_email(
    make_resident: Callable, indstilling: Client, rooms: list
) -> None:
    make_resident(email="taken@gahk.dk")
    resp = indstilling.post(
        URL,
        {
            "action": "add_new",
            "period": "current",
            "first_name": "Nikoline",
            "last_name": "Nyborg",
            "email": "taken@gahk.dk",
            "room": rooms[0].id,
        },
    )
    assert resp.status_code == 200
    assert resp.context["new_draft"]["first_name"] == "Nikoline"


@pytest.mark.django_db
def test_add_existing_keeps_its_selections_when_the_room_is_taken(
    make_resident: Callable, indstilling: Client, rooms: list
) -> None:
    from core.models import Workgroup
    from residents.models import Residency, active_period

    y, m = active_period()
    Residency.objects.create(
        resident=make_resident(email="occupant2@gahk.dk"), room=rooms[0], year=y, month=m
    )
    newcomer = make_resident(email="tilfoej@gahk.dk")
    wg = Workgroup.objects.create(name="Haven")

    resp = indstilling.post(
        URL,
        {
            "action": "add_existing",
            "period": "current",
            "resident": newcomer.id,
            "room": rooms[0].id,  # the one mistake
            "workgroup": wg.id,
        },
    )
    assert resp.status_code == 200
    draft = resp.context["existing_draft"]
    assert (draft["resident"], draft["room"], draft["workgroup"]) == (newcomer.id, rooms[0].id, wg.id)
    assert not Residency.objects.filter(resident=newcomer, year=y, month=m).exists()

    # Marked selected in the rendered form — and only in that form. Both add-forms post a field
    # called `room`, so the draft must not bleed into the "opret ny beboer" one beside it.
    html = resp.content.decode()
    existing_form, new_form = html.split('value="add_new"', 1)
    existing_form = existing_form.split('value="add_existing"', 1)[1]
    assert f'value="{rooms[0].id}" selected' in existing_form
    assert f'value="{rooms[0].id}" selected' not in new_form
    assert resp.context["new_draft"] is None


@pytest.mark.django_db
def test_a_successful_add_still_redirects(make_resident: Callable, indstilling: Client, rooms: list) -> None:
    """Post/redirect/get is what stops a refresh adding the same person twice — keep it on success."""
    from residents.models import Residency, active_period

    y, m = active_period()
    Residency.objects.create(resident=make_resident(email="anchor@gahk.dk"), room=rooms[0], year=y, month=m)
    newcomer = make_resident(email="ok@gahk.dk")
    resp = indstilling.post(
        URL,
        {"action": "add_existing", "period": "current", "resident": newcomer.id, "room": rooms[1].id},
    )
    assert resp.status_code == 302
    assert Residency.objects.filter(resident=newcomer, year=y, month=m).exists()
