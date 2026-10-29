"""Albums and films embedded in a post on opslagstavlen (core.embeds, opslagstavle.embeds): drawn as
their own features draw themselves, the composer's pickers, and "Del på opslagstavlen" on the
album's and the film's pages."""

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
from core import embeds as embed_syntax
from gahkflix import access as gahkflix_access
from gahkflix.models import Series, Video, Watch
from opslagstavle import access as board_access
from opslagstavle import embeds
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


def post(author: Resident, body: str) -> Notice:
    return Notice.objects.create(author=author, body=body)


def album(name: str = "Julefrokost 2026", **fields: object) -> Album:
    return Album.objects.create(folder="2026", name=name, **fields)


def embed(thing: Album | Video) -> str:
    """The Markdown that embeds an album or a film, as the picker writes it."""
    if isinstance(thing, Album):
        return embed_syntax.markdown("album", thing.pk, thing.name)
    return embed_syntax.markdown("gahkflix", thing.pk, thing.title)


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


def textarea(html: str) -> tuple[str, str]:
    """The composer's body field: its tag, and what it holds."""
    found = re.search(r"(<textarea[^>]*>)(.*?)</textarea>", html, re.DOTALL)
    assert found is not None
    return found.group(1), found.group(2)


def body_of(html: str) -> str:
    """The post's own text, as drawn: from its .board-body to its foot."""
    return html.split('<div class="board-body prose-gahk">')[1].split(
        '<div class="notice-foot">', maxsplit=1
    )[0]


# --- the album's widget ---------------------------------------------------------------------------


def test_an_album_is_drawn_as_the_photo_albums_own_widget_where_it_is_embedded(beboer: Resident) -> None:
    party = album()
    for minute in range(3):
        photo(party, beboer, minute)
    notice = post(beboer, f"Så er billederne oppe!\n\n{embed(party)}\n\nTak for i aftes.")

    text = body_of(client_for(beboer).get(f"{BOARD}{notice.pk}").content.decode())

    [widget] = album_widgets(text)
    assert widget.startswith(
        f'<a class="album-timeline-card album-embed album-embed-n3" href="/intern/fotoalbum/{party.pk}">'
    )
    assert '<span class="album-eyebrow">Fotoalbum · 2026</span>' in widget
    assert "<strong>Julefrokost 2026</strong>" in widget
    assert "3 billeder" in widget and "Se albummet →" in widget
    # Newest first, as the album page shows them.
    assert pictures(widget) == [f"/photo-album/{party.pk}/thumbnails/{minute}.jpg" for minute in (2, 1, 0)]
    # Where the author put it, between the two paragraphs, and nothing of the embed left in the text.
    assert text.index("Så er billederne oppe!") < text.index(widget) < text.index("Tak for i aftes.")
    assert "album:" not in text.replace(widget, "")


def test_the_feed_shows_the_excerpt_and_the_first_widget_under_it(beboer: Resident) -> None:
    first, second = album("Fredag"), album("Lørdag")
    notice = post(beboer, f"Fredag:\n\n{embed(first)}\n\nOg lørdag:\n\n{embed(second)}")
    client = client_for(beboer)

    feed = client.get(BOARD).content.decode()
    page = client.get(f"{BOARD}{notice.pk}").content.decode()

    [widget] = album_widgets(feed)
    assert "<strong>Fredag</strong>" in widget
    excerpt = feed.split('<div class="board-body prose-gahk">')[1].split("</div>")[0]
    assert "Fredag:" in excerpt and "Og lørdag:" in excerpt and "album-embed" not in excerpt
    assert [
        ("<strong>Fredag</strong>" in each, "<strong>Lørdag</strong>" in each) for each in album_widgets(page)
    ] == [
        (True, False),
        (False, True),
    ]


def test_an_embed_in_a_sentence_is_drawn_after_its_paragraph(beboer: Resident) -> None:
    notice = post(beboer, f"Se {embed(album())} fra i går.")

    text = body_of(client_for(beboer).get(f"{BOARD}{notice.pk}").content.decode())

    [widget] = album_widgets(text)
    assert text.index("<p>Se  fra i går.</p>") < text.index(widget)


def test_the_album_widget_counts_pictures_and_videos_all_the_way_down(beboer: Resident) -> None:
    roskilde = album("Roskilde")
    saturday = album("Lørdag", parent=roskilde)
    for minute in range(4):
        photo(roskilde, beboer, minute)
    for minute in range(4, 9):
        photo(saturday, beboer, minute)
    photo(saturday, beboer, 9, content_type="video/mp4")
    notice = post(beboer, f"{embed(roskilde)}\n\n{embed(saturday)}")

    root, sub = album_widgets(client_for(beboer).get(f"{BOARD}{notice.pk}").content.decode())

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
    post(beboer, embed(party))

    # The uploader of the pending picture sees what everyone sees.
    [widget] = album_widgets(client_for(other).get(BOARD).content.decode())

    assert "2 billeder" in widget
    assert pictures(widget) == [f"/photo-album/{party.pk}/thumbnails/0.jpg"]


