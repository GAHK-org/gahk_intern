"""Cards for the photo albums and GAHKFlix videos a post links to.

A post about a new album or film shows it under the text: a few of the album's pictures, or the
film's thumbnail, with its title. Like a link preview on the Facebook group this board replaced, and
like that, it comes from the LINK rather than from a field on the post. A link is what the author
already has (the address bar, or "Del på opslagstavlen", which writes one into the composer), a post
can link to more than one thing, and posts written before this existed get their cards too.

ONLY OUR OWN ADDRESSES. A link counts when it is relative or names this site's host, and its path
resolves to an album's or a video's page. The card is built from the album or the video itself,
never from the link's text, so a link cannot make a card claim anything.

EVERY CARD IS DRAWN FOR ITS READER, by the feature's own rules: an album for anyone who may open the
photo album, a video for those inside GAHKFlix's rollout gate who may see that video (so a draft only
for its uploader and the administrators, and marked as one). The kitchen account gets none: it can
open neither feature. A link the reader cannot follow stays a plain link.

A FIXED NUMBER OF QUERIES PER PAGE, however many posts link to however many albums and videos: the
board draws twenty posts at a time.
"""

import operator
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import reduce
from urllib.parse import urlsplit

from django.conf import settings
from django.db.models import Count, F, Model, Q, Window
from django.db.models.functions import RowNumber
from django.http import HttpRequest
from django.http.request import split_domain_port
from django.urls import Resolver404, resolve, reverse

from core.markdown import link_hrefs
from residents.permissions import effective_roles, is_kitchen

from .models import Notice

ALBUM = "album"
VIDEO = "video"

# The pages a card is drawn for, by URL name. The player is a film's address as much as its page is.
_PAGES = {"photo_album:detail": ALBUM, "gahkflix:detail": VIDEO, "gahkflix:play": VIDEO}

# The most cards one post shows, on its own page. The feed shows the first (_notice_card.html).
MAX_PREVIEWS = 4

# An album's card shows this many of its pictures, in a row.
ALBUM_PICTURES = 4

# The largest key a table holds. A link's id comes straight out of the URL, and a bigger one in a
# query is an error on Postgres: one post would take the whole board down.
_MAX_PK = 2**63 - 1

_EARLIEST = datetime.min.replace(tzinfo=UTC)

# Backslashed in a title written into Markdown link text, so "[Revy] 2009" stays one link.
_MARKDOWN_PUNCTUATION = re.compile(r"([\\`*_\[\]<>])")


@dataclass
class Preview:
    kind: str
    url: str
    title: str
    # Where it lives, above the title: "Fotoalbum · 2026" or "GAHKFlix · Revyen".
    kicker: str
    # Under the title, joined with " · ".
    details: list[str] = field(default_factory=list)
    pictures: list[str] = field(default_factory=list)
    # "Kladde" or "I papirkurven": a video only its uploader and the administrators see.
    state: str = ""
    # A video's running time, shown on its picture.
    duration: str = ""


Target = tuple[str, int]


def _bare(host: str) -> str:
    return host.lower().removeprefix("www.")


def _our_hosts(request: HttpRequest) -> set[str]:
    """The host names a link may carry and still be ours: this request's, the site's, and each exact
    ALLOWED_HOSTS entry ("*" and ".domain" also match hosts that are not ours)."""
    hosts = [split_domain_port(request.get_host())[0], urlsplit(settings.SITE_URL).hostname or ""]
    hosts += [host for host in settings.ALLOWED_HOSTS if host != "*" and not host.startswith(".")]
    return {_bare(host) for host in hosts if host}


def _pk(value: object) -> int | None:
    if isinstance(value, str):
        value = int(value) if value.isdecimal() and len(value) < 20 else None
    return value if isinstance(value, int) and 0 < value <= _MAX_PK else None


def target_of(href: str, hosts: set[str]) -> Target | None:
    """The album or video `href` points at, if it is one of ours."""
    try:
        parts = urlsplit(href)
        hostname = parts.hostname
    except ValueError:
        return None
    if parts.scheme or parts.netloc:
        if parts.scheme not in ("http", "https") or _bare(hostname or "") not in hosts:
            return None
    elif not parts.path.startswith("/"):
        return None
    try:
        match = resolve(parts.path)
    except Resolver404:
        return None
    kind = _PAGES.get(match.view_name)
    pk = _pk(match.kwargs.get("pk"))
    return (kind, pk) if kind is not None and pk is not None else None


def targets(body: str, hosts: set[str]) -> list[Target]:
    """The albums and videos `body` links to, in order and each once: at most MAX_PREVIEWS."""
    # Most posts link to neither, and parsing the body only to learn that is the whole cost.
    if not any(prefix in body for prefix in (reverse("photo_album:index"), reverse("gahkflix:home"))):
        return []
    found: list[Target] = []
    for href in link_hrefs(body):
        target = target_of(href, hosts)
        if target is not None and target not in found:
            found.append(target)
            if len(found) == MAX_PREVIEWS:
                break
    return found


def attach(request: HttpRequest, notices: Iterable[Notice]) -> None:
    """Give each notice `previews`: the cards for what it links to, as this reader may see them."""
    notices = list(notices)
    hosts = _our_hosts(request)
    wanted = {notice.pk: targets(notice.body, hosts) for notice in notices}
    cards = _load(request, {target for found in wanted.values() for target in found})
    for notice in notices:
        notice.previews = [cards[target] for target in wanted[notice.pk] if target in cards]  # type: ignore[attr-defined]


