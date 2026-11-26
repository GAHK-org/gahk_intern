"""The site-wide search (search app): the top bar's suggestions and the results page.

Most of what is tested here is who sees what: every source must show a reader only what the feature
behind it would, since a name or a title in a result is already the leak, click or no click.
"""

import datetime
from collections.abc import Callable

import pytest
from django.test import Client
from django.utils import timezone

from cms.models import Page
from core.models import Room, Workgroup
from events.models import Event, EventInvite, Visibility
from gahkflix import access as gahkflix_access
from gahkflix.models import Video
from opslagstavle import access as opslagstavle_access
from opslagstavle.models import Notice
from photo_album.models import Album
from reparationer.models import RepairTask
from residents.models import Residency, Resident, Role, active_period
from search.text import excerpt, fold, mark

pytestmark = pytest.mark.django_db

MakeResident = Callable[..., Resident]


@pytest.fixture
def beboer(make_resident: MakeResident) -> Resident:
    return make_resident(email="beboer@gahk.dk", first_name="Bente", last_name="Beboer")


def client_for(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def results(resident: Resident, q: str) -> str:
    response = client_for(resident).get("/search/", {"q": q})
    assert response.status_code == 200
    return response.content.decode()


def suggestions(resident: Resident, q: str) -> str:
    response = client_for(resident).get("/search/suggestions", {"q": q})
    assert response.status_code == 200
    return response.content.decode()


def section_keys(resident: Resident, q: str) -> list[str]:
    response = client_for(resident).get("/search/", {"q": q})
    return [section.key for section in response.context["sections"]]


def hit_titles(resident: Resident, q: str, key: str) -> list[str]:
    response = client_for(resident).get("/search/", {"q": q})
    return [
        hit.title for section in response.context["sections"] if section.key == key for hit in section.hits
    ]


def live_here(
    resident: Resident, number: int, period: tuple[int, int] | None = None, **fields: object
) -> Residency:
    year, month = period or active_period()
    room, _ = Room.objects.get_or_create(
        number=number, defaults={"legacy_index": number, "floor": "stuen", "side": "mod gaden"}
    )
    return Residency.objects.create(resident=resident, room=room, year=year, month=month, **fields)


# --- the top bar and the gate ------------------------------------------------------------------------


def test_the_top_bar_searches_the_whole_intranet(beboer: Resident) -> None:
    page = client_for(beboer).get("/").content.decode()
    assert 'action="/search/"' in page
    assert 'hx-get="/search/suggestions"' in page


def test_the_kitchen_account_finds_only_what_it_may_open(
    beboer: Resident, make_resident: MakeResident
) -> None:
    """The kitchen has the search too, but its middleware gate (core.middleware) keeps it to a few
    pages, so the search leaves out every result behind that gate: a title is already the leak."""
    kitchen = make_resident(email="koekken@gahk.dk", is_kitchen=True)
    assert "data-site-search" in client_for(kitchen).get("/opslagstavle/").content.decode()

    live_here(make_resident(email="vask@gahk.dk", study="Vaskemaskineteknik"), 42)
    Notice.objects.create(author=beboer, body="Vaskemaskinen i kælderen er fixet")
    RepairTask.objects.create(reported_by=beboer, title="Vaskemaskine larmer", location="Kælderen")
    Event.objects.create(
        organiser=beboer, title="Vaskemaskinekursus", starts_at=timezone.now() + datetime.timedelta(days=2)
    )
    Album.objects.create(folder="2026", name="Vaskemaskinefesten")

    assert section_keys(beboer, "vaskemaskine") == ["residents", "notices", "events", "albums", "repairs"]
    assert section_keys(kitchen, "vaskemaskine") == ["notices", "repairs"]
    assert "Vaskemaskinekursus" not in suggestions(kitchen, "vaskemaskine")

    # Its own menu's pages and the account pages it may open, but no profile: it has none.
    assert hit_titles(kitchen, "reparation", "pages") == ["Reparationer"]
    assert hit_titles(kitchen, "notifikationer", "pages") == ["Notifikationsindstillinger"]
    assert hit_titles(kitchen, "profil", "pages") == []
    assert hit_titles(beboer, "profil", "pages") != []


def test_logged_out_is_sent_to_log_in() -> None:
    response = Client().get("/search/", {"q": "øl"})
    assert response.status_code == 302
    assert "next=" in response["Location"]


def test_a_query_too_short_searches_nothing(beboer: Resident) -> None:
    assert section_keys(beboer, "a") == []
    assert "Skriv mindst 2 tegn" in results(beboer, "a")
    assert suggestions(beboer, "a").strip() == ""


# --- Sider -------------------------------------------------------------------------------------------


def test_pages_are_found_by_name_by_other_words_and_without_danish_letters(beboer: Resident) -> None:
    assert hit_titles(beboer, "alumne", "pages") == ["Alumneliste"]
    assert "Begivenheder" in hit_titles(beboer, "kalender", "pages")
    assert hit_titles(beboer, "oelkaelder", "pages")[0] == "Ølkælder"
    assert "Skift kodeord" in hit_titles(beboer, "password", "pages")


def test_a_page_the_reader_cannot_open_is_not_found(beboer: Resident, make_resident: MakeResident) -> None:
    admin = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    assert "Site-admin" not in hit_titles(beboer, "site-admin", "pages")
    assert "Site-admin" in hit_titles(admin, "site-admin", "pages")


def test_pages_follow_the_preview_too(make_resident: MakeResident) -> None:
    admin = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    client = client_for(admin)
    session = client.session
    session["preview_roles"] = []
    session.save()
    response = client.get("/search/", {"q": "site-admin"})
    assert [h.title for s in response.context["sections"] if s.key == "pages" for h in s.hits] == []


# --- Alumnelisten ------------------------------------------------------------------------------------


def test_residents_are_found_by_several_words_and_by_room(
    beboer: Resident, make_resident: MakeResident
) -> None:
    anders = make_resident(email="anders@gahk.dk", first_name="Anders", last_name="Hansen", study="Medicin")
    make_resident(email="anne@gahk.dk", first_name="Anne", last_name="Hansen")
    live_here(anders, 42)
    assert hit_titles(beboer, "anders hansen", "residents") == ["Anders Hansen"]
    assert hit_titles(beboer, "042", "residents") == ["Anders Hansen"]
    assert hit_titles(beboer, "medicin", "residents") == ["Anders Hansen"]
    assert "Værelse 042 · Medicin" in results(beboer, "anders")


def test_residents_by_embedsgruppe(beboer: Resident, make_resident: MakeResident) -> None:
    anders = make_resident(email="anders@gahk.dk", first_name="Anders", last_name="Hansen")
    live_here(anders, 42, workgroup=Workgroup.objects.get_or_create(name="Inspektionen")[0])
    assert hit_titles(beboer, "inspektion", "residents") == ["Anders Hansen"]


def test_only_this_months_alumneliste(beboer: Resident, make_resident: MakeResident) -> None:
    gone = make_resident(email="gone@gahk.dk", first_name="Gorm", last_name="Gammel")
    year, month = active_period()
    live_here(beboer, 8)  # this month's list, which makes last year's an old one
    live_here(gone, 7, period=(year - 1, month))
    assert hit_titles(beboer, "gorm", "residents") == []


def test_the_alumneliste_itself_now_takes_several_words(
    beboer: Resident, make_resident: MakeResident
) -> None:
    anders = make_resident(email="anders@gahk.dk", first_name="Anders", last_name="Hansen")
    live_here(anders, 42)
    page = client_for(beboer).get("/alumneliste/", {"q": "anders hansen"})
    assert [row.resident for row in page.context["rows"]] == [anders]


# --- Opslagstavlen -----------------------------------------------------------------------------------


def test_notices_are_found_with_the_words_around_the_match(beboer: Resident) -> None:
    filler = " ".join(["ord"] * 60)
    Notice.objects.create(author=beboer, body=f"{filler} Husk julefrokosten på fredag. {filler}")
    page = results(beboer, "julefrokost")
    assert '<mark class="search-hit">julefrokost</mark>en på fredag' in page
    assert hit_titles(beboer, "julefrokost", "notices") == ["Bente Beboer"]


def test_a_notice_is_escaped(beboer: Resident) -> None:
    Notice.objects.create(author=beboer, body="Pas på `<script>alert(1)</script>` i køkkenet")
    page = results(beboer, "køkkenet")
    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;" in page


def test_notices_behind_their_rollout_gate(beboer: Resident, monkeypatch: pytest.MonkeyPatch) -> None:
    Notice.objects.create(author=beboer, body="Husk julefrokosten")
    monkeypatch.setattr(opslagstavle_access, "ACCESS_ROLES", (Role.ADMINISTRATOR,))
    assert "notices" not in section_keys(beboer, "julefrokost")


# --- Begivenheder ------------------------------------------------------------------------------------


def test_a_private_event_is_found_only_by_those_invited(
    beboer: Resident, make_resident: MakeResident
) -> None:
    host = make_resident(email="host@gahk.dk")
    guest = make_resident(email="guest@gahk.dk")
    soon = timezone.now() + datetime.timedelta(days=3)
    Event.objects.create(organiser=host, title="Fællesspisning", starts_at=soon)
    secret = Event.objects.create(
        organiser=host, title="Surprise-fest", starts_at=soon, visibility=Visibility.KUN_INVITEREDE
    )
    EventInvite.objects.create(event=secret, resident=guest)
    assert hit_titles(beboer, "fællesspisning", "events") == ["Fællesspisning"]
    assert hit_titles(beboer, "surprise", "events") == []
    assert hit_titles(guest, "surprise", "events") == ["Surprise-fest"]
    assert hit_titles(host, "surprise", "events") == ["Surprise-fest"]


def test_a_finished_event_is_not_found(beboer: Resident) -> None:
    Event.objects.create(
        organiser=beboer, title="Fællesspisning", starts_at=timezone.now() - datetime.timedelta(days=2)
    )
    assert hit_titles(beboer, "fællesspisning", "events") == []


# --- Fotoalbum, GAHKFlix -----------------------------------------------------------------------------


def test_albums_by_name_and_year(beboer: Resident) -> None:
    Album.objects.create(folder="2025", name="Julefrokost")
    Album.objects.create(folder="2026", name="Sommerfest")
    assert hit_titles(beboer, "julefrokost", "albums") == ["Julefrokost"]
    assert hit_titles(beboer, "2026", "albums") == ["Sommerfest"]


def test_films_behind_gahkflix_gate_and_never_a_draft(
    beboer: Resident, make_resident: MakeResident, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gahkflix_access, "ACCESS_ROLES", (Role.ADMINISTRATOR, Role.FOTO))
    Video.objects.create(title="Revy 2026", uploaded_by=beboer, published_at=timezone.now())
    Video.objects.create(title="Revy kladde", uploaded_by=beboer)
    foto = make_resident(email="foto@gahk.dk", roles=(Role.FOTO,))
    assert "films" not in section_keys(beboer, "revy")
    assert hit_titles(foto, "revy", "films") == ["Revy 2026"]


# --- Reparationer ------------------------------------------------------------------------------------


def test_repairs_by_several_words_archived_ones_too(beboer: Resident) -> None:
    RepairTask.objects.create(reported_by=beboer, title="Vaskemaskine larmer", location="Kælderen")
    RepairTask.objects.create(
        reported_by=beboer, title="Vaskemaskine i stykker", location="Kælderen", archived_at=timezone.now()
    )
    RepairTask.objects.create(reported_by=beboer, title="Vaskemaskine utæt", location="Loftet")
    assert hit_titles(beboer, "vaskemaskine kælder", "repairs") == [
        "Vaskemaskine larmer",
        "Vaskemaskine i stykker",
    ]


# --- Hjemmeside --------------------------------------------------------------------------------------


def test_website_pages_only_for_its_editors(beboer: Resident, make_resident: MakeResident) -> None:
    Page.objects.create(slug="faciliteter", header="Faciliteter")
    editor = make_resident(email="pr@gahk.dk", roles=(Role.PR,))
    assert "website" not in section_keys(beboer, "faciliteter")
    assert hit_titles(editor, "faciliteter", "website") == ["Faciliteter"]


# --- the dropdown ------------------------------------------------------------------------------------


def test_suggestions_are_options_and_end_with_every_result(beboer: Resident) -> None:
    html = suggestions(beboer, "alumne")
    assert 'role="listbox"' in html
    assert 'href="/alumneliste/" role="option"' in html
    assert 'href="/search/?q=alumne"' in html


def test_suggestions_say_when_nothing_is_found(beboer: Resident) -> None:
    assert "Intet fundet" in suggestions(beboer, "zzqxj")


# --- text --------------------------------------------------------------------------------------------


def test_fold() -> None:
    assert fold("Ølkælder") == fold("oelkaelder") == "oelkaelder"
    assert fold("Café Århus") == "cafe aarhus"


def test_mark_escapes_everything_and_marks_case_insensitively() -> None:
    assert mark("<b>Øl</b> og øl", ["øl"]) == (
        '&lt;b&gt;<mark class="search-hit">Øl</mark>&lt;/b&gt; og <mark class="search-hit">øl</mark>'
    )
    assert mark("a & b", []) == "a &amp; b"


def test_excerpt_centres_the_first_match() -> None:
    text = " ".join(f"w{i}" for i in range(100)) + " julefrokost " + " ".join(f"v{i}" for i in range(100))
    short = excerpt(text, ["julefrokost"], words=12)
    assert short.startswith("… ") and short.endswith(" …")
    assert "julefrokost" in short
    assert excerpt("kort tekst", ["x"]) == "kort tekst"
