"""The photo album's and GAHKFlix's own widgets on opslagstavlen: a post about an album or a film.

A post can carry an album and a film (models.Notice.album and .video), picked in the composer or
attached by "Del på opslagstavlen" on the album's or the film's page. Each is drawn under the text
the way its own feature draws itself: the album as the photo album's mosaic (the tile its index
leads with), the film as a small GAHKFlix hero, dark, with its banner and a play button. Neither is
a link in the text.

NO ADDRESSES ARE STORED. A post holds the album and the film by key, and every link in a widget is
reversed by URL name as it is drawn, so posts keep pointing at the right pages however the photo
album or GAHKFlix moves them.

EVERY WIDGET IS DRAWN FOR ITS READER, by its feature's rules: an album for anyone who may open the
photo album; a film only for those GAHKFlix's rollout gate lets in, and only while they may see it
(binned after it was attached, it is drawn for the administrators alone, marked as such). The
kitchen account, which can open neither, gets none.

A FIXED NUMBER OF QUERIES PER PAGE: albums and films come with the posts (select_related), the
albums' pictures and counts in two more queries for the whole page, the films' progress in one.

The photo album and GAHKFlix are imported inside the functions, as opslagstavle.forms does with
events: neither depends on the board, and loading the board's models should not depend on them.
"""

import operator
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import reduce
from typing import TYPE_CHECKING

from django.db.models import Count, F, Model, Q, QuerySet, Window
from django.db.models.functions import RowNumber
from django.http import HttpRequest
from django.urls import reverse

from residents.permissions import current_resident, effective_roles, is_kitchen

from .models import Notice

if TYPE_CHECKING:
    from gahkflix.models import Video
    from photo_album.models import Album

ALBUM = "album"
VIDEO = "video"

# The album widget is the photo album index's mosaic: one large picture and six small (three on a
# phone, where the CSS hides the rest).
MOSAIC = 7

# How many albums or films the composer's picker lists at a time.
CHOICES = 20

# The largest key a table holds; a bigger one in a query is an error on Postgres.
_MAX_PK = 2**63 - 1

_EARLIEST = datetime.min.replace(tzinfo=UTC)

# (path, items, of them videos) per album, and (path, sort_at, pk, thumbnail) per picture.
Counts = list[tuple[str, int, int]]
Pictures = list[tuple[str, datetime | None, int, str]]


def counts_label(images: int, videos: int) -> str:
    """ "214 billeder · 3 videoer", or "Ingen billeder endnu"."""
    parts = []
    if images:
        parts.append(f"{images} billede{'r' if images != 1 else ''}")
    if videos:
        parts.append(f"{videos} video{'er' if videos != 1 else ''}")
    return " · ".join(parts) or "Ingen billeder endnu"


@dataclass
class AlbumWidget:
    album: "Album"
    url: str
    # Where it lives: its folder, and the albums above it ("2026 · Roskilde Festival").
    place: str
    pictures: list[str] = field(default_factory=list)
    counts: str = ""


@dataclass
class VideoWidget:
    video: "Video"
    url: str
    play_url: str
    # The banner, else the thumbnail.
    picture: str
    # "S1:A2", the release date and the length, those it has.
    meta: list[str] = field(default_factory=list)
    full_hd: bool = False
    # How far this reader is into it, 0–1, as GAHKFlix's own cards show it.
    progress: float | None = None
    # "I papirkurven": binned after it was attached, and drawn only for those who may still see it.
    state: str = ""

    @property
    def resumes(self) -> bool:
        return self.progress is not None and self.progress < 1


@dataclass
class Choice:
    """One album or film in the composer's picker."""

    pk: int
    title: str
    detail: str
    picture: str = ""


def _pk(value: object) -> int | None:
    if isinstance(value, str):
        value = int(value) if value.isdecimal() and len(value) < 20 else None
    return value if isinstance(value, int) and 0 < value <= _MAX_PK else None


def albums_allowed(request: HttpRequest) -> bool:
    """Whether this reader gets album widgets, and may attach an album."""
    from photo_album import access

    return not is_kitchen(request) and access.roles_allowed(effective_roles(request))


def videos_allowed(request: HttpRequest) -> bool:
    """Whether this reader gets film widgets, and may attach a film: GAHKFlix has its own rollout
    gate, and a widget for a reader it keeps out would be a button that answers 403."""
    from gahkflix import access

    return not is_kitchen(request) and access.request_allowed(request)


