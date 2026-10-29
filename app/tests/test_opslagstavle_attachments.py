"""A post about a photo album or a GAHKFlix film (opslagstavle.attachments): the album and the film
drawn as their own features draw themselves, the composer's pickers, and "Del på opslagstavlen" on
the album's and the film's pages."""

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import include, path
from django.utils import timezone

import config.urls
from gahkflix import access as gahkflix_access
from gahkflix.models import Series, Watch
from opslagstavle import access as board_access
from opslagstavle import services as board_services
from opslagstavle.models import Notice
from photo_album.models import Album, DerivativeState, Media, MediaStatus
from residents.models import Resident, Role

from .test_gahkflix import make_video

BOARD = "/intern/opslagstavle/"
START = datetime(2026, 12, 12, 18, tzinfo=UTC)
pytestmark = pytest.mark.django_db

# The site with the photo album and GAHKFlix moved to other addresses, for the test that posts follow
# them. Listed first, so reversing their names finds these mounts rather than the usual ones.
urlpatterns = [
    path("billeder/", include("photo_album.urls")),
    path("film/", include("gahkflix.urls")),
    *config.urls.urlpatterns,
]


@pytest.fixture
def beboer(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="beboer@gahk.dk", first_name="Bo", last_name="Beboer")


@pytest.fixture
def other(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="anden@gahk.dk", first_name="Ann", last_name="Anden")


@pytest.fixture
def member(make_resident: Callable[..., Resident]) -> Resident:
    """Fotogruppen: inside GAHKFlix's rollout gate, and one of its administrators."""
    return make_resident(email="foto@gahk.dk", roles=(Role.FOTO,), first_name="Dorte")


@pytest.fixture
def videos_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift GAHKFlix's rollout gate: these tests are about the widget. The ones about the gate say so."""
    monkeypatch.setattr(gahkflix_access, "ACCESS_ROLES", None)


