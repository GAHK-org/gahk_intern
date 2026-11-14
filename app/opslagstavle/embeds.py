"""The photo album's and GAHKFlix's own widgets in a post on opslagstavlen (core.embeds).

A post embeds an album or a film in its body, `![Julefrokost 2026](album:12)`, picked in the composer
or written there by "Del på opslagstavlen" on the album's or the film's page. Each is drawn where it
stands the way its own feature draws itself: the album as the photo album's mosaic (the tile its
index leads with), the film as a small GAHKFlix hero, dark, with its banner and a play button.

ANOTHER KIND OF WIDGET is an entry in KINDS below (how to find what it shows, which template draws
it, what the composer's picker lists) and its name and icon in core.embeds.KINDS. Nothing about a
post's schema changes.

AN ALBUM'S PICTURES are its newest, or those the author picked (core.embeds: `album:12/88,91,95`),
in `Vælg billeder` on its widget in the composer. A pick that is gone, unapproved, binned or not in
the album is passed over, and with none left the album shows its newest again.

EVERY WIDGET IS DRAWN FOR ITS READER, by its feature's rules: an album for anyone who may open the
photo album; a film only for those GAHKFlix's rollout gate lets in, and only while they may see it
(a draft for its uploader and the administrators, a binned one for the administrators, each marked
as such). The kitchen account, which can open neither, gets none. Where no widget is drawn, the
name the author embedded it under stands in its place (core.markdown).

A FIXED NUMBER OF QUERIES PER PAGE, however many posts embed however many albums and films: the
albums, their pictures, their counts and the picked pictures in four queries for the whole page, the
films and the reader's progress in two.

The photo album and GAHKFlix are imported inside the functions, as opslagstavle.forms does with
events: neither depends on the board, and loading the board's models should not depend on them.
"""

import operator
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import reduce
from typing import TYPE_CHECKING

from django.db.models import Count, F, Model, Q, Window
from django.db.models.functions import RowNumber
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.safestring import SafeString

from core import embeds as embed_syntax
from core.embeds import Embed
from core.markdown import embed_references
from residents.permissions import current_resident, effective_roles, is_kitchen

from .models import Notice

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from gahkflix.models import Video
    from photo_album.models import Album, Media

# The album widget is the photo album index's mosaic: one large picture and six small (on a phone
# one large and two small, where the CSS hides the rest). As many as an author may pick.
MOSAIC = embed_syntax.FEATURING["album"]

# How many of an album's pictures `Vælg billeder` lists at a time.
PICTURES_PAGE = 48

# How many albums or films the composer's picker lists at a time.
CHOICES = 20

# The most widgets one post draws. Past it, the names stand in: a body is 8,000 characters, and a
# crafted one should not be able to make a page draw hundreds.
MAX_PER_POST = 10

_EARLIEST = datetime.min.replace(tzinfo=UTC)

# (path, items, of them videos) per album, and (path, sort_at, pk, thumbnail) per picture.
Counts = list[tuple[str, int, int]]
Row = tuple[str, datetime | None, int, str]
Pictures = list[Row]


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
    # "Kladde" or "I papirkurven", for the few who may see a film in either.
    state: str = ""

    @property
    def resumes(self) -> bool:
        return self.progress is not None and self.progress < 1


@dataclass
class Choice:
    """One album or film in the composer's picker, with the Markdown that embeds it."""

    pk: int
    title: str
    detail: str
    markdown: str
    picture: str = ""


@dataclass
class Drawn:
    """A widget under the composer's text (_embedded.html)."""

    reference: str
    html: SafeString
    # `Vælg billeder`: the picker of what it shows, with what is picked now. "" for a film.
    featuring_url: str = ""


@dataclass
class Picture:
    """One of an album's pictures in `Vælg billeder`."""

    pk: int
    url: str
    name: str
    video: bool


@dataclass(frozen=True)
class Kind:
    """One kind of widget: what core.embeds calls it, and everything the board needs to draw it."""

    name: str
    template: str
    # Whether this reader gets these widgets at all, and may embed one.
    allowed: Callable[[HttpRequest], bool]
    # The widgets for these embeds that this reader may see, by embed.
    widgets: Callable[[HttpRequest, set[Embed]], dict[Embed, object]]
    # The picker's list: the newest, or those matching what is typed.
    choices: Callable[[HttpRequest, str], list[Choice]]
    # The name "Del på opslagstavlen" embeds it under, or None for what may not be shared.
    shareable: Callable[[HttpRequest, int], str | None]
    # The URL name of the composer's picker of what the widget shows (core.embeds.FEATURING), if any.
    featuring: str = ""


