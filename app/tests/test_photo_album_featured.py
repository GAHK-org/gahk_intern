"""The dashboard's photo strip (Dagens billeder, På denne dag) and the index's timeline mosaic (photo_album.featured)."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from photo_album.featured import (
    FEATURED_PER_DAY,
    MOSAIC_SIZE,
    ON_THIS_DAY_LIMIT,
    featured_for,
    on_this_day,
    timeline_mosaic,
)
from photo_album.models import Album, DerivativeState, FeaturedMedia, Media, MediaStatus
from residents.models import Resident

DAY = date(2026, 10, 13)


def _image(album: Album, resident: Resident, **fields: object) -> Media:
    defaults: dict[str, object] = {
        "title": "photo",
        "original": "a",
        "high_definition": "b",
        "thumbnail": "c",
        "content_type": "image/jpeg",
        "status": MediaStatus.APPROVED,
    }
    return Media.objects.create(album=album, requested_by=resident, **{**defaults, **fields})


@pytest.fixture
def album() -> Album:
    return Album.objects.create(folder="2026", name="Sommerfest")


@pytest.mark.django_db
def test_the_day_is_picked_once_and_then_fixed(album: Album, make_resident: Callable[..., Resident]) -> None:
    resident = make_resident()
    for _ in range(10):
        _image(album, resident)

    first = featured_for(DAY)
    assert len(first) == FEATURED_PER_DAY
    assert featured_for(DAY) == first
    assert FeaturedMedia.objects.filter(date=DAY).count() == FEATURED_PER_DAY


@pytest.mark.django_db
def test_only_what_everyone_may_see_is_picked(album: Album, make_resident: Callable[..., Resident]) -> None:
    resident = make_resident()
    shown = _image(album, resident)
    _image(album, resident, status=MediaStatus.PENDING)
    _image(album, resident, deleted_at=datetime(2026, 1, 1, tzinfo=UTC))
    _image(album, resident, content_type="video/mp4")
    _image(album, resident, derivative_state=DerivativeState.PENDING)
    _image(album, resident, thumbnail="")

    assert featured_for(DAY) == [shown]


@pytest.mark.django_db
def test_a_pick_binned_later_drops_out_without_a_replacement(
    album: Album, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    for _ in range(6):
        _image(album, resident)
    picked = featured_for(DAY)
    Media.objects.filter(pk=picked[0].pk).update(deleted_at=datetime(2026, 10, 13, tzinfo=UTC))

    assert featured_for(DAY) == picked[1:]


@pytest.mark.django_db
def test_recent_picks_sit_out_but_top_up_a_small_archive(
    album: Album, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    items = [_image(album, resident) for _ in range(5)]
    for position, item in enumerate(items[:3]):
        FeaturedMedia.objects.create(date=DAY - timedelta(days=1), position=position, media=item)

    picked = {item.pk for item in featured_for(DAY)}
    assert len(picked) == FEATURED_PER_DAY
    assert {items[3].pk, items[4].pk} <= picked  # the two fresh ones first, then one cooling down


@pytest.mark.django_db
def test_on_this_day_spreads_over_earlier_years(album: Album, make_resident: Callable[..., Resident]) -> None:
    resident = make_resident()
    for year in (2023, 2024, 2025):
        for hour in range(4):
            _image(album, resident, captured_at=datetime(year, 10, 13, 10 + hour, tzinfo=UTC))
    _image(album, resident, captured_at=datetime(2026, 10, 13, 10, tzinfo=UTC))  # this year: not "earlier"
    _image(album, resident, captured_at=datetime(2025, 10, 14, 10, tzinfo=UTC))  # another day

    earlier = on_this_day(DAY)
    years = [item.captured_at.year for item in earlier if item.captured_at]
    assert len(earlier) == ON_THIS_DAY_LIMIT
    assert years == [2025, 2024, 2023, 2025, 2024, 2023]
    assert on_this_day(DAY) == earlier  # stable through the day


@pytest.mark.django_db
def test_on_this_day_drops_a_pick_binned_while_it_was_drawn(
    album: Album, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    """It used to raise KeyError, and the dashboard with it."""
    from photo_album import featured

    resident = make_resident()
    kept = _image(album, resident, captured_at=datetime(2025, 10, 13, 10, tzinfo=UTC))
    binned = _image(album, resident, captured_at=datetime(2024, 10, 13, 10, tzinfo=UTC))
    showable = featured._showable
    calls = 0

    def binned_after_the_draw() -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            Media.objects.filter(pk=binned.pk).update(deleted_at=datetime(2026, 10, 13, tzinfo=UTC))
        return showable()

    monkeypatch.setattr(featured, "_showable", binned_after_the_draw)

    assert on_this_day(DAY) == [kept]


@pytest.mark.django_db
def test_dashboard_links_each_picture_to_its_album(
    client: Client, album: Album, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pinned: on any other day the picture below is not "on this day".
    monkeypatch.setattr("photo_album.templatetags.photo_album_extras.current_date", lambda: DAY)
    resident = make_resident()
    item = _image(album, resident, captured_at=datetime(2024, 10, 13, 10, tzinfo=UTC))
    client.force_login(resident)

    page = client.get("/intern/").content.decode()
    link = f"{reverse('photo_album:detail', args=[album.pk])}?media={item.pk}"
    assert "Dagens billeder" in page and "På denne dag" in page
    assert page.count(link) == 2  # featured, and on this day


@pytest.mark.django_db
def test_dashboard_strip_follows_the_album_rollout_gate(
    client: Client, album: Album, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-gating the album (photo_album.access.roles_allowed) must hide its photos from the
    dashboard as well, not only the sidebar link — the strip used to render for everyone."""
    monkeypatch.setattr("photo_album.templatetags.photo_album_extras.current_date", lambda: DAY)
    resident = make_resident()
    _image(album, resident, captured_at=datetime(2024, 10, 13, 10, tzinfo=UTC))
    client.force_login(resident)
    assert "Dagens billeder" in client.get("/intern/").content.decode()

    monkeypatch.setattr("photo_album.access.roles_allowed", lambda _roles: False)

    assert "Dagens billeder" not in client.get("/intern/").content.decode()


