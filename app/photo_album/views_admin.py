"""The administrators' photo-album dashboard, part of the site admin (residents.urls_admin): the
overview, the dating in detail, and the items behind each of the overview's counts."""

from dataclasses import dataclass
from datetime import datetime

from django.contrib import messages
from django.core.paginator import Page, Paginator
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from residents.permissions import role_required

from . import dashboard
from .models import PURGE_AFTER, Media, StorageReport
from .tasks import measure_photo_album_storage

LIST_PAGE_SIZE = 25  # the dating page's two lists
UPKEEP_PAGE_SIZE = 50
TIERS_BY_KEY = {tier.key: tier for tier in dashboard.TIERS}
# The ?kilde= values of the list of items without a date of their own, and their names there.
UNDATED_FILTERS = {key: TIERS_BY_KEY[key] for key in ("neighbours", "album_year", "added")}
ORPHANS = "filer-uden-billede"  # the list of files no item points at, from the storage measurement


@dataclass
class ListedMedia:
    media: Media
    album: str  # "Root / Sub"
    tier: dashboard.Tier
    expires: datetime | None = None  # when it goes for good, if it is waiting to
    group: int = 0  # in the list of duplicates: which group of the same content


@dataclass
class MediaPage:
    page: Page
    items: list[ListedMedia]
    previous_url: str
    next_url: str


def _media_page(
    request: HttpRequest,
    queryset: QuerySet[Media],
    param: str,
    anchor: str = "",
    *,
    size: int = LIST_PAGE_SIZE,
) -> MediaPage:
    """A page of `queryset`, its number in `param`, with links to its neighbours that keep the rest
    of the query (another list's page included)."""
    page = Paginator(queryset, size).get_page(request.GET.get(param))
    items = list(page)
    labels = dashboard.media_labels(items)

    def url(number: int) -> str:
        query = request.GET.copy()
        query[param] = str(number)
        return f"?{query.urlencode()}" + (f"#{anchor}" if anchor else "")

    return MediaPage(
        page=page,
        items=[
            ListedMedia(
                media,
                labels[media.pk],
                TIERS_BY_KEY[dashboard.tier_of(media.date_source, media.file_time_source)],
            )
            for media in items
        ],
        previous_url=url(page.previous_page_number()) if page.has_previous() else "",
        next_url=url(page.next_page_number()) if page.has_next() else "",
    )


def _latest_report() -> StorageReport | None:
    return StorageReport.objects.first()


@role_required("administrator")
def photo_album_dashboard(request: HttpRequest) -> HttpResponse:
    report = _latest_report()
    view = dashboard.overview(report)
    return render(
        request,
        "siteadmin/photo_album.html",
        {
            "now": timezone.now(),
            "view": view,
            "storage": dashboard.storage(view, report),
            "upkeep": dashboard.upkeep(),
            "orphans_list": ORPHANS,
        },
    )


@role_required("administrator")
def photo_album_dates(request: HttpRequest) -> HttpResponse:
    """How the album is dated, in detail: by tier, year, folder and album, and the items to look at."""
    view = dashboard.overview()
    folder = request.GET.get("mappe", "")
    selected = next((row for row in view.folders if row.folder == folder), None)
    if selected is None:
        folder = ""
    by_file_time = request.GET.get("filtid") == "1"
    source = request.GET.get("kilde", "")
    if source not in UNDATED_FILTERS:
        source = ""
    outside = _media_page(
        request, dashboard.outside_media(folder, by_file_time=by_file_time), "uden_for", "uden-for"
    )
    undated = _media_page(request, dashboard.undated_media(folder, source), "uden_dato", "uden-dato")
    counts = selected.counts if selected else view.counts
    return render(
        request,
        "siteadmin/photo_album_dates.html",
        {
            "view": view,
            "counts": counts,
            "tiers": dashboard.TIERS,
            "folder": folder,
            "selected": selected,
            "album_rows": dashboard.album_rows(view, folder),
            "outside": outside,
            "outside_shown_count": dashboard.outside_media(folder).count()
            if by_file_time
            else outside.page.paginator.count,
            "outside_file_count": outside.page.paginator.count
            if by_file_time
            else dashboard.outside_media(folder, by_file_time=True).count(),
            "by_file_time": by_file_time,
            "undated": undated,
            "undated_filters": [(key, tier, counts.tiers[key]) for key, tier in UNDATED_FILTERS.items()],
            "source": source,
            "file_time_sources_to_read": dashboard.MEDIA_LISTS["filtid-kilde"].items().count(),
            "capture_dates_to_read": dashboard.MEDIA_LISTS["optagelsesdato"].items().count(),
        },
    )


@role_required("administrator")
def photo_album_list(request: HttpRequest, kind: str) -> HttpResponse:
    """The items behind one of the overview's counts, each with everything known about it — or the
    files no item points at, from the latest storage measurement."""
    others = [(media_list, media_list.items().count()) for media_list in dashboard.MEDIA_LISTS.values()]
    context: dict[str, object] = {
        "now": timezone.now(),
        "kind": kind,
        "others": others,
        "orphans_list": ORPHANS,
    }
    if kind == ORPHANS:
        report = _latest_report()
        return render(
            request,
            "siteadmin/photo_album_list.html",
            {
                **context,
                "title": "Filer uden billede",
                "hint": "Filer under photo-album/, som intet billede eller nogen video peger på, blandt dem, der er"
                " over et døgn gamle: rester fra en fejlet upload, import eller sletning. Fra den seneste måling af"
                " lageret; den gemmer de første 50. Intet slettes herfra.",
                "report": report,
                "orphans": dashboard.orphan_files(report),
            },
        )
    media_list = dashboard.MEDIA_LISTS.get(kind)
    if media_list is None:
        raise Http404
    queryset = media_list.items().select_related("album", "requested_by", "approved_by", "deleted_by")
    page = _media_page(request, queryset, "side", size=UPKEEP_PAGE_SIZE)
    group, previous_hash = 0, None
    for row in page.items:
        if kind == "afventer":
            row.expires = row.media.added_at + PURGE_AFTER
        elif kind == "papirkurv" and row.media.deleted_at is not None:
            row.expires = row.media.deleted_at + PURGE_AFTER
        if row.media.sha256 != previous_hash:
            group, previous_hash = group + 1, row.media.sha256
        row.group = group
    return render(
        request,
        "siteadmin/photo_album_list.html",
        {
            **context,
            "title": media_list.title,
            "hint": media_list.hint,
            "media_list": media_list,
            "list": page,
        },
    )


@require_POST
@role_required("administrator")
def photo_album_measure(request: HttpRequest) -> HttpResponseRedirect:
    """Measure the storage now rather than tonight."""
    measure_photo_album_storage.delay()
    messages.success(request, "Lageret måles nu. Genindlæs siden om et par minutter.")
    return redirect(f"{reverse('siteadmin:photo_album')}#lager")
