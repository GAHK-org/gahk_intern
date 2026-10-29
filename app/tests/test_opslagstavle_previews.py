"""Cards on opslagstavlen for the photo albums and GAHKFlix videos a post links to
(opslagstavle.previews), and "Del på opslagstavlen" on the album's and the video's own pages."""

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from gahkflix import access as gahkflix_access
from gahkflix.models import Series
from opslagstavle import access as board_access
from opslagstavle.models import Notice
from photo_album.models import Album, DerivativeState, Media, MediaStatus
from residents.models import Resident, Role

from .test_gahkflix import make_video

BOARD = "/intern/opslagstavle/"
START = datetime(2026, 12, 12, 18, tzinfo=UTC)
pytestmark = pytest.mark.django_db


@pytest.fixture
def beboer(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="beboer@gahk.dk", first_name="Bo", last_name="Beboer")


@pytest.fixture
def other(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="anden@gahk.dk", first_name="Ann", last_name="Anden")


@pytest.fixture(autouse=True)
def site(settings: object) -> None:
    """The site's own address, whatever a developer's app/.env says: links naming it are ours."""
    settings.SITE_URL = "https://gahk.dk"  # type: ignore[attr-defined]


@pytest.fixture
def member_client(make_resident: Callable[..., Resident]) -> Client:
    """Fotogruppen: inside GAHKFlix's rollout gate."""
    return client_for(make_resident(email="foto@gahk.dk", roles=(Role.FOTO,)))


@pytest.fixture
def videos_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift GAHKFlix's rollout gate: these tests are about the cards. The ones about the gate say so."""
    monkeypatch.setattr(gahkflix_access, "ACCESS_ROLES", None)