def client_for(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def post(author: Resident, body: str = "Se her!", **fields: object) -> Notice:
    return Notice.objects.create(author=author, body=body, **fields)


def album(name: str = "Julefrokost 2026", **fields: object) -> Album:
    return Album.objects.create(folder="2026", name=name, **fields)


def photo(into: Album, owner: Resident, minute: int, **fields: object) -> Media:
    defaults: dict[str, object] = {
        "title": "foto",
        "original": "o",
        "high_definition": "h",
        "thumbnail": f"photo-album/{into.pk}/thumbnails/{minute}.jpg",
        "content_type": "image/jpeg",
        "status": MediaStatus.APPROVED,
        "captured_at": START + timedelta(minutes=minute),
    }
    return Media.objects.create(album=into, requested_by=owner, **{**defaults, **fields})


def album_widgets(html: str) -> list[str]:
    return re.findall(r'<a class="album-timeline-card album-embed.*?</a>', html, re.DOTALL)


def pictures(widget: str) -> list[str]:
    return re.findall(r'<img src="([^"]+)"', widget)


def hidden(html: str, name: str) -> str:
    found = re.search(rf'<input type="hidden" name="{name}" id="id_{name}" value="([^"]*)">', html)
    assert found is not None, f"no hidden {name} field"
    return found.group(1)


# --- the album's widget ---------------------------------------------------------------------------


def test_a_post_about_an_album_shows_the_photo_albums_own_widget(beboer: Resident) -> None:
    party = album()
    for minute in range(3):
        photo(party, beboer, minute)
    post(beboer, "Så er billederne oppe!", album=party)

    html = client_for(beboer).get(BOARD).content.decode()

    [widget] = album_widgets(html)
    assert widget.startswith(
        f'<a class="album-timeline-card album-embed album-embed-n3" href="/intern/fotoalbum/{party.pk}">'
    )
    assert '<span class="album-eyebrow">Fotoalbum · 2026</span>' in widget
    assert "<strong>Julefrokost 2026</strong>" in widget
    assert "3 billeder" in widget
    assert "Se albummet →" in widget
    # Newest first, as the album page shows them.
    assert pictures(widget) == [f"/photo-album/{party.pk}/thumbnails/{minute}.jpg" for minute in (2, 1, 0)]
    # The post's text carries no link: the widget is the album.
    assert 'href="/intern/fotoalbum/' not in html.split('<div class="board-body')[1].split("</div>")[0]


def test_the_album_widget_counts_pictures_and_videos_all_the_way_down(beboer: Resident) -> None:
    roskilde = album("Roskilde")
    saturday = album("Lørdag", parent=roskilde)
    for minute in range(4):
        photo(roskilde, beboer, minute)
    for minute in range(4, 9):
        photo(saturday, beboer, minute)
    photo(saturday, beboer, 9, content_type="video/mp4")
    post(beboer, album=saturday)
    post(beboer, album=roskilde)

    root, sub = album_widgets(client_for(beboer).get(BOARD).content.decode())

    assert "9 billeder · 1 video" in root
    # The newest seven of the whole tree.
    assert len(pictures(root)) == 7
    assert pictures(root)[0] == f"/photo-album/{saturday.pk}/thumbnails/9.jpg"
    assert "album-embed-n6" in sub and "5 billeder · 1 video" in sub
    assert '<span class="album-eyebrow">Fotoalbum · 2026 · Roskilde</span>' in sub


def test_the_album_widget_shows_what_every_reader_sees(beboer: Resident, other: Resident) -> None:
    party = album()
    photo(party, beboer, 0)
    photo(party, other, 1, status=MediaStatus.PENDING)
    photo(party, beboer, 2, deleted_at=timezone.now())
    photo(party, beboer, 3, derivative_state=DerivativeState.PENDING)  # counted, not shown yet
    post(beboer, album=party)

    # The uploader of the pending picture sees what everyone sees.
    [widget] = album_widgets(client_for(other).get(BOARD).content.decode())

    assert "2 billeder" in widget
    assert pictures(widget) == [f"/photo-album/{party.pk}/thumbnails/0.jpg"]


def test_an_empty_album_still_has_its_widget(beboer: Resident) -> None:
    post(beboer, album=album())

    [widget] = album_widgets(client_for(beboer).get(BOARD).content.decode())

    assert "album-embed-n0" in widget
    assert "Ingen billeder endnu" in widget


def test_deleting_the_album_keeps_the_post(beboer: Resident) -> None:
    party = album()
    notice = post(beboer, "Billeder på vej", album=party)

    party.delete()

    notice.refresh_from_db()
    assert notice.album is None
    assert album_widgets(client_for(beboer).get(BOARD).content.decode()) == []


# --- the film's widget ----------------------------------------------------------------------------


def test_a_post_about_a_film_shows_gahkflixs_own_widget(beboer: Resident, videos_open: None) -> None:
    show = Series.objects.create(title="Revyen", created_by=beboer)
    film = make_video(
        beboer, title="Revy 2009", series=show, episode=3, duration=754.0, description="Sange og sketches."
    )
    post(beboer, "Se den!", video=film)

    html = client_for(beboer).get(BOARD).content.decode()

    assert '<div class="flix-embed">' in html
    assert '<span class="flix-embed-logo" aria-hidden="true">GAHKFLIX</span>' in html
    assert f'<a class="flix-embed-title" href="/intern/gahkflix/v/{film.pk}">Revy 2009</a>' in html
    assert f'href="/intern/gahkflix/v/{film.pk}/afspil"' in html
    assert '<b aria-hidden="true">G</b>Revyen' in html
    for item in ("Afsnit 3", "13 min", "HD", "Sange og sketches."):
        assert item in html
    assert re.search(rf'class="flix-embed-img" src="/intern/gahkflix/v/{film.pk}/billede/banner\?v=', html)


def test_the_film_widget_says_how_far_the_reader_got(
    beboer: Resident, other: Resident, videos_open: None
) -> None:
    film = make_video(beboer, duration=600.0)
    post(beboer, video=film)
    Watch.objects.create(video=film, resident=other, position=150.0, position_at=timezone.now())

    theirs = client_for(other).get(BOARD).content.decode()
    mine = client_for(beboer).get(BOARD).content.decode()

    assert '<div class="flix-embed has-progress">' in theirs
    assert '<span style="width:25%"></span>' in theirs
    assert ">Fortsæt</a>" in theirs
    assert ">Afspil</a>" in mine
    assert "flix-progress" not in mine


def test_a_film_binned_after_it_was_attached_shows_only_for_the_administrators(
    beboer: Resident, member: Resident, videos_open: None
) -> None:
    film = make_video(beboer)
    post(beboer, "Se den", video=film)
    film.deleted_at = timezone.now()
    film.save()

    assert "flix-embed" not in client_for(beboer).get(BOARD).content.decode()
    administrator = client_for(member).get(BOARD).content.decode()
    assert '<span class="flix-state is-binned">I papirkurven</span>' in administrator


def test_no_film_widget_for_a_reader_gahkflix_keeps_out(beboer: Resident, member: Resident) -> None:
    """GAHKFlix's own rollout gate: its widget would be a play button that answers 403."""
    film = make_video(member)
    post(member, "Ny film", video=film, album=album())

    plain = client_for(beboer).get(BOARD).content.decode()

    assert "flix-embed" not in plain
    assert len(album_widgets(plain)) == 1
    assert "flix-embed" in client_for(member).get(BOARD).content.decode()


def test_the_kitchen_account_gets_no_widgets(
    make_resident: Callable[..., Resident], videos_open: None
) -> None:
    """It can open neither the photo album nor GAHKFlix (core.middleware.KitchenAccountGate)."""
    kitchen = make_resident(email="koekken@gahk.dk", is_kitchen=True)
    post(kitchen, "Fra køkkenet", album=album(), video=make_video(kitchen))

    response = client_for(kitchen).get(BOARD)

    assert response.status_code == 200
    assert album_widgets(response.content.decode()) == []
    assert "flix-embed" not in response.content.decode()


@override_settings(ROOT_URLCONF=__name__)
def test_the_widgets_follow_their_pages_to_new_addresses(beboer: Resident, videos_open: None) -> None:
    """Posts hold the album and the film, not their addresses: move the pages and every post follows."""
    party = album()
    film = make_video(beboer)
    post(beboer, album=party, video=film)

    html = client_for(beboer).get(BOARD).content.decode()

    assert f'href="/billeder/{party.pk}"' in html
    assert f'<a class="flix-embed-title" href="/film/v/{film.pk}">' in html
    assert f'href="/film/v/{film.pk}/afspil"' in html
    assert "/intern/fotoalbum/" not in html.split('id="opslag-')[1]
    assert "/intern/gahkflix/v/" not in html.split('id="opslag-')[1]


def test_widgets_cost_the_same_queries_however_many_posts_carry_one(
    beboer: Resident, videos_open: None
) -> None:
    notices = [post(beboer, "Ingen vedhæftning") for _ in range(4)]
    client = client_for(beboer)

    def attach(notice: Notice, n: int) -> None:
        party = album(f"Album {n}")
        photo(party, beboer, n)
        notice.album = party
        notice.video = make_video(beboer, title=f"Film {n}")
        notice.save()

    def board_queries() -> int:
        with CaptureQueriesContext(connection) as queries:
            assert client.get(BOARD).status_code == 200
        return len(queries.captured_queries)

    attach(notices[0], 0)
    one_attached = board_queries()
    for n, notice in enumerate(notices[1:], start=1):
        attach(notice, n)

    assert board_queries() == one_attached
    html = client.get(BOARD).content.decode()
    assert len(album_widgets(html)) == 4
    assert html.count('<div class="flix-embed">') == 4


# --- posting ---------------------------------------------------------------------------------------


def test_an_album_can_be_the_whole_post(
    beboer: Resident, other: Resident, monkeypatch: pytest.MonkeyPatch
) -> None:
    party = album()
    told: list[str] = []
    monkeypatch.setattr(
        board_services.services, "notify", lambda *args, **kwargs: told.append(kwargs["body"])
    )

    response = client_for(beboer).post(BOARD + "opret", {"category": "nyt", "body": "", "album": party.pk})

    notice = Notice.objects.get()
    assert response.status_code == 302
    assert (notice.body, notice.album) == ("", party)
    assert told == ["📷 Julefrokost 2026"]
    assert len(album_widgets(client_for(other).get(BOARD).content.decode())) == 1


def test_a_post_needs_text_or_something_attached(beboer: Resident) -> None:
    response = client_for(beboer).post(BOARD + "opret", {"category": "nyt", "body": "  "})

    assert response.status_code == 200
    assert "Skriv et indhold, eller vedhæft et album eller en video." in response.content.decode()
    assert not Notice.objects.exists()


def test_a_draft_cannot_be_attached(beboer: Resident, videos_open: None) -> None:
    draft = make_video(beboer, published=False)

    response = client_for(beboer).post(
        BOARD + "opret", {"category": "nyt", "body": "Snart!", "video": draft.pk}
    )

    assert response.status_code == 200
    assert not Notice.objects.exists()


def test_a_film_cannot_be_attached_past_gahkflixs_gate(beboer: Resident, member: Resident) -> None:
    """The field is not there for a reader GAHKFlix keeps out, so a posted id is ignored."""
    film = make_video(member)

    client_for(beboer).post(BOARD + "opret", {"category": "nyt", "body": "Hej", "video": film.pk})

    assert Notice.objects.get().video is None


def test_editing_a_post_can_change_and_remove_what_it_carries(beboer: Resident, videos_open: None) -> None:
    first, second = album("Fredag"), album("Lørdag")
    notice = post(beboer, "Billeder", album=first)
    client = client_for(beboer)

    client.post(f"{BOARD}{notice.pk}/rediger", {"category": "nyt", "body": "Billeder", "album": second.pk})
    notice.refresh_from_db()
    assert notice.album == second

    client.post(f"{BOARD}{notice.pk}/rediger", {"category": "nyt", "body": "Billeder", "album": ""})
    notice.refresh_from_db()
    assert notice.album is None


def test_a_post_keeps_its_film_through_an_edit_after_the_film_was_binned(
    member: Resident, videos_open: None
) -> None:
    """An administrator still sees it, so their edit must not throw it away."""
    film = make_video(member)
    notice = post(member, "Se den", video=film)
    film.deleted_at = timezone.now()
    film.save()

    client_for(member).post(
        f"{BOARD}{notice.pk}/rediger", {"category": "nyt", "body": "Se den!", "video": film.pk}
    )

    notice.refresh_from_db()
    assert (notice.body, notice.video) == ("Se den!", film)


# --- the composer and its pickers -------------------------------------------------------------------


def test_sharing_an_album_opens_the_composer_with_it_attached(beboer: Resident) -> None:
    party = album()
    photo(party, beboer, 0)

    html = client_for(beboer).get(BOARD + f"opret?album={party.pk}").content.decode()

    assert hidden(html, "album") == str(party.pk)
    assert len(album_widgets(html)) == 1
    assert re.search(r"<textarea[^>]*autofocus", html)
    # The widget opens in a new tab from the composer, so following it does not lose the draft.
    assert 'target="_blank"' in album_widgets(html)[0]
    assert "Fjern albummet" in html


def test_sharing_a_film_opens_the_composer_with_it_attached(beboer: Resident, videos_open: None) -> None:
    film = make_video(beboer, title="Revy 2009")

    html = client_for(beboer).get(BOARD + f"opret?video={film.pk}").content.decode()

    assert hidden(html, "video") == str(film.pk)
    assert '<div class="flix-embed">' in html
    assert "Fjern videoen" in html


@pytest.mark.parametrize("query", ["album=999999", "album=abc", "album=99999999999999999999", "noget=1"])
def test_sharing_nothing_attachable_opens_an_empty_composer(beboer: Resident, query: str) -> None:
    html = client_for(beboer).get(BOARD + f"opret?{query}").content.decode()

    assert hidden(html, "album") == ""
    assert not re.search(r"<textarea[^>]*autofocus", html)


def test_a_draft_cannot_be_shared(beboer: Resident, videos_open: None) -> None:
    draft = make_video(beboer, published=False)

    html = client_for(beboer).get(BOARD + f"opret?video={draft.pk}").content.decode()

    assert hidden(html, "video") == ""


def test_the_composer_offers_what_the_reader_may_attach(beboer: Resident, member: Resident) -> None:
    plain = client_for(beboer).get(BOARD + "opret").content.decode()
    fotogruppen = client_for(member).get(BOARD + "opret").content.decode()

    assert 'data-attach-open="album"' in plain
    assert 'data-attach-open="video"' not in plain
    assert 'id="js-attach-video"' not in plain
    assert 'data-attach-open="video"' in fotogruppen
    assert 'id="js-attach-video"' in fotogruppen


def test_the_album_picker_lists_the_newest_and_finds_by_name(beboer: Resident) -> None:
    old = album("Forårsrengøring")
    party = album()
    photo(party, beboer, 0)
    client = client_for(beboer)

    newest = client.get(BOARD + "vedhaeft/album").content.decode()
    found = client.get(BOARD + "vedhaeft/album", {"q": "forår"}).content.decode()
    none = client.get(BOARD + "vedhaeft/album", {"q": "xyz"}).content.decode()

    assert newest.index("Julefrokost 2026") < newest.index("Forårsrengøring")
    assert f'hx-get="{BOARD}vedhaeft?album={party.pk}"' in newest
    assert f'<img src="/photo-album/{party.pk}/thumbnails/0.jpg"' in newest
    assert "2026 · 1 billede" in newest
    assert f"?album={old.pk}" in found and "Julefrokost" not in found
    assert "Ingen albummer matcher «xyz»." in none


def test_the_film_picker_lists_published_films_only(beboer: Resident, videos_open: None) -> None:
    show = Series.objects.create(title="Gangen", created_by=beboer)
    film = make_video(beboer, title="Pilot", series=show)
    draft = make_video(beboer, title="Ufærdig", published=False)
    binned = make_video(beboer, title="Væk", deleted_at=timezone.now())
    client = client_for(beboer)

    listed = client.get(BOARD + "vedhaeft/video").content.decode()
    by_series = client.get(BOARD + "vedhaeft/video", {"q": "gang"}).content.decode()

    assert f"?video={film.pk}" in listed
    assert f"?video={draft.pk}" not in listed
    assert f"?video={binned.pk}" not in listed
    assert f"?video={film.pk}" in by_series


def test_picking_redraws_the_attachments_with_both(beboer: Resident, videos_open: None) -> None:
    party = album()
    film = make_video(beboer)
    client = client_for(beboer)

    both = client.get(BOARD + "vedhaeft", {"album": party.pk, "video": film.pk}).content.decode()
    album_removed = client.get(BOARD + "vedhaeft", {"video": film.pk}).content.decode()
    junk = client.get(BOARD + "vedhaeft", {"album": "999999", "video": "x"}).content.decode()

    assert (hidden(both, "album"), hidden(both, "video")) == (str(party.pk), str(film.pk))
    assert len(album_widgets(both)) == 1 and '<div class="flix-embed">' in both
    assert (hidden(album_removed, "album"), hidden(album_removed, "video")) == ("", str(film.pk))
    assert (hidden(junk, "album"), hidden(junk, "video")) == ("", "")
    assert 'data-attach-open="album"' in junk


def test_the_preview_tab_shows_the_widgets(beboer: Resident) -> None:
    party = album()

    response = client_for(beboer).post(BOARD + "forhaandsvisning", {"body": "", "album": party.pk})

    assert len(album_widgets(response.content.decode())) == 1


@pytest.mark.parametrize("url", ["vedhaeft", "vedhaeft/album", "vedhaeft/video"])
def test_the_composer_endpoints_are_for_residents(client: Client, url: str) -> None:
    response = client.get(BOARD + url)

    assert response.status_code == 302
    assert response["Location"].startswith("/intern/admin/login")


# --- "Del på opslagstavlen" -------------------------------------------------------------------------


def test_the_album_page_offers_to_share_it(beboer: Resident) -> None:
    party = album()

    html = client_for(beboer).get(f"/intern/fotoalbum/{party.pk}").content.decode()

    assert f'<a class="btn-link" href="{BOARD}opret?album={party.pk}">Del på opslagstavlen</a>' in html


def test_no_share_button_for_a_reader_the_board_keeps_out(
    beboer: Resident, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(board_access, "ACCESS_ROLES", (Role.INSPEKTION,))

    html = client_for(beboer).get(f"/intern/fotoalbum/{album().pk}").content.decode()

    assert "Del på opslagstavlen" not in html


def test_a_published_film_offers_to_share_it_and_a_draft_does_not(
    beboer: Resident, videos_open: None
) -> None:
    film = make_video(beboer)
    draft = make_video(beboer, title="Kladde", published=False)
    client = client_for(beboer)

    assert (
        f'href="{BOARD}opret?video={film.pk}"' in client.get(f"/intern/gahkflix/v/{film.pk}").content.decode()
    )
    assert "Del på opslagstavlen" not in client.get(f"/intern/gahkflix/v/{draft.pk}").content.decode()
