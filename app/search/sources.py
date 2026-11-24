"""What the site-wide search looks in, one source per feature.

EVERY SOURCE ASKS ITS FEATURE WHO MAY SEE WHAT, and none decides that itself: the pages come from the
sidebar's own builder (core.context_processors.nav_sections), the opslag behind opslagstavlen's
rollout gate, and so on. A search that knew its own rules would drift from the pages it links to,
and the first sign would be a result that answers 403 — or worse, a name or a title someone was not
meant to see, which no click is needed to leak.

A source takes the request, the query and how many hits it may show, and returns a Section, or None
when the reader may not use that feature at all (so the page does not show an empty heading for
something they cannot open). It fetches one more hit than it shows, to know whether there are more.
"""

import html
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.db.models import Q
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from core.context_processors import nav_sections
from core.markdown import plain_text
from residents.models import Residency, active_period
from residents.permissions import CMS_EDITOR_ROLES, current_resident, effective_roles, request_has_role

from .text import excerpt, fold


@dataclass(frozen=True)
class Query:
    text: str  # as typed, stripped
    marks: list[str]  # what to highlight: its words, and their Danish stems (opslagstavle.search)

    @property
    def words(self) -> list[str]:
        return self.text.split()


@dataclass(frozen=True)
class Hit:
    title: str
    url: str
    detail: str = ""  # one line under the title: where it is, when, who
    snippet: str = ""  # the matching text, for content (an opslag's words around the match)
    icon: str = ""  # a symbol of base.html's sprite, for hits without a picture
    avatar: str = ""  # a picture URL (a resident's photo)
    initials: str = ""  # a resident without a photo
    external: bool = False  # opens in a new tab (the wiki, the feedback form)


@dataclass
class Section:
    key: str
    label: str
    icon: str
    hits: list[Hit] = field(default_factory=list)
    more: bool = False  # there are more hits than shown
    more_url: str = ""  # the feature's own search, which shows them all
    more_label: str = ""


def _with_query(url: str, query: Query, **extra: str) -> str:
    return f"{url}?{urlencode({'q': query.text, **extra})}"


def _clip(hits: list[Hit], limit: int) -> tuple[list[Hit], bool]:
    return hits[:limit], len(hits) > limit


def _date(moment: object) -> str:
    return date_format(timezone.localtime(moment), "j. M Y")  # type: ignore[arg-type]


# --- Sider: the intranet's pages and tools ---------------------------------------------------------

# Other words people use for a page, so "kalender" finds Begivenheder and "øl" the ølkælder. Keyed
# by the sidebar label; a page missing here is still found by its own name.
KEYWORDS: dict[str, tuple[str, ...]] = {
    "Dashboard": ("forside", "hjem", "oversigt"),
    "Min profil": ("profilbillede", "bio"),
    "Den Hurtige": ("chat", "beskeder", "køkkenet"),
    "Opslagstavle": ("opslag", "nyheder", "facebook"),
    "Begivenheder": ("kalender", "arrangementer", "fester", "tilmelding", "events"),
    "Alumneliste": ("beboere", "beboerliste", "telefonliste", "værelser"),
    "Fotoalbum": ("billeder", "fotos", "album"),
    "GAHKFlix": ("film", "video", "revy"),
    "Søg værelse": ("værelsesrunden", "flytte", "kvotient"),
    "Værelsestjek": ("tjek",),
    "Værelsesudbud": ("udbud", "værelsesrunden"),
    "Ølkælder": ("øl", "saldo", "indbetaling", "bar"),
    "Ølkælder-admin": ("salgsoverblik", "personoversigt", "rente"),
    "Regnskab": ("økonomi", "bogføring"),
    "Reparationer": ("reparation", "i stykker", "defekt"),
    "Ansøgninger": ("optagelse", "ansøgere"),
    "Hjemmeside": ("gahk.dk", "cms", "sider"),
    "Site-admin": ("administration",),
    "Roller": ("rettigheder", "embedsgrupper"),
    "Stamtræ": ("fylgje", "slægt"),
    "Statistik": ("tal", "grafer"),
    "Wiki": ("vejledning", "håndbog"),
    "Fejl & ønsker": ("feedback", "fejlmelding", "ønsker"),
    "Notifikationsindstillinger": ("notifikationer", "push", "mails"),
    "Skift kodeord": ("adgangskode", "password"),
    "Installér appen": ("app", "telefon", "hjemmeskærm"),
}


@dataclass(frozen=True)
class _Page:
    label: str
    url: str
    section: str
    icon: str


def _pages(request: HttpRequest) -> list[_Page]:
    """Every page this reader's sidebar shows, then the ones only reached from their account menu."""
    roles = effective_roles(request)
    pages = [
        _Page(label, url, section, icon)
        for section, items in nav_sections(roles, request.user.pk or 0)
        for url, label, icon in items
    ]
    account = "Min konto"
    pages += [
        _Page("Rediger profil", reverse("edit_profile"), account, "edit"),
        _Page("Notifikationsindstillinger", reverse("notifications:preferences"), account, "inbox"),
        _Page("Skift kodeord", reverse("password_change"), account, "lock"),
        _Page("Installér appen", reverse("install"), account, "share"),
    ]
    return pages