def counts_label(images: int, videos: int) -> str:
    """ "214 billeder · 3 videoer", or "Ingen billeder endnu"."""
    parts = []
    if images:
        parts.append(f"{images} billede{'r' if images != 1 else ''}")
    if videos:
        parts.append(f"{videos} video{'er' if videos != 1 else ''}")
    return " · ".join(parts) or "Ingen billeder endnu"


# --- drawing them ---------------------------------------------------------------------------------


def attach(request: HttpRequest, notices: Iterable[Notice]) -> None:
    """Give each notice `embeds` (reference → widget HTML, for core.markdown) and `first_embed` (the
    feed's one widget under its excerpt), as this reader may see them."""
    notices = list(notices)
    wanted = {notice.pk: _references(notice.body) for notice in notices}
    drawn = rendered(request, {reference for references in wanted.values() for reference in references})
    for notice in notices:
        mine = {reference: drawn[reference] for reference in wanted[notice.pk] if reference in drawn}
        notice.embeds = mine  # type: ignore[attr-defined]
        notice.first_embed = next(iter(mine.values()), "")  # type: ignore[attr-defined]


def for_body(request: HttpRequest, body: str, *, compose: bool = False) -> list[Drawn]:
    """The widgets `body` embeds, in order, as this reader would see them."""
    references = _references(body)
    drawn = rendered(request, references, compose=compose)
    out = []
    for reference in references:
        embed = embed_syntax.parse(reference)
        if embed is None or reference not in drawn:
            continue
        featuring = KINDS[embed.kind].featuring
        url = ""
        if featuring:
            url = reverse(featuring, args=[embed.key])
            if embed.featured:
                url += f"?valgt={','.join(map(str, embed.featured))}"
        out.append(Drawn(reference, drawn[reference], url))
    return out


def rendered(
    request: HttpRequest, references: Iterable[str], *, compose: bool = False
) -> dict[str, SafeString]:
    """Each embed's widget, drawn for this reader, by reference; those they may not see are left out.

    `compose` is for the composer: there a widget's links open a new tab, so following one does not
    lose the draft.
    """
    # Two sources can name one embed (`album:12/88,88` and `album:12/88`): drawn once, under each.
    sources: dict[Embed, list[str]] = defaultdict(list)
    for reference in references:
        if (embed := embed_syntax.parse(reference)) is not None:
            sources[embed].append(reference)
    out: dict[str, SafeString] = {}
    for name, kind in KINDS.items():
        wanted = {embed for embed in sources if embed.kind == name}
        if not wanted or not kind.allowed(request):
            continue
        for embed, widget in kind.widgets(request, wanted).items():
            html = render_to_string(kind.template, {"widget": widget, "compose": compose})
            for reference in sources[embed]:
                out[reference] = html
    return out


def _references(body: str) -> list[str]:
    """What `body` embeds, each once, in order, at most MAX_PER_POST."""
    if not any(f"]({name}:" in body for name in embed_syntax.KINDS):
        return []  # most posts embed nothing, and parsing one to learn that is the whole cost
    return list(dict.fromkeys(embed_references(body)))[:MAX_PER_POST]


# --- the composer ---------------------------------------------------------------------------------


def shared_body(request: HttpRequest) -> str:
    """What "Del på opslagstavlen" starts a post with (`?album=` or `?gahkflix=` on the composer): the
    embed, below an empty line for the text. "" when there is nothing the reader may share."""
    for name, kind in KINDS.items():
        key = _key(name, request.GET.get(name, ""))
        if key is None or not kind.allowed(request):
            continue
        if (title := kind.shareable(request, key)) is not None:
            return f"\n\n{embed_syntax.markdown(name, key, title)}"
    return ""


def share_url(obj: Model) -> str:
    """The composer, with an album or a film embedded ("Del på opslagstavlen")."""
    name = {"photo_album.album": "album", "gahkflix.video": "gahkflix"}[obj._meta.label_lower]
    return f"{reverse('opslagstavle:create')}?{name}={obj.pk}"


