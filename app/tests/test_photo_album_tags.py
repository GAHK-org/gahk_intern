"""Tags on albums and media: who may set them, how albums pass them down, and the timeline filter."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from photo_album.models import Album, Media, MediaStatus, Tag, validate_tag_name
from photo_album.services import search_tags, tag_named
from photo_album.views import PAGE_SIZE
from residents.models import Resident, Role

START = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


def _media(album: Album, resident: Resident, *, at: datetime = START, **fields: object) -> Media:
    defaults: dict[str, object] = {
        "title": "photo",
        "original": "a",
        "high_definition": "b",
        "thumbnail": "c",
        "status": MediaStatus.APPROVED,
        "captured_at": at,
    }
    return Media.objects.create(album=album, requested_by=resident, **{**defaults, **fields})


def _timeline_ids(client: Client, *tags: str) -> list[int]:
    response = client.get(reverse("photo_album:timeline"), {"tag": list(tags)})
    return [item.pk for item in response.context["page"].items]


# --- Names --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Fest", "jul-2024", "a.b_c", "æblegrød", "x" * 30])
def test_valid_tag_names(name: str) -> None:
    validate_tag_name(name)


@pytest.mark.parametrize("name", ["", "to ord", "a/b", "#fest", "fest!", "x" * 31, "tab\there"])
def test_invalid_tag_names(name: str) -> None:
    with pytest.raises(ValidationError):
        validate_tag_name(name)


@pytest.mark.django_db
def test_a_tag_is_one_tag_ignoring_case_and_keeps_its_first_spelling() -> None:
    first = tag_named(" Sommerfest ")
    assert first.name == "Sommerfest"
    assert tag_named("sommerFEST") == first
    assert Tag.objects.count() == 1


# --- Adding and removing ------------------------------------------------------------------------


@pytest.mark.django_db
def test_fotogruppen_tags_and_untags_an_album(client: Client, make_resident: Callable[..., Resident]) -> None:
    client.force_login(make_resident(roles=(Role.FOTO,)))
    album = Album.objects.create(folder="2024", name="Fest")
    url = reverse("photo_album:album_tags", args=[album.pk])

    response = client.post(url, {"action": "add", "name": "sommer"})
    assert response.status_code == 200
    assert [tag["name"] for tag in response.json()["tags"]] == ["sommer"]
    assert list(album.tags.values_list("name", flat=True)) == ["sommer"]

    response = client.post(url, {"action": "remove", "name": "SOMMER"})
    assert response.json()["tags"] == []
    assert not album.tags.exists()


@pytest.mark.django_db
def test_ordinary_residents_cannot_change_tags(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    client.force_login(resident)
    album = Album.objects.create(folder="2024", name="Fest")
    media = _media(album, resident)

    assert client.post(reverse("photo_album:album_tags", args=[album.pk]), {"name": "x"}).status_code == 403
    assert client.post(reverse("photo_album:media_tags", args=[media.pk]), {"name": "x"}).status_code == 403
    assert not Tag.objects.exists()


@pytest.mark.django_db
def test_tagging_ignores_locks(client: Client, make_resident: Callable[..., Resident]) -> None:
    photographer = make_resident(roles=(Role.FOTO,))
    client.force_login(photographer)
    album = Album.objects.create(folder="2024", name="Fest", manually_locked_at=timezone.now())
    media = _media(album, photographer)
    assert album.is_locked()

    assert (
        client.post(reverse("photo_album:album_tags", args=[album.pk]), {"name": "arkiv"}).status_code == 200
    )
    assert (
        client.post(reverse("photo_album:media_tags", args=[media.pk]), {"name": "portræt"}).status_code
        == 200
    )
    assert (
        client.post(
            reverse("photo_album:media_tags", args=[media.pk]), {"action": "remove", "name": "portræt"}
        ).status_code
        == 200
    )
    assert list(album.tags.values_list("name", flat=True)) == ["arkiv"]
    assert not media.tags.exists()


@pytest.mark.django_db
def test_an_invalid_tag_is_refused_with_a_reason(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    client.force_login(make_resident(roles=(Role.FOTO,)))
    album = Album.objects.create(folder="2024", name="Fest")

    response = client.post(reverse("photo_album:album_tags", args=[album.pk]), {"name": "to ord"})

    assert response.status_code == 400
    assert "bogstaver" in response.json()["error"]
    assert not Tag.objects.exists()


@pytest.mark.django_db
def test_tag_search_offers_tags_in_use_prefix_matches_first(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2024", name="Fest")
    media = _media(album, resident)
    album.tags.add(tag_named("julefest"))
    media.tags.add(tag_named("fest"), tag_named("Festival"))
    tag_named("festløs")  # carried by nothing

    assert search_tags("FEST") == ["fest", "Festival", "julefest"]
    assert search_tags("") == ["fest", "Festival", "julefest"]

    client.force_login(resident)
    assert client.get(reverse("photo_album:tag_search"), {"q": "jul"}).json() == {"tags": ["julefest"]}


# --- Showing them -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_viewer_shows_own_and_inherited_tags(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    root = Album.objects.create(folder="2024", name="Fest")
    sub = Album.objects.create(name="Lørdag", parent=root)
    media = _media(sub, resident)
    root.tags.add(tag_named("sommer"))
    media.tags.add(tag_named("dans"))
    url = reverse("photo_album:media_detail", args=[media.pk])

    client.force_login(resident)
    detail = client.get(url).json()
    assert [(tag["name"], tag["inherited"], tag["from"]) for tag in detail["tags"]] == [
        ("dans", False, ""),
        ("sommer", True, "Fest"),
    ]
    assert detail["tags"][0]["url"] == f"{reverse('photo_album:timeline')}?tag=dans"
    assert detail["tagUrl"] == ""

    client.force_login(make_resident(email="foto@gahk.dk", roles=(Role.FOTO,)))
    assert client.get(url).json()["tagUrl"] == reverse("photo_album:media_tags", args=[media.pk])


@pytest.mark.django_db
def test_a_sub_album_page_shows_the_tags_it_inherits(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    root = Album.objects.create(folder="2024", name="Fest")
    sub = Album.objects.create(name="Lørdag", parent=root)
    root.tags.add(tag_named("sommer"))
    client.force_login(make_resident())

    content = client.get(reverse("photo_album:detail", args=[sub.pk])).content.decode()

    assert 'title="Fra albummet Fest"' in content
    assert "data-tag-input" not in content.split("data-album-viewer")[0]


# --- The timeline filter ------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_timeline_filters_on_a_tag_from_the_item_or_any_album_above_it(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    root = Album.objects.create(folder="2024", name="Fest")
    sub = Album.objects.create(name="Lørdag", parent=root)
    other = Album.objects.create(folder="2024", name="Andet album")
    in_root = _media(root, resident, at=START)
    in_sub = _media(sub, resident, at=START + timedelta(days=1))
    tagged_alone = _media(other, resident, at=START + timedelta(days=2))
    untagged = _media(other, resident, at=START + timedelta(days=3))
    sommer = tag_named("sommer")
    root.tags.add(sommer)
    tagged_alone.tags.add(sommer)
    in_sub.tags.add(tag_named("dans"))
    client.force_login(resident)

    assert _timeline_ids(client, "Sommer") == [tagged_alone.pk, in_sub.pk, in_root.pk]
    assert _timeline_ids(client, "sommer", "dans") == [in_sub.pk]
    assert _timeline_ids(client, "ukendt") == []
    assert untagged.pk in _timeline_ids(client)


@pytest.mark.django_db
def test_the_tag_filter_keeps_others_pending_items_hidden(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    other = make_resident(email="other@gahk.dk")
    album = Album.objects.create(folder="2024", name="Fest")
    album.tags.add(tag_named("sommer"))
    mine = _media(album, resident)
    _media(album, other, status=MediaStatus.PENDING)
    client.force_login(resident)

    assert _timeline_ids(client, "sommer") == [mine.pk]


@pytest.mark.django_db
def test_the_tag_filter_survives_paging_and_year_jumps(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2024", name="Fest")
    album.tags.add(tag_named("sommer"))
    for day in range(PAGE_SIZE + 1):
        _media(album, resident, at=START + timedelta(days=day))
    client.force_login(resident)

    response = client.get(reverse("photo_album:timeline"), {"tag": "sommer"})
    content = response.content.decode()

    assert "tag=sommer&amp;after=" in content
    assert "?tag=sommer&amp;year=2024" in content
    assert 'aria-label="Fjern filteret sommer"' in content


# --- Demo seed ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_demo_seed_tags_albums_at_every_level_and_single_media(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    from photo_album import demo
    from photo_album.services import tagged_media

    monkeypatch.setattr(demo, "DIMENSIONS", [(32, 24)])  # the pictures' size is beside the point here
    resident = make_resident(email="seed@gahk.dk")
    demo.seed([resident], timezone.now(), None)  # type: ignore[arg-type]

    for (folder, path), names in demo.ALBUM_TAGS.items():
        album = demo._album(folder, path)
        assert sorted(album.tags.values_list("name", flat=True)) == sorted(names)
    assert Media.objects.filter(tags__isnull=False).distinct().count() == len(demo.MEDIA_TAGS)
    assert max(media.tags.count() for media in Media.objects.filter(tags__isnull=False)) >= 2

    # "fodbold" sits on Sport/Fodbold, so it reaches the Pokalfinale album below it too.
    fodbold = tagged_media(Media.objects.all(), Tag.objects.get(name="fodbold"))
    assert set(fodbold.values_list("album__name", flat=True)) == {"Fodbold", "Pokalfinale"}

    # A re-seed adds nothing new, but tags media an older seed made before tags existed.
    Media.tags.through.objects.all().delete()
    demo.seed([resident], timezone.now(), None)  # type: ignore[arg-type]
    assert Media.objects.filter(tags__isnull=False).distinct().count() == len(demo.MEDIA_TAGS)