def _page_rank(page: _Page, query: Query) -> int | None:
    """How well `page` answers `query`, best first; None when some word is not found at all."""
    label = fold(page.label)
    label_words = label.replace("-", " ").split()
    other = fold(" ".join([*KEYWORDS.get(page.label, ()), page.section]))
    words = [fold(word) for word in query.words]
    if not all(word in label or word in other for word in words):
        return None
    if label.startswith(fold(query.text)):
        return 0
    if all(any(lw.startswith(word) for lw in label_words) for word in words):
        return 1
    if all(word in label for word in words):
        return 2
    return 3


def pages(request: HttpRequest, query: Query, limit: int) -> Section | None:
    ranked = [
        (rank, i, page)
        for i, page in enumerate(_pages(request))
        if (rank := _page_rank(page, query)) is not None
    ]
    ranked.sort(key=lambda item: item[:2])
    hits = [
        Hit(
            title=page.label,
            url=page.url,
            detail=page.section,
            icon=page.icon,
            external="://" in page.url,
        )
        for _rank, _i, page in ranked
    ]
    section = Section("pages", "Sider", "dashboard")
    section.hits, section.more = _clip(hits, limit)
    return section


# --- Alumnelisten: who lives here now ----------------------------------------------------------------


def residents(request: HttpRequest, query: Query, limit: int) -> Section | None:
    from residents import search as resident_search

    year, month = active_period()
    rows = list(
        resident_search.matching(
            Residency.objects.filter(year=year, month=month).select_related("resident", "room", "workgroup"),
            query.text,
        ).order_by("resident__first_name", "resident__last_name", "resident_id")[: limit + 1]
    )
    hits = []
    for row in rows:
        person = row.resident
        detail = [f"Værelse {row.room.number:03d}"]
        if row.workgroup is not None:
            detail.append(row.workgroup.name)
        if person.study:
            detail.append(person.study)
        hits.append(
            Hit(
                title=person.full_name,
                url=reverse("resident_profile", args=[person.pk]),
                detail=" · ".join(detail),
                avatar=person.avatar_url,
                initials=f"{person.first_name[:1]}{person.last_name[:1]}",
            )
        )
    section = Section(
        "residents",
        "Alumnelisten",
        "list",
        more_url=_with_query(reverse("directory"), query),
        more_label="Søg i alumnelisten",
    )
    section.hits, section.more = _clip(hits, limit)
    return section


# --- Opslagstavlen ----------------------------------------------------------------------------------


def notices(request: HttpRequest, query: Query, limit: int) -> Section | None:
    from opslagstavle import access
    from opslagstavle import search as notice_search
    from opslagstavle.models import Notice

    if not access.request_allowed(request):
        return None
    found = list(
        notice_search.search(Notice.objects.select_related("author"), query.text).order_by(
            "-created_at", "-pk"
        )[: limit + 1]
    )
    hits = [
        Hit(
            title=notice.author.full_name,
            url=reverse("opslagstavle:detail", args=[notice.pk]),
            detail=f"{notice.get_category_display()} · {_date(notice.created_at)}",
            snippet=excerpt(html.unescape(plain_text(notice.body)), query.marks),
            avatar=notice.author.avatar_url,
            initials=f"{notice.author.first_name[:1]}{notice.author.last_name[:1]}",
        )
        for notice in found
    ]
    section = Section(
        "notices",
        "Opslagstavlen",
        "board",
        more_url=_with_query(reverse("opslagstavle:board"), query),
        more_label="Søg på opslagstavlen",
    )
    section.hits, section.more = _clip(hits, limit)
    return section


# --- Begivenheder ------------------------------------------------------------------------------------


def events(request: HttpRequest, query: Query, limit: int) -> Section | None:
    """Only what the events list would show this reader: open events and the private ones they
    organise or are invited to (events.access.visible_to), not yet over. A private event's title is
    itself the secret, so this never starts from Event.objects."""
    from events import access

    if not access.request_allowed(request):
        return None
    found = access.visible_to(current_resident(request)).upcoming()
    for word in query.words:
        found = found.filter(
            Q(title__icontains=word) | Q(description__icontains=word) | Q(location__icontains=word)
        )
    hits = []
    for event in found[: limit + 1]:
        detail = [_date(event.starts_at)]
        if event.location:
            detail.append(event.location)
        if event.cancelled_at:
            detail.append("Aflyst")
        hits.append(
            Hit(
                title=event.title,
                url=reverse("events:detail", args=[event.pk]),
                detail=" · ".join(detail),
                snippet=excerpt(event.description, query.marks, words=20),
                icon="calendar",
            )
        )
    section = Section("events", "Begivenheder", "calendar")
    section.hits, section.more = _clip(hits, limit)
    return section