def test_an_empty_album_still_has_its_widget(beboer: Resident) -> None:
    post(beboer, embed(album()))

    [widget] = album_widgets(client_for(beboer).get(BOARD).content.decode())

    assert "album-embed-n0" in widget and "Ingen billeder endnu" in widget


def test_an_album_that_is_gone_leaves_its_name(beboer: Resident) -> None:
    party = album()
    notice = post(beboer, f"Billeder:\n\n{embed(party)}")
    party.delete()

    text = body_of(client_for(beboer).get(f"{BOARD}{notice.pk}").content.decode())

    assert album_widgets(text) == []
    assert "<p>Julefrokost 2026</p>" in text


# --- the film's widget ----------------------------------------------------------------------------


def test_a_film_is_drawn_as_gahkflixs_own_widget(beboer: Resident, videos_open: None) -> None:
    show = Series.objects.create(title="Revyen", created_by=beboer)
    film = make_video(
        beboer, title="Revy 2009", series=show, episode=3, duration=754.0, description="Sange og sketches."
    )
    notice = post(beboer, f"Se den!\n\n{embed(film)}")

    text = body_of(client_for(beboer).get(f"{BOARD}{notice.pk}").content.decode())

    assert '<div class="flix-embed">' in text
    assert '<span class="flix-embed-logo" aria-hidden="true">GAHKFLIX</span>' in text
    assert f'<a class="flix-embed-title" href="/intern/gahkflix/v/{film.pk}">Revy 2009</a>' in text
    assert f'href="/intern/gahkflix/v/{film.pk}/afspil"' in text
    assert '<b aria-hidden="true">G</b>Revyen' in text
    for item in ("Afsnit 3", "13 min", "HD", "Sange og sketches."):
        assert item in text
    assert re.search(rf'class="flix-embed-img" src="/intern/gahkflix/v/{film.pk}/billede/banner\?v=', text)


def test_the_film_widget_says_how_far_the_reader_got(
    beboer: Resident, other: Resident, videos_open: None
) -> None:
    film = make_video(beboer, duration=600.0)
    post(beboer, embed(film))
    Watch.objects.create(video=film, resident=other, position=150.0, position_at=timezone.now())

    theirs = client_for(other).get(BOARD).content.decode()
    mine = client_for(beboer).get(BOARD).content.decode()

    assert '<div class="flix-embed has-progress">' in theirs
    assert '<span style="width:25%"></span>' in theirs
    assert ">Fortsæt</a>" in theirs
    assert ">Afspil</a>" in mine
    assert "flix-progress" not in mine


def test_a_draft_or_binned_film_is_drawn_only_for_those_who_may_see_it(
    beboer: Resident, other: Resident, member: Resident, videos_open: None
) -> None:
    draft = make_video(other, title="Ufærdig", published=False)
    binned = make_video(other, title="Væk", deleted_at=timezone.now())
    notice = post(other, f"{embed(draft)}\n\n{embed(binned)}")

    resident = client_for(beboer).get(f"{BOARD}{notice.pk}").content.decode()
    uploader = client_for(other).get(f"{BOARD}{notice.pk}").content.decode()
    administrator = client_for(member).get(f"{BOARD}{notice.pk}").content.decode()

    assert "flix-embed" not in resident
    assert "<p>Ufærdig</p>" in resident and "<p>Væk</p>" in resident
    assert '<span class="flix-state is-draft">Kladde</span>' in uploader
    assert '<span class="flix-state is-binned">I papirkurven</span>' in administrator


def test_no_film_widget_for_a_reader_gahkflix_keeps_out(beboer: Resident, member: Resident) -> None:
    """GAHKFlix's own rollout gate: its widget would be a play button that answers 403."""
    film = make_video(member, title="Revy")
    notice = post(member, f"{embed(film)}\n\n{embed(album())}")

    plain = body_of(client_for(beboer).get(f"{BOARD}{notice.pk}").content.decode())

    assert "flix-embed" not in plain
    assert "<p>Revy</p>" in plain
    assert len(album_widgets(plain)) == 1
    assert "flix-embed" in client_for(member).get(f"{BOARD}{notice.pk}").content.decode()


def test_the_kitchen_account_gets_no_widgets(make_resident: Callable[..., Resident]) -> None:
    """It can open neither the photo album nor GAHKFlix (core.middleware.KitchenAccountGate)."""
    kitchen = make_resident(email="koekken@gahk.dk", is_kitchen=True)
    post(kitchen, f"Fra køkkenet\n\n{embed(album())}")

    response = client_for(kitchen).get(BOARD)

    assert response.status_code == 200
    assert album_widgets(response.content.decode()) == []
    assert "Julefrokost 2026" in response.content.decode()