def attachable_videos(current: int | None = None) -> "QuerySet[Video]":
    """The films a post may carry: published and not binned, as every reader with access sees them.

    A draft is no news yet, and nobody but its uploader could see it. `current` is the film a post
    being edited already carries, which stays valid if it has been binned since.
    """
    from gahkflix.models import Video

    published = Video.objects.published()
    return published | Video.objects.filter(pk=current) if current else published


# --- the widgets on a page of posts ---------------------------------------------------------------


def attach(request: HttpRequest, notices: Iterable[Notice]) -> None:
    """Give each notice `album_widget` and `video_widget`, as this reader may see them, or None.

    The notices must come with `album`, `video__file` and `video__series` selected (views.
    _list_queryset), or each one costs a query or three.
    """
    notices = list(notices)
    albums = {album.pk: album for notice in notices if notice.album_id and (album := notice.album)}
    videos = {video.pk: video for notice in notices if notice.video_id and (video := notice.video)}
    album_widgets = album_widgets_for(request, albums.values()) if albums else {}
    video_widgets = video_widgets_for(request, videos.values()) if videos else {}
    for notice in notices:
        notice.album_widget = album_widgets.get(notice.album_id or 0)  # type: ignore[attr-defined]
        notice.video_widget = video_widgets.get(notice.video_id or 0)  # type: ignore[attr-defined]


def widgets(
    request: HttpRequest, album: object, video: object
) -> tuple[AlbumWidget | None, VideoWidget | None]:
    """The widgets for an album and a film named by key: the composer's, before there is a post.

    A film must be published; one binned since it was attached still draws for those who may see it.
    """
    from gahkflix.models import Video
    from photo_album.models import Album

    album_widget = video_widget = None
    if (album_pk := _pk(album)) is not None and albums_allowed(request):
        if (found := Album.objects.filter(pk=album_pk).first()) is not None:
            album_widget = album_widgets_for(request, [found]).get(found.pk)
    if (video_pk := _pk(video)) is not None and videos_allowed(request):
        film = (
            Video.objects.filter(pk=video_pk, published_at__isnull=False)
            .select_related("file", "series")
            .first()
        )
        if film is not None:
            video_widget = video_widgets_for(request, [film]).get(film.pk)
    return album_widget, video_widget


def album_widgets_for(request: HttpRequest, albums: Iterable["Album"]) -> dict[int, AlbumWidget]:
    """The newest pictures of each album and everything under it, and how many items it holds.

    What every reader sees (approved and not binned), so the widget is the same for everyone, and
    in the album page's own order. Three queries, however many albums.
    """
    from photo_album.storage import get_photo_album_storage

    albums = list(albums)
    if not albums or not albums_allowed(request):
        return {}
    counts, newest = _contents(albums, MOSAIC)
    names = _ancestor_names(albums)
    storage = get_photo_album_storage()
    return {
        album.pk: AlbumWidget(
            album=album,
            url=reverse("photo_album:detail", args=[album.pk]),
            place=_place(album, names),
            pictures=[storage.url(row[3]) for row in _newest(album, newest, MOSAIC)],
            counts=counts_label(*_tally(album, counts)),
        )
        for album in albums
    }


def _contents(albums: list["Album"], per_album: int) -> tuple[Counts, Pictures]:
    """How many items every album in the subtrees holds, and each one's newest `per_album` built
    pictures. Per album, and added up by `path` prefix afterwards (_tally, _newest): a subtree's
    newest few are among those of its albums, so one query serves every widget on the page."""
    from photo_album.models import DerivativeState, Media, MediaStatus

    shown = Media.objects.filter(
        reduce(operator.or_, (Q(album__path__startswith=album.path) for album in albums)),
        status=MediaStatus.APPROVED,
        deleted_at__isnull=True,
    )
    counts = list(
        shown.order_by()
        .values_list("album__path")
        .annotate(total=Count("pk"), moving=Count("pk", filter=Q(content_type__startswith="video/")))
    )
    newest = list(
        shown.filter(derivative_state=DerivativeState.READY)
        .exclude(thumbnail="")
        .annotate(
            rank=Window(
                RowNumber(),
                partition_by=F("album_id"),
                order_by=[F("sort_at").desc(nulls_last=True), F("pk").desc()],
            )
        )
        .filter(rank__lte=per_album)
        .values_list("album__path", "sort_at", "pk", "thumbnail")
    )
    return counts, newest


def _tally(album: "Album", counts: Counts) -> tuple[int, int]:
    """(pictures, videos) in the album and everything under it."""
    images = videos = 0
    for path, total, moving in counts:
        if path.startswith(album.path):
            images += total - moving
            videos += moving
    return images, videos