# --- Fotoalbum ---------------------------------------------------------------------------------------


def albums(request: HttpRequest, query: Query, limit: int) -> Section | None:
    """Albums by name, tag or year folder. Every album is open to every resident who may use the
    photo album (there are no private ones); the same gate as an album embedded in an opslag."""
    from opslagstavle.embeds import albums_allowed
    from photo_album.models import Album

    if not albums_allowed(request):
        return None
    found = Album.objects.select_related("parent")
    for word in query.words:
        found = found.filter(Q(name__icontains=word) | Q(tags__name__icontains=word) | Q(folder=word))
    found = found.distinct().order_by("-folder", "name", "pk")
    hits = []
    for album in found[: limit + 1]:
        where = [album.folder]
        if album.parent is not None:
            where.append(album.parent.name)
        hits.append(
            Hit(
                title=album.name,
                url=reverse("photo_album:detail", args=[album.pk]),
                detail=" · ".join(where),
                icon="photo",
            )
        )
    section = Section("albums", "Fotoalbum", "photo")
    section.hits, section.more = _clip(hits, limit)
    return section


# --- GAHKFlix ----------------------------------------------------------------------------------------


def films(request: HttpRequest, query: Query, limit: int) -> Section | None:
    """Published films only (gahkflix.services.search, GAHKFlix's own search box), behind its rollout
    gate. Drafts stay out even for those who may see them: they are found on GAHKFlix itself."""
    from gahkflix import services
    from opslagstavle.embeds import videos_allowed

    if not videos_allowed(request):
        return None
    found = services.search(query.text).order_by("-published_at", "-pk")
    hits = []
    for video in found[: limit + 1]:
        title = video.title
        detail = [video.date_label]
        if video.series is not None:
            detail.insert(0, " ".join(filter(None, [video.series.title, video.episode_label])))
        hits.append(
            Hit(
                title=title,
                url=reverse("gahkflix:detail", args=[video.pk]),
                detail=" · ".join(detail),
                icon="film",
            )
        )
    section = Section(
        "films",
        "GAHKFlix",
        "film",
        more_url=_with_query(reverse("gahkflix:search"), query),
        more_label="Søg på GAHKFlix",
    )
    section.hits, section.more = _clip(hits, limit)
    return section


# --- Reparationer ------------------------------------------------------------------------------------


def repairs(request: HttpRequest, query: Query, limit: int) -> Section | None:
    """Open tickets first, then archived ones: the archive is kept precisely so it can be searched.
    Open to every resident, like the board."""
    from django.db.models import F

    from reparationer.models import RepairTask
    from reparationer.views import search_tasks

    found = search_tasks(RepairTask.objects.select_related("reported_by"), query.text).order_by(
        F("archived_at").desc(nulls_first=True), "-created_at", "-pk"
    )
    hits = []
    for task in found[: limit + 1]:
        detail = [task.get_status_display()]
        if task.archived_at:
            detail = ["Arkiveret"]
        if task.location:
            detail.append(task.location)
        detail.append(_date(task.created_at))
        hits.append(
            Hit(
                title=task.title,
                url=reverse("reparationer:detail", args=[task.pk]),
                detail=" · ".join(detail),
                snippet=excerpt(task.description, query.marks, words=20),
                icon="wrench",
            )
        )
    section = Section(
        "repairs",
        "Reparationer",
        "wrench",
        more_url=_with_query(reverse("reparationer:board"), query),
        more_label="Søg i reparationer",
    )
    section.hits, section.more = _clip(hits, limit)
    return section


# --- Hjemmeside: gahk.dk's pages, for the people who edit them ----------------------------------------


def website_pages(request: HttpRequest, query: Query, limit: int) -> Section | None:
    """gahk.dk's pages by heading or address, opening in the editor. Only for its editors: everyone
    else reads gahk.dk itself, and the editor would answer 403."""
    from cms.models import Page

    if not request_has_role(request, *CMS_EDITOR_ROLES):
        return None
    found = Page.objects.all()
    for word in query.words:
        found = found.filter(Q(header__icontains=word) | Q(slug__icontains=word))
    hits = [
        Hit(
            title=page.header,
            url=reverse("cms:page_edit", args=[page.pk]),
            detail=f"gahk.dk/{page.slug}" if page.slug else "Ingen adresse",
            icon="globe",
        )
        for page in found.order_by("header", "pk")[: limit + 1]
    ]
    section = Section("website", "Hjemmeside", "globe")
    section.hits, section.more = _clip(hits, limit)
    return section


Source = Callable[[HttpRequest, Query, int], Section | None]

# In the order the results page shows them.
SOURCES: list[Source] = [pages, residents, notices, events, albums, films, repairs, website_pages]