@override_settings(ROOT_URLCONF=__name__)
def test_the_widgets_follow_their_pages_to_new_addresses(beboer: Resident, videos_open: None) -> None:
    """Posts hold what they embed, not its address: move the pages and every post follows."""
    party = album()
    film = make_video(beboer)
    notice = post(beboer, f"{embed(party)}\n\n{embed(film)}")

    text = body_of(client_for(beboer).get(f"{BOARD}{notice.pk}").content.decode())

    assert f'href="/billeder/{party.pk}"' in text
    assert f'<a class="flix-embed-title" href="/film/v/{film.pk}">' in text
    assert f'href="/film/v/{film.pk}/afspil"' in text
    assert "/intern/fotoalbum/" not in text
    assert "/intern/gahkflix/v/" not in text


def test_widgets_cost_the_same_queries_however_many_posts_embed_one(
    beboer: Resident, videos_open: None
) -> None:
    notices = [post(beboer, "Ingen indlejringer") for _ in range(4)]
    client = client_for(beboer)

    def embedding(notice: Notice, n: int) -> None:
        party = album(f"Album {n}")
        photo(party, beboer, n)
        notice.body = f"{embed(party)}\n\n{embed(make_video(beboer, title=f'Film {n}'))}"
        notice.save()

    def board_queries() -> int:
        with CaptureQueriesContext(connection) as queries:
            assert client.get(BOARD).status_code == 200
        return len(queries.captured_queries)

    embedding(notices[0], 0)
    one_embedding = board_queries()
    for n, notice in enumerate(notices[1:], start=1):
        embedding(notice, n)

    assert board_queries() == one_embedding


def test_a_post_draws_at_most_ten_widgets(beboer: Resident) -> None:
    albums = [album(f"Album {n}") for n in range(embeds.MAX_PER_POST + 1)]
    notice = post(beboer, "\n\n".join(embed(each) for each in albums))

    text = body_of(client_for(beboer).get(f"{BOARD}{notice.pk}").content.decode())

    assert len(album_widgets(text)) == embeds.MAX_PER_POST
    assert f"<p>Album {embeds.MAX_PER_POST}</p>" in text


def test_core_and_the_board_know_the_same_kinds() -> None:
    assert set(embed_syntax.KINDS) == set(embeds.KINDS)


def test_an_album_can_be_the_whole_post(
    beboer: Resident, other: Resident, monkeypatch: pytest.MonkeyPatch
) -> None:
    party = album()
    told: list[str] = []
    monkeypatch.setattr(
        board_services.services, "notify", lambda *args, **kwargs: told.append(kwargs["body"])
    )

    response = client_for(beboer).post(BOARD + "opret", {"category": "nyt", "body": embed(party)})

    assert response.status_code == 302
    # The notification names what the post embeds.
    assert told == ["📷 Julefrokost 2026"]
    assert len(album_widgets(client_for(other).get(BOARD).content.decode())) == 1


# --- the composer and its pickers -------------------------------------------------------------------


def test_sharing_an_album_starts_a_post_with_it_embedded(beboer: Resident) -> None:
    party = album()
    photo(party, beboer, 0)

    html = client_for(beboer).get(BOARD + f"opret?album={party.pk}").content.decode()

    tag, body = textarea(html)
    assert "autofocus" in tag
    # An empty line on top for the text, where the cursor is.
    assert body.strip("\n") == f"![Julefrokost 2026](album:{party.pk})"
    assert body.startswith("\n\n\n")
    [widget] = album_widgets(html)
    # In the composer a widget opens a new tab, so following it does not lose the draft.
    assert 'target="_blank"' in widget
    assert f'data-embed-remove="album:{party.pk}"' in html


def test_sharing_a_film_starts_a_post_with_it_embedded(beboer: Resident, videos_open: None) -> None:
    film = make_video(beboer, title="Revy 2009")

    html = client_for(beboer).get(BOARD + f"opret?gahkflix={film.pk}").content.decode()

    assert f"![Revy 2009](gahkflix:{film.pk})" in textarea(html)[1]
    assert '<div class="flix-embed">' in html


def test_a_shared_name_stays_one_embed(beboer: Resident) -> None:
    party = album("[Revy] 2009")
    client = client_for(beboer)

    body = textarea(client.get(BOARD + f"opret?album={party.pk}").content.decode())[1].strip()
    assert body == rf"![\[Revy\] 2009](album:{party.pk})"

    client.post(BOARD + "opret", {"category": "nyt", "body": body})

    assert "<strong>[Revy] 2009</strong>" in album_widgets(client.get(BOARD).content.decode())[0]