def _newest(album: "Album", pictures: Pictures, limit: int) -> Pictures:
    return sorted(
        (row for row in pictures if row[0].startswith(album.path)),
        key=lambda row: (row[1] or _EARLIEST, row[2]),
        reverse=True,
    )[:limit]


def _ancestor_names(albums: list["Album"]) -> dict[int, str]:
    from photo_album.models import Album

    wanted = {pk for album in albums for pk in album.ancestor_ids}
    return dict(Album.objects.filter(pk__in=wanted).values_list("pk", "name")) if wanted else {}


def _place(album: "Album", names: dict[int, str]) -> str:
    parents = " › ".join(names[pk] for pk in album.ancestor_ids if pk in names)
    return " · ".join(filter(None, [album.folder, parents]))


def video_widgets_for(request: HttpRequest, videos: Iterable["Video"]) -> dict[int, VideoWidget]:
    """The films this reader may see, with their pictures and how far the reader is into them.

    The videos must come with `file` and `series` selected. One query, for the progress.
    """
    from gahkflix import access, services
    from gahkflix.templatetags.gahkflix import duration, picture_url

    if not videos_allowed(request):
        return {}
    shown = [video for video in videos if access.can_see(request, video)]
    progress = services.progress_by_video(current_resident(request), shown) if shown else {}
    return {
        video.pk: VideoWidget(
            video=video,
            url=reverse("gahkflix:detail", args=[video.pk]),
            play_url=reverse("gahkflix:play", args=[video.pk]),
            picture=picture_url(video, "banner") or picture_url(video, "thumbnail"),
            meta=list(filter(None, [video.episode_label, video.date_label, duration(video.duration)])),
            full_hd=bool(video.file and video.file.full_hd),
            progress=progress.get(video.pk),
            state="I papirkurven" if video.is_binned else "",
        )
        for video in shown
    }


# --- the composer ---------------------------------------------------------------------------------


def shared(request: HttpRequest) -> dict[str, int]:
    """What "Del på opslagstavlen" attaches (`?album=` or `?video=` on the composer): the form's
    initial values, for what the reader may attach."""
    from photo_album.models import Album

    initial: dict[str, int] = {}
    album = _pk(request.GET.get(ALBUM, ""))
    if album is not None and albums_allowed(request) and Album.objects.filter(pk=album).exists():
        initial[ALBUM] = album
    video = _pk(request.GET.get(VIDEO, ""))
    if video is not None and videos_allowed(request) and attachable_videos().filter(pk=video).exists():
        initial[VIDEO] = video
    return initial


def share_url(obj: Model) -> str:
    """The composer, with an album or a film attached ("Del på opslagstavlen")."""
    kind = {"photo_album.album": ALBUM, "gahkflix.video": VIDEO}[obj._meta.label_lower]
    return f"{reverse('opslagstavle:create')}?{kind}={obj.pk}"


def album_choices(request: HttpRequest, query: str) -> list[Choice]:
    """Albums for the picker: those whose name holds `query`, newest first, each with its cover."""
    from photo_album.models import Album
    from photo_album.storage import get_photo_album_storage

    if not albums_allowed(request):
        return []
    albums = Album.objects.order_by("-created_at", "-pk")
    if query:
        albums = albums.filter(name__icontains=query)
    found = list(albums[:CHOICES])
    if not found:
        return []
    counts, newest = _contents(found, 1)
    names = _ancestor_names(found)
    storage = get_photo_album_storage()
    choices = []
    for album in found:
        cover = _newest(album, newest, 1)
        choices.append(
            Choice(
                pk=album.pk,
                title=album.name,
                detail=f"{_place(album, names)} · {counts_label(*_tally(album, counts))}",
                picture=storage.url(cover[0][3]) if cover else "",
            )
        )
    return choices


def video_choices(request: HttpRequest, query: str) -> list[Choice]:
    """Published films for the picker: those whose title or series holds `query`, newest first."""
    from gahkflix.templatetags.gahkflix import duration, picture_url

    if not videos_allowed(request):
        return []
    videos = attachable_videos().select_related("file", "series").order_by("-published_at", "-pk")
    if query:
        videos = videos.filter(Q(title__icontains=query) | Q(series__title__icontains=query))
    return [
        Choice(
            pk=video.pk,
            title=video.title,
            detail=" · ".join(
                filter(
                    None,
                    [video.series.title if video.series else "", video.date_label, duration(video.duration)],
                )
            ),
            picture=picture_url(video, "thumbnail"),
        )
        for video in videos[:CHOICES]
    ]