def for_body(request: HttpRequest, body: str) -> list[Preview]:
    """The cards `body` would show this reader: the compose form's, before there is a post."""
    found = targets(body, _our_hosts(request))
    cards = _load(request, found)
    return [cards[target] for target in found if target in cards]


def share_link(request: HttpRequest) -> str:
    """What the composer starts with when "Del på opslagstavlen" opened it (`?album=` or `?video=`):
    a Markdown link named after the album or the film. "" when there is nothing to share.

    Only what every reader can see: a draft or a binned video is not news to share yet.
    """
    for kind in (ALBUM, VIDEO):
        pk = _pk(request.GET.get(kind, ""))
        if pk is None:
            continue
        card = _load(request, [(kind, pk)]).get((kind, pk))
        if card is not None and not card.state:
            title = _MARKDOWN_PUNCTUATION.sub(r"\\\1", card.title)
            return f"[{title}]({card.url})"
    return ""


def share_url(obj: Model) -> str:
    """The composer, opened to share an album or a video ("Del på opslagstavlen")."""
    kind = {"photo_album.album": ALBUM, "gahkflix.video": VIDEO}[obj._meta.label_lower]
    return f"{reverse('opslagstavle:create')}?{kind}={obj.pk}"


def _load(request: HttpRequest, wanted: Iterable[Target]) -> dict[Target, Preview]:
    """The cards for `wanted` that this reader may see."""
    if is_kitchen(request):  # it can open neither feature (core.middleware.KitchenAccountGate)
        return {}
    pks: dict[str, set[int]] = defaultdict(set)
    for kind, pk in wanted:
        pks[kind].add(pk)
    cards: dict[Target, Preview] = {}
    if pks[ALBUM]:
        cards.update(((ALBUM, pk), card) for pk, card in _albums(request, pks[ALBUM]).items())
    if pks[VIDEO]:
        cards.update(((VIDEO, pk), card) for pk, card in _videos(request, pks[VIDEO]).items())
    return cards


def _albums(request: HttpRequest, pks: set[int]) -> dict[int, Preview]:
    """The newest few pictures of each album and everything under it, and how many items there are.

    The same items the album's own card on the photo album's index counts (photo_album.views.
    _summarise): approved and not binned, so every reader sees the same card. Four queries, however
    many albums.

    Imported here rather than at the top, as opslagstavle.forms does with events: the photo album
    does not depend on the board, and neither should loading the board's models depend on it.
    """
    from photo_album import access
    from photo_album.models import Album, DerivativeState, Media, MediaStatus
    from photo_album.storage import get_photo_album_storage

    if not access.roles_allowed(effective_roles(request)):
        return {}
    albums = list(Album.objects.filter(pk__in=pks))
    if not albums:
        return {}
    shown = Media.objects.filter(
        reduce(operator.or_, (Q(album__path__startswith=album.path) for album in albums)),
        status=MediaStatus.APPROVED,
        deleted_at__isnull=True,
    )
    # Per album in the subtrees, added up below by `path` prefix.
    counts = list(shown.order_by().values_list("album__path").annotate(n=Count("pk")))
    # The newest few of every album in the subtrees, in the album page's order. A subtree's newest
    # few are among those of its albums, so one query serves every card.
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
        .filter(rank__lte=ALBUM_PICTURES)
        .values_list("album__path", "sort_at", "pk", "thumbnail")
    )
    names = dict(
        Album.objects.filter(pk__in={pk for album in albums for pk in album.ancestor_ids}).values_list(
            "pk", "name"
        )
    )
    storage = get_photo_album_storage()
    cards: dict[int, Preview] = {}
    for album in albums:
        count = sum(n for path, n in counts if path.startswith(album.path))
        pictures = sorted(
            (row for row in newest if row[0].startswith(album.path)),
            key=lambda row: (row[1] or _EARLIEST, row[2]),
            reverse=True,
        )[:ALBUM_PICTURES]
        parents = " › ".join(names[pk] for pk in album.ancestor_ids if pk in names)
        cards[album.pk] = Preview(
            kind=ALBUM,
            url=reverse("photo_album:detail", args=[album.pk]),
            title=album.name,
            kicker=" · ".join(filter(None, ["Fotoalbum", album.folder, parents])),
            details=[f"{count} medie{'r' if count != 1 else ''}" if count else "Ingen medier endnu"],
            pictures=[storage.url(row[3]) for row in pictures],
        )
    return cards


def videos_allowed(request: HttpRequest) -> bool:
    """Whether this reader gets video cards at all: GAHKFlix has its own rollout gate."""
    from gahkflix import access

    return access.request_allowed(request) and not is_kitchen(request)


def _videos(request: HttpRequest, pks: set[int]) -> dict[int, Preview]:
    """A video's thumbnail, title and running time — for a reader GAHKFlix would show it to. One query."""
    from gahkflix import access
    from gahkflix.models import Video
    from gahkflix.templatetags.gahkflix import duration, picture_url

    if not videos_allowed(request):
        return {}
    cards: dict[int, Preview] = {}
    for video in Video.objects.filter(pk__in=pks).select_related("file", "series"):
        if not access.can_see(request, video):
            continue
        cards[video.pk] = Preview(
            kind=VIDEO,
            url=reverse("gahkflix:detail", args=[video.pk]),
            title=video.title,
            kicker=" · ".join(filter(None, ["GAHKFlix", video.series.title if video.series else ""])),
            details=list(filter(None, [video.episode_label, video.date_label])),
            pictures=list(filter(None, [picture_url(video, "thumbnail")])),
            state="I papirkurven" if video.is_binned else "Kladde" if video.is_draft else "",
            duration=duration(video.duration),
        )
    return cards
