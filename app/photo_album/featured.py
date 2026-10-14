"""The dashboard's photo strip ("Dagens billeder", "På denne dag") and the album index's timeline mosaic.

All three draw only on what every resident may see — approved, live images whose thumbnail is built —
so the strip needs no per-reader filtering and a pick never points at something a reader cannot open.
"""

import random
from collections import defaultdict
from datetime import date, timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import QuerySet

from .models import DerivativeState, FeaturedMedia, Media, MediaStatus

FEATURED_PER_DAY = 3
# An image featured once sits out this long, so a small archive does not show the same three all week.
FEATURED_COOLDOWN_DAYS = 365
ON_THIS_DAY_LIMIT = 6
# "På denne dag" picks among at most this many of the day's candidates; plenty for six.
_ON_THIS_DAY_POOL = 2000
# The album index's way into the timeline: one large tile and six small ones.
MOSAIC_SIZE = 7
# A fresh draw this often rather than on every load: ORDER BY random() reads every showable row.
_MOSAIC_SECONDS = 10 * 60
_MOSAIC_CACHE_KEY = "photo_album:timeline_mosaic"


def _showable() -> QuerySet[Media]:
    return (
        Media.objects.filter(
            status=MediaStatus.APPROVED,
            deleted_at__isnull=True,
            derivative_state=DerivativeState.READY,
            content_type__startswith="image/",
        )
        .exclude(thumbnail="")
        .select_related("album")
    )


def featured_for(day: date) -> list[Media]:
    """The day's pictures, picked by the first dashboard load of the day and then fixed for everyone.

    A pick that has since been binned or rejected drops out rather than being replaced, so the
    strip can shrink during the day but never changes under somebody who is looking at it.
    """
    if not FeaturedMedia.objects.filter(date=day).exists():
        _pick(day)
    return list(_showable().filter(featured__date=day).order_by("featured__position"))


def _pick(day: date) -> None:
    showable = _showable()
    recent = FeaturedMedia.objects.filter(
        date__gte=day - timedelta(days=FEATURED_COOLDOWN_DAYS), date__lt=day
    ).values("media_id")
    chosen = list(
        showable.exclude(pk__in=recent).order_by("?").values_list("pk", flat=True)[:FEATURED_PER_DAY]
    )
    if len(chosen) < FEATURED_PER_DAY:
        # Fewer fresh images than a day needs: top up with ones still cooling down rather than show fewer.
        chosen += list(
            showable.exclude(pk__in=chosen)
            .order_by("?")
            .values_list("pk", flat=True)[: FEATURED_PER_DAY - len(chosen)]
        )
    if not chosen:
        return
    try:
        with transaction.atomic():
            FeaturedMedia.objects.bulk_create(
                FeaturedMedia(date=day, position=position, media_id=pk) for position, pk in enumerate(chosen)
            )
    except IntegrityError:
        pass  # Another request picked the day first; its choice stands.


def on_this_day(day: date) -> list[Media]:
    """Images captured on this calendar day in earlier years, newest year first.

    One per year before any year gets a second, so a single well-photographed party does not fill
    the row. Seeded by the date: the same six all day, a different six tomorrow.
    """
    candidates = list(
        _showable()
        .filter(
            captured_at__month=day.month,
            captured_at__day=day.day,
            captured_at__year__lt=day.year,
        )
        .order_by("pk")
        .values_list("pk", "captured_at__year")[:_ON_THIS_DAY_POOL]
    )
    by_year: dict[int, list[int]] = defaultdict(list)
    for pk, year in candidates:
        by_year[year].append(pk)
    shuffle = random.Random(day.toordinal())  # noqa: S311 — picking photos, not secrets  # nosec B311
    for pks in by_year.values():
        shuffle.shuffle(pks)
    years = sorted(by_year, reverse=True)
    chosen: list[int] = []
    while len(chosen) < ON_THIS_DAY_LIMIT and any(by_year[year] for year in years):
        for year in years:
            if by_year[year] and len(chosen) < ON_THIS_DAY_LIMIT:
                chosen.append(by_year[year].pop())
    media = _showable().in_bulk(chosen)
    return [media[pk] for pk in chosen]


def timeline_mosaic() -> list[Media]:
    """A handful of random pictures from across the archive, for the timeline's tile on the index.

    Redrawn every few minutes, so coming back to the index shows other pictures. A pick binned since
    the draw drops out rather than leaving a hole that points at nothing.
    """
    chosen = cache.get(_MOSAIC_CACHE_KEY)
    if chosen is None:
        chosen = list(_showable().order_by("?").values_list("pk", flat=True)[:MOSAIC_SIZE])
        cache.set(_MOSAIC_CACHE_KEY, chosen, _MOSAIC_SECONDS)
    media = _showable().in_bulk(chosen)
    return [media[pk] for pk in chosen if pk in media]