@pytest.mark.django_db
def test_dashboard_has_no_strip_without_photos(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    client.force_login(make_resident())
    assert "Dagens billeder" not in client.get("/intern/").content.decode()


@pytest.fixture
def fresh_mosaic() -> None:
    cache.clear()


@pytest.mark.django_db
@pytest.mark.usefixtures("fresh_mosaic")
def test_the_mosaic_draws_only_what_everyone_may_see(
    album: Album, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    shown = _image(album, resident)
    _image(album, resident, status=MediaStatus.PENDING)
    _image(album, resident, deleted_at=datetime(2026, 1, 1, tzinfo=UTC))
    _image(album, resident, content_type="video/mp4")
    _image(album, resident, derivative_state=DerivativeState.PENDING)

    assert timeline_mosaic() == [shown]


@pytest.mark.django_db
@pytest.mark.usefixtures("fresh_mosaic")
def test_the_mosaic_holds_its_draw_and_drops_a_binned_pick(
    album: Album, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    for _ in range(MOSAIC_SIZE + 3):
        _image(album, resident)
    drawn = timeline_mosaic()
    assert len(drawn) == MOSAIC_SIZE
    assert timeline_mosaic() == drawn

    Media.objects.filter(pk=drawn[0].pk).update(deleted_at=datetime(2026, 10, 13, tzinfo=UTC))
    assert timeline_mosaic() == drawn[1:]


@pytest.mark.django_db
@pytest.mark.usefixtures("fresh_mosaic")
def test_the_index_leads_with_the_timeline_mosaic(
    client: Client, album: Album, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    item = _image(album, resident)
    client.force_login(resident)

    page = client.get(reverse("photo_album:index")).content.decode()
    assert f'class="album-timeline-card" href="{reverse("photo_album:timeline")}"' in page
    assert item.thumbnail.url in page

    in_folder = client.get(reverse("photo_album:index"), {"folder": "2026"}).content.decode()
    assert "album-timeline-card" not in in_folder
    assert f'href="{reverse("photo_album:timeline")}"' in in_folder


@pytest.mark.django_db
@pytest.mark.usefixtures("fresh_mosaic")
def test_the_timeline_card_shows_without_photos(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    client.force_login(make_resident())
    page = client.get(reverse("photo_album:index")).content.decode()
    assert (
        "album-timeline-card" in page
        and "<img" not in page.split("album-timeline-mosaic")[1].split("</span>")[0]
    )