@pytest.mark.parametrize("query", ["album=999999", "album=abc", "album=99999999999999999999", "noget=1"])
def test_sharing_nothing_embeddable_opens_an_empty_composer(beboer: Resident, query: str) -> None:
    tag, body = textarea(client_for(beboer).get(BOARD + f"opret?{query}").content.decode())

    assert body.strip() == ""
    assert "autofocus" not in tag


def test_a_draft_cannot_be_shared(beboer: Resident, videos_open: None) -> None:
    draft = make_video(beboer, published=False)

    html = client_for(beboer).get(BOARD + f"opret?gahkflix={draft.pk}").content.decode()

    assert textarea(html)[1].strip() == ""


def test_the_composer_offers_what_the_reader_may_embed(beboer: Resident, member: Resident) -> None:
    plain = client_for(beboer).get(BOARD + "opret").content.decode()
    fotogruppen = client_for(member).get(BOARD + "opret").content.decode()

    assert 'data-embed-open="album"' in plain
    assert 'data-embed-open="gahkflix"' not in plain
    assert 'id="js-embed-gahkflix"' not in plain
    assert 'data-embed-open="gahkflix"' in fotogruppen
    assert 'id="js-embed-gahkflix"' in fotogruppen


def test_the_album_picker_lists_the_newest_and_finds_by_name(beboer: Resident) -> None:
    old = album("Forårsrengøring")
    party = album()
    photo(party, beboer, 0)
    client = client_for(beboer)

    newest = client.get(BOARD + "indlejr/album").content.decode()
    found = client.get(BOARD + "indlejr/album", {"q": "forår"}).content.decode()
    none = client.get(BOARD + "indlejr/album", {"q": "xyz"}).content.decode()

    assert newest.index("Julefrokost 2026") < newest.index("Forårsrengøring")
    assert f'data-embed="![Julefrokost 2026](album:{party.pk})"' in newest
    assert f'<img src="/photo-album/{party.pk}/thumbnails/0.jpg"' in newest
    assert "2026 · 1 billede" in newest
    assert f"(album:{old.pk})" in found
    assert "Julefrokost" not in found
    assert "Ingen albummer matcher «xyz»." in none


def test_the_film_picker_lists_published_films_only(beboer: Resident, videos_open: None) -> None:
    show = Series.objects.create(title="Gangen", created_by=beboer)
    film = make_video(beboer, title="Pilot", series=show)
    draft = make_video(beboer, title="Ufærdig", published=False)
    binned = make_video(beboer, title="Væk", deleted_at=timezone.now())
    client = client_for(beboer)

    listed = client.get(BOARD + "indlejr/gahkflix").content.decode()
    by_series = client.get(BOARD + "indlejr/gahkflix", {"q": "gang"}).content.decode()

    assert f"(gahkflix:{film.pk})" in listed
    assert f"(gahkflix:{draft.pk})" not in listed
    assert f"(gahkflix:{binned.pk})" not in listed
    assert f"(gahkflix:{film.pk})" in by_series


def test_no_film_picker_for_a_reader_gahkflix_keeps_out(beboer: Resident, member: Resident) -> None:
    make_video(member, title="Revy")

    assert "Revy" not in client_for(beboer).get(BOARD + "indlejr/gahkflix").content.decode()


def test_the_composer_draws_the_widgets_the_body_embeds(beboer: Resident) -> None:
    party = album()
    client = client_for(beboer)

    drawn = client.post(BOARD + "indlejret", {"body": f"Hej\n\n{embed(party)}"}).content.decode()
    nothing = client.post(BOARD + "indlejret", {"body": "Ingen indlejringer"}).content.decode()

    assert len(album_widgets(drawn)) == 1
    assert f'data-embed-remove="album:{party.pk}"' in drawn
    assert album_widgets(nothing) == []


def test_the_preview_tab_draws_the_widgets_where_they_stand(beboer: Resident) -> None:
    party = album()

    response = client_for(beboer).post(
        BOARD + "forhaandsvisning", {"body": f"Før\n\n{embed(party)}\n\nEfter"}
    )

    html = response.content.decode()
    [widget] = album_widgets(html)
    assert html.index("Før") < html.index(widget) < html.index("Efter")


@pytest.mark.parametrize(
    ("method", "url"), [("get", "indlejr/album"), ("get", "indlejr/gahkflix"), ("post", "indlejret")]
)
def test_the_composer_endpoints_are_for_residents(client: Client, method: str, url: str) -> None:
    response = getattr(client, method)(BOARD + url)

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
        f'href="{BOARD}opret?gahkflix={film.pk}"'
        in client.get(f"/intern/gahkflix/v/{film.pk}").content.decode()
    )
    assert "Del på opslagstavlen" not in client.get(f"/intern/gahkflix/v/{draft.pk}").content.decode()