def client_for(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def post(author: Resident, body: str) -> Notice:
    return Notice.objects.create(author=author, body=body)


def album(name: str = "Julefrokost 2026", **fields: object) -> Album:
    return Album.objects.create(folder="2026", name=name, **fields)


def photo(into: Album, owner: Resident, minute: int, **fields: object) -> Media:
    defaults: dict[str, object] = {
        "title": "foto",
        "original": "o",
        "high_definition": "h",
        "thumbnail": f"photo-album/{into.pk}/thumbnails/{minute}.jpg",
        "status": MediaStatus.APPROVED,
        "captured_at": START + timedelta(minutes=minute),
    }
    return Media.objects.create(album=into, requested_by=owner, **{**defaults, **fields})


def cards(html: str) -> list[str]:
    """Each card on the page, as its HTML, in order."""
    return re.findall(r'<a class="notice-preview is-.*?</a>', html, re.DOTALL)


def textarea(html: str) -> tuple[str, str]:
    """The composer's body field: its tag, and what it holds."""
    found = re.search(r"(<textarea[^>]*>)(.*?)</textarea>", html, re.DOTALL)
    assert found is not None
    return found.group(1), found.group(2).strip()


def pictures(card: str) -> list[str]:
    return re.findall(r'<img src="([^"]+)"', card)


def album_link(target: Album) -> str:
    return f"[Billederne]({album_url(target)})"


def album_url(target: Album) -> str:
    return f"/intern/fotoalbum/{target.pk}"


# --- albums ---------------------------------------------------------------------------------------


def test_a_post_linking_an_album_shows_a_card_with_its_pictures(beboer: Resident) -> None:
    party = album()
    for minute in range(3):
        photo(party, beboer, minute)
    post(beboer, f"Så er billederne oppe! {album_link(party)}")

    [card] = cards(client_for(beboer).get(BOARD).content.decode())

    assert f'href="{album_url(party)}"' in card
    assert "Julefrokost 2026" in card
    assert "Fotoalbum · 2026" in card
    assert "3 medier" in card
    # Newest first, as the album page shows them.
    assert pictures(card) == [f"/photo-album/{party.pk}/thumbnails/{minute}.jpg" for minute in (2, 1, 0)]


def test_an_album_card_shows_the_newest_four_of_the_album_and_its_sub_albums(beboer: Resident) -> None:
    roskilde = album("Roskilde")
    saturday = album("Lørdag", parent=roskilde)
    photo(roskilde, beboer, 0)
    photo(roskilde, beboer, 3)
    for minute in (1, 2, 4, 5):
        photo(saturday, beboer, minute)
    post(beboer, f"{album_link(roskilde)} og {album_link(saturday)}")

    root_card, sub_card = cards(client_for(beboer).get(f"{BOARD}{Notice.objects.get().pk}").content.decode())

    assert "6 medier" in root_card
    assert pictures(root_card) == [
        f"/photo-album/{saturday.pk}/thumbnails/5.jpg",
        f"/photo-album/{saturday.pk}/thumbnails/4.jpg",
        f"/photo-album/{roskilde.pk}/thumbnails/3.jpg",
        f"/photo-album/{saturday.pk}/thumbnails/2.jpg",
    ]
    # A sub-album says where it lives.
    assert "Fotoalbum · 2026 · Roskilde" in sub_card
    assert "4 medier" in sub_card


def test_an_album_card_leaves_out_what_not_every_reader_may_see(beboer: Resident, other: Resident) -> None:
    party = album()
    photo(party, beboer, 0)
    photo(party, other, 1, status=MediaStatus.PENDING)
    photo(party, beboer, 2, deleted_at=timezone.now())
    # Approved but its thumbnail not built yet: counted, not shown.
    photo(party, beboer, 3, derivative_state=DerivativeState.PENDING)
    post(beboer, album_link(party))

    # The uploader of the pending photo sees the same card as everyone else.
    [card] = cards(client_for(other).get(BOARD).content.decode())

    assert "2 medier" in card
    assert pictures(card) == [f"/photo-album/{party.pk}/thumbnails/0.jpg"]


def test_an_empty_album_still_has_a_card(beboer: Resident) -> None:
    party = album()
    post(beboer, album_link(party))

    [card] = cards(client_for(beboer).get(BOARD).content.decode())

    assert "Ingen medier endnu" in card
    assert pictures(card) == []
    assert "#i-photo" in card


@pytest.mark.parametrize(
    "link",
    [
        "[se her]({path})",
        "https://gahk.dk{path}",
        "gahk.dk{path}",  # how people type an address; linkify finds it
        "<https://www.gahk.dk{path}>",
        "http://testserver{path}",  # the host the page was opened on
        "[se her]({path}?media=12)",
    ],
)
def test_a_link_to_the_album_counts_however_it_is_written(beboer: Resident, link: str) -> None:
    party = album()
    post(beboer, "Billeder: " + link.format(path=album_url(party)))

    assert len(cards(client_for(beboer).get(BOARD).content.decode())) == 1


@pytest.mark.parametrize(
    "body",
    [
        "https://example.com{path}",  # somebody else's site with our path
        "`https://gahk.dk{path}`",  # code, not a link
        "    https://gahk.dk{path}",  # an indented code block
        "{path}",  # a bare path is text, not a link
        "[x](/intern/fotoalbum/999999)",  # no such album
        "[x](/intern/fotoalbum/99999999999999999999)",  # bigger than any key
        "[x](/intern/fotoalbum/)",
        "[x](javascript:alert(1))",
    ],
)
def test_other_links_draw_no_card(beboer: Resident, body: str) -> None:
    party = album()
    post(beboer, body.format(path=album_url(party)))

    response = client_for(beboer).get(BOARD)

    assert response.status_code == 200
    assert cards(response.content.decode()) == []


def test_an_album_linked_twice_has_one_card(beboer: Resident) -> None:
    party = album()
    post(beboer, f"{album_link(party)} og igen https://gahk.dk{album_url(party)}")

    assert len(cards(client_for(beboer).get(f"{BOARD}{Notice.objects.get().pk}").content.decode())) == 1


def test_the_feed_shows_the_first_card_and_the_post_shows_them_all(beboer: Resident) -> None:
    first, second = album("Fredag"), album("Lørdag")
    notice = post(beboer, f"{album_link(first)} og {album_link(second)}")
    client = client_for(beboer)

    [feed_card] = cards(client.get(BOARD).content.decode())
    post_cards = cards(client.get(f"{BOARD}{notice.pk}").content.decode())

    assert "Fredag" in feed_card
    assert ["Fredag" in card for card in post_cards] == [True, False]
    assert "Lørdag" in post_cards[1]


def test_a_card_in_the_feed_is_its_own_link_above_the_posts(beboer: Resident) -> None:
    """The feed card is a stretched link to the post; the album's card must go to the album."""
    party = album()
    post(beboer, album_link(party))

    html = client_for(beboer).get(BOARD).content.decode()

    assert 'class="notice-card card-flat is-clickable"' in html
    assert cards(html)[0].startswith(f'<a class="notice-preview is-album" href="{album_url(party)}">')


def test_the_kitchen_account_gets_no_cards(make_resident: Callable[..., Resident]) -> None:
    """It can open neither the photo album nor GAHKFlix (core.middleware.KitchenAccountGate)."""
    kitchen = make_resident(email="koekken@gahk.dk", is_kitchen=True)
    post(kitchen, album_link(album()))

    response = client_for(kitchen).get(BOARD)

    assert response.status_code == 200
    assert cards(response.content.decode()) == []


# --- videos ---------------------------------------------------------------------------------------


def test_a_post_linking_a_video_shows_its_thumbnail_and_running_time(
    beboer: Resident, videos_open: None
) -> None:
    show = Series.objects.create(title="Revyen", created_by=beboer)
    film = make_video(beboer, title="Revy 2009", series=show, episode=3, duration=754.0)
    post(beboer, f"Se den: [Revy 2009](/intern/gahkflix/v/{film.pk})")

    [card] = cards(client_for(beboer).get(BOARD).content.decode())

    assert 'class="notice-preview is-video"' in card
    assert f'href="/intern/gahkflix/v/{film.pk}"' in card
    assert "GAHKFlix · Revyen" in card
    assert "Afsnit 3" in card
    assert "13 min" in card
    assert pictures(card)[0].startswith(f"/intern/gahkflix/v/{film.pk}/billede/thumbnail?v=")


def test_a_link_to_the_player_draws_the_card_too(beboer: Resident, videos_open: None) -> None:
    film = make_video(beboer)
    post(beboer, f"https://gahk.dk/intern/gahkflix/v/{film.pk}/afspil")

    [card] = cards(client_for(beboer).get(BOARD).content.decode())

    assert f'href="/intern/gahkflix/v/{film.pk}"' in card


def test_a_draft_has_a_card_only_for_those_who_may_see_it_and_says_so(
    beboer: Resident, other: Resident, videos_open: None
) -> None:
    draft = make_video(other, title="Ufærdig", published=False)
    post(other, f"[snart](/intern/gahkflix/v/{draft.pk})")

    assert cards(client_for(beboer).get(BOARD).content.decode()) == []
    [card] = cards(client_for(other).get(BOARD).content.decode())
    assert '<span class="notice-preview-state">Kladde</span>' in card


def test_a_binned_video_has_a_card_only_for_the_administrators(
    beboer: Resident, make_resident: Callable[..., Resident], videos_open: None
) -> None:
    binned = make_video(beboer, deleted_at=timezone.now())
    post(beboer, f"[væk](/intern/gahkflix/v/{binned.pk})")
    administrator = make_resident(email="foto@gahk.dk", roles=(Role.FOTO,))

    assert cards(client_for(beboer).get(BOARD).content.decode()) == []
    [card] = cards(client_for(administrator).get(BOARD).content.decode())
    assert "I papirkurven" in card


def test_no_video_card_for_a_reader_gahkflix_keeps_out(
    beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """GAHKFlix's own rollout gate: a card would be a link that answers 403."""
    film = make_video(beboer)
    notice = post(beboer, f"[film](/intern/gahkflix/v/{film.pk}) og {album_link(album())}")
    member = make_resident(email="foto@gahk.dk", roles=(Role.FOTO,))

    def kinds(resident: Resident, page: str) -> list[str]:
        return [card.split('"')[1] for card in cards(client_for(resident).get(page).content.decode())]

    assert kinds(beboer, f"{BOARD}{notice.pk}") == ["notice-preview is-album"]
    assert kinds(member, f"{BOARD}{notice.pk}") == ["notice-preview is-video", "notice-preview is-album"]
    # The feed's one card is the first the reader may see.
    assert kinds(beboer, BOARD) == ["notice-preview is-album"]


def test_cards_cost_the_same_queries_however_many_posts_link(beboer: Resident, videos_open: None) -> None:
    """One page of posts linking to albums and videos costs a fixed handful of queries, not a few per post."""
    notices = [post(beboer, "Ingen links") for _ in range(4)]
    client = client_for(beboer)

    def linking(notice: Notice, n: int) -> None:
        party = album(f"Album {n}")
        photo(party, beboer, n)
        film = make_video(beboer, title=f"Film {n}")
        notice.body = f"{album_link(party)} [film](/intern/gahkflix/v/{film.pk})"
        notice.save()

    def board_queries() -> int:
        with CaptureQueriesContext(connection) as queries:
            assert client.get(BOARD).status_code == 200
        return len(queries.captured_queries)

    linking(notices[0], 0)
    one_linking = board_queries()
    for n, notice in enumerate(notices[1:], start=1):
        linking(notice, n)

    assert board_queries() == one_linking
    assert len(cards(client.get(BOARD).content.decode())) == 4


# --- composing -------------------------------------------------------------------------------------


def test_the_composer_draws_the_cards_as_the_body_is_typed(beboer: Resident) -> None:
    party = album()
    client = client_for(beboer)

    with_link = client.post(BOARD + "forhaandsvisning/kort", {"body": f"Se {album_link(party)}"})
    without = client.post(BOARD + "forhaandsvisning/kort", {"body": "Ingen links"})

    [card] = cards(with_link.content.decode())
    # A new tab, so following it does not lose the draft.
    assert 'target="_blank"' in card
    assert cards(without.content.decode()) == []


def test_the_composer_preview_shows_the_cards_too(beboer: Resident) -> None:
    party = album()

    response = client_for(beboer).post(BOARD + "forhaandsvisning", {"body": f"Se {album_link(party)}"})

    assert "Julefrokost 2026" in cards(response.content.decode())[0]


def test_the_card_endpoint_is_for_residents_only(client: Client) -> None:
    response = client.post(BOARD + "forhaandsvisning/kort", {"body": "x"})

    assert response.status_code == 302
    assert response["Location"].startswith("/intern/admin/login")


def test_the_edit_form_shows_the_posts_cards(beboer: Resident) -> None:
    notice = post(beboer, album_link(album()))

    html = client_for(beboer).get(f"{BOARD}{notice.pk}/rediger").content.decode()

    assert 'id="js-link-previews"' in html
    assert len(cards(html)) == 1


def test_the_composer_says_links_are_shown_with_pictures(beboer: Resident, member_client: Client) -> None:
    plain = client_for(beboer).get(BOARD + "opret").content.decode()
    member = member_client.get(BOARD + "opret").content.decode()

    assert "Et link til et fotoalbum bliver vist med billeder." in plain
    assert "Et link til et fotoalbum eller en video på GAHKFlix bliver vist med billeder." in member


# --- "Del på opslagstavlen" -----------------------------------------------------------------------


def test_sharing_an_album_starts_a_post_with_a_link_to_it(beboer: Resident) -> None:
    party = album()
    photo(party, beboer, 0)

    html = client_for(beboer).get(BOARD + f"opret?album={party.pk}").content.decode()

    tag, body = textarea(html)
    assert "autofocus" in tag
    assert body == f"[Julefrokost 2026]({album_url(party)})"
    assert len(cards(html)) == 1


def test_sharing_a_video_starts_a_post_with_a_link_to_it(beboer: Resident, videos_open: None) -> None:
    film = make_video(beboer, title="Revy 2009")

    html = client_for(beboer).get(BOARD + f"opret?video={film.pk}").content.decode()

    assert f"[Revy 2009](/intern/gahkflix/v/{film.pk})" in html


def test_a_shared_title_stays_one_link(beboer: Resident) -> None:
    party = album("[Revy] *2009*")
    client = client_for(beboer)

    html = client.get(BOARD + f"opret?album={party.pk}").content.decode()
    body = textarea(html)[1]
    assert body == rf"[\[Revy\] \*2009\*]({album_url(party)})"

    client.post(BOARD + "opret", {"category": "nyt", "body": body})
    page = client.get(BOARD).content.decode()

    assert f'<a href="{album_url(party)}" rel="noopener noreferrer nofollow">[Revy] *2009*</a>' in page


@pytest.mark.parametrize("query", ["album=999999", "album=abc", "album=", "video=1", "noget=1"])
def test_sharing_nothing_that_exists_opens_an_empty_composer(beboer: Resident, query: str) -> None:
    html = client_for(beboer).get(BOARD + f"opret?{query}").content.decode()

    assert textarea(html)[1] == ""
    assert "autofocus" not in textarea(html)[0]


def test_a_draft_cannot_be_shared(beboer: Resident, videos_open: None) -> None:
    draft = make_video(beboer, published=False)

    html = client_for(beboer).get(BOARD + f"opret?video={draft.pk}").content.decode()

    assert f"/intern/gahkflix/v/{draft.pk})" not in html


def test_the_album_page_offers_to_share_it(beboer: Resident) -> None:
    party = album()

    html = client_for(beboer).get(album_url(party)).content.decode()

    assert f'<a class="btn-link" href="{BOARD}opret?album={party.pk}">Del på opslagstavlen</a>' in html


def test_no_share_button_for_a_reader_the_board_keeps_out(
    beboer: Resident, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(board_access, "ACCESS_ROLES", (Role.INSPEKTION,))
    party = album()

    html = client_for(beboer).get(album_url(party)).content.decode()

    assert "Del på opslagstavlen" not in html


def test_a_published_video_offers_to_share_it_and_a_draft_does_not(
    beboer: Resident, videos_open: None
) -> None:
    film = make_video(beboer)
    draft = make_video(beboer, title="Kladde", published=False)
    client = client_for(beboer)

    assert (
        f'href="{BOARD}opret?video={film.pk}"' in client.get(f"/intern/gahkflix/v/{film.pk}").content.decode()
    )
    assert "Del på opslagstavlen" not in client.get(f"/intern/gahkflix/v/{draft.pk}").content.decode()