def _key(name: str, value: str) -> int | None:
    """A key from the querystring, read the way an embed's is: digits, and no bigger than a row's."""
    embed = embed_syntax.parse(f"{name}:{value}")
    return embed.key if embed is not None and not embed.featured else None


# --- the photo album ------------------------------------------------------------------------------


def albums_allowed(request: HttpRequest) -> bool:
    from photo_album import access

    return not is_kitchen(request) and access.roles_allowed(effective_roles(request))


def _album_widgets(request: HttpRequest, wanted: set[Embed]) -> dict[Embed, object]:
    """Each album's picked pictures, else the newest of it and everything under it, and how many
    items it holds.

    What every reader sees (approved and not binned), so the widget is the same for everyone, in
    the album page's own order. Four queries, however many albums: three without picks.
    """
    from photo_album.models import Album
    from photo_album.storage import get_photo_album_storage

    albums = {album.pk: album for album in Album.objects.filter(pk__in={embed.key for embed in wanted})}
    if not albums:
        return {}
    counts, newest = _contents(list(albums.values()), MOSAIC)
    picked = _shown_pictures(
        list(albums.values()), {pk for embed in wanted if embed.key in albums for pk in embed.featured}
    )
    names = _ancestor_names(list(albums.values()))
    storage = get_photo_album_storage()
    widgets: dict[Embed, object] = {}
    for embed in wanted:
        if (album := albums.get(embed.key)) is None:
            continue
        mine = [picked[pk] for pk in embed.featured if pk in picked and picked[pk][0].startswith(album.path)]
        widgets[embed] = AlbumWidget(
            album=album,
            url=reverse("photo_album:detail", args=[album.pk]),
            place=_place(album, names),
            pictures=[storage.url(row[3]) for row in mine or _newest(album, newest, MOSAIC)],
            counts=counts_label(*_tally(album, counts)),
        )
    return widgets


def _shown(albums: list["Album"]) -> "QuerySet[Media]":
    """What every reader sees of these albums and everything under them: approved, not binned."""
    from photo_album.models import Media, MediaStatus

    return Media.objects.filter(
        reduce(operator.or_, (Q(album__path__startswith=album.path) for album in albums)),
        status=MediaStatus.APPROVED,
        deleted_at__isnull=True,
    )


def _with_pictures(media: "QuerySet[Media]") -> "QuerySet[Media]":
    """Those with a thumbnail to show: built, and not empty."""
    from photo_album.models import DerivativeState

    return media.filter(derivative_state=DerivativeState.READY).exclude(thumbnail="")


def _shown_pictures(albums: list["Album"], pks: set[int]) -> dict[int, Row]:
    """Of `pks`, those a widget for one of `albums` may show, by pk. One query, none without any."""
    if not pks:
        return {}
    rows = _with_pictures(_shown(albums).filter(pk__in=pks)).values_list(
        "album__path", "sort_at", "pk", "thumbnail"
    )
    return {row[2]: row for row in rows}


def _contents(albums: list["Album"], per_album: int) -> tuple[Counts, Pictures]:
    """How many items every album in the subtrees holds, and each one's newest `per_album` built
    pictures. Per album, and added up by `path` prefix afterwards (_tally, _newest): a subtree's
    newest few are among those of its albums, so one query serves every widget on the page."""
    shown = _shown(albums)
    counts = list(
        shown.order_by()
        .values_list("album__path")
        .annotate(total=Count("pk"), moving=Count("pk", filter=Q(content_type__startswith="video/")))
    )
    newest = list(
        _with_pictures(shown)
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


def _album_choices(request: HttpRequest, query: str) -> list[Choice]:
    """Albums whose name holds `query`, newest first, each with its cover."""
    from photo_album.models import Album
    from photo_album.storage import get_photo_album_storage

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
                markdown=embed_syntax.markdown("album", album.pk, album.name),
                picture=storage.url(cover[0][3]) if cover else "",
            )
        )
    return choices


def _album_shareable(request: HttpRequest, key: int) -> str | None:
    from photo_album.models import Album

    return Album.objects.filter(pk=key).values_list("name", flat=True).first()


def album_featuring(key: int, page: int, picked: str) -> dict[str, object] | None:
    """What `Vælg billeder` on an album's widget shows (views.embed_album_pictures), or None for an
    album that is gone.

    Every picture the widget may show, a page at a time and newest first, as the widget picks them:
    the first seven are what it shows without picks. On the first page, also those picked now
    (`picked`, as the embed holds them: "88,91,95"), in their order, while it may still show them.
    """
    from photo_album.models import Album

    album = Album.objects.filter(pk=key).first()
    if album is None:
        return None
    pictures = _with_pictures(_shown([album]))
    rows = list(
        pictures.order_by(F("sort_at").desc(nulls_last=True), "-pk")[
            (page - 1) * PICTURES_PAGE : page * PICTURES_PAGE + 1
        ]
    )
    context: dict[str, object] = {
        "album": album,
        "pictures": _pictures(rows[:PICTURES_PAGE]),
        "next_page": page + 1 if len(rows) > PICTURES_PAGE else None,
        "mosaic": MOSAIC,
    }
    if page == 1:
        embed = embed_syntax.parse(f"album:{key}/{picked}") if picked else None
        wanted = embed.featured if embed is not None else ()
        by_pk = {media.pk: media for media in pictures.filter(pk__in=wanted)} if wanted else {}
        context["picked"] = _pictures([by_pk[pk] for pk in wanted if pk in by_pk])
    return context


def _pictures(media: Iterable["Media"]) -> list[Picture]:
    from photo_album.storage import get_photo_album_storage

    storage = get_photo_album_storage()
    return [
        Picture(
            pk=each.pk,
            url=storage.url(each.thumbnail.name),
            name=each.display_name,
            video=each.content_type.startswith("video/"),
        )
        for each in media
    ]


# --- GAHKFlix -------------------------------------------------------------------------------------


def videos_allowed(request: HttpRequest) -> bool:
    """GAHKFlix has its own rollout gate, and a widget for a reader it keeps out would be a play
    button that answers 403."""
    from gahkflix import access

    return not is_kitchen(request) and access.request_allowed(request)


def _video_widgets(request: HttpRequest, wanted: set[Embed]) -> dict[Embed, object]:
    """The films this reader may see, with their pictures and how far they are into them. Two
    queries: the films, and the progress."""
    from gahkflix import access, services
    from gahkflix.models import Video
    from gahkflix.templatetags.gahkflix import duration, picture_url

    shown = [
        video
        for video in Video.objects.filter(pk__in={embed.key for embed in wanted}).select_related(
            "file", "series"
        )
        if access.can_see(request, video)
    ]
    progress = services.progress_by_video(current_resident(request), shown) if shown else {}
    widgets = {
        video.pk: VideoWidget(
            video=video,
            url=reverse("gahkflix:detail", args=[video.pk]),
            play_url=reverse("gahkflix:play", args=[video.pk]),
            picture=picture_url(video, "banner") or picture_url(video, "thumbnail"),
            meta=list(filter(None, [video.episode_label, video.date_label, duration(video.duration)])),
            full_hd=bool(video.file and video.file.full_hd),
            progress=progress.get(video.pk),
            state="I papirkurven" if video.is_binned else "Kladde" if video.is_draft else "",
        )
        for video in shown
    }
    return {embed: widgets[embed.key] for embed in wanted if embed.key in widgets}


def _video_choices(request: HttpRequest, query: str) -> list[Choice]:
    """Published films whose title or series holds `query`, newest first."""
    from gahkflix.models import Video
    from gahkflix.templatetags.gahkflix import duration, picture_url

    videos = Video.objects.published().select_related("file", "series").order_by("-published_at", "-pk")
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
            markdown=embed_syntax.markdown("gahkflix", video.pk, video.title),
            picture=picture_url(video, "thumbnail"),
        )
        for video in videos[:CHOICES]
    ]


def _video_shareable(request: HttpRequest, key: int) -> str | None:
    """Only a published film: a draft or a binned one is no news to share."""
    from gahkflix.models import Video

    return Video.objects.published().filter(pk=key).values_list("title", flat=True).first()


KINDS: dict[str, Kind] = {
    "album": Kind(
        name="album",
        template="opslagstavle/_album_widget.html",
        allowed=albums_allowed,
        widgets=_album_widgets,
        choices=_album_choices,
        shareable=_album_shareable,
        featuring="opslagstavle:embed_album_pictures",
    ),
    "gahkflix": Kind(
        name="gahkflix",
        template="opslagstavle/_gahkflix_widget.html",
        allowed=videos_allowed,
        widgets=_video_widgets,
        choices=_video_choices,
        shareable=_video_shareable,
    ),
}
