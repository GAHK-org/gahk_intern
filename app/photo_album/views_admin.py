"""The administrators' photo-album dashboard, part of the site admin (residents.urls_admin)."""

from dataclasses import dataclass

from django.contrib import messages
from django.core.paginator import Page, Paginator
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from residents.permissions import role_required

from . import dashboard
from .models import Media, StorageReport
from .tasks import measure_photo_album_storage

LIST_PAGE_SIZE = 25
TIERS_BY_KEY = {tier.key: tier for tier in dashboard.TIERS}
# The ?kilde= values of the list of items without a date of their own, and their names there.
UNDATED_FILTERS = {key: TIERS_BY_KEY[key] for key in ("neighbours", "album_year", "added")}


@dataclass
class ListedMedia:
    media: Media
    album: str  # "Root / Sub"
    tier: dashboard.Tier


@dataclass
class MediaList:
    page: Page
    items: list[ListedMedia]
    previous_url: str
    next_url: str


def _media_list(request: HttpRequest, queryset: QuerySet[Media], param: str, anchor: str) -> MediaList:
    """A page of `queryset`, its number in `param`, with links to its neighbours that keep the rest
    of the page's query (the other list's page included)."""
    page = Paginator(queryset, LIST_PAGE_SIZE).get_page(request.GET.get(param))
    items = list(page)
    labels = dashboard.media_labels(items)

    def url(number: int) -> str:
        query = request.GET.copy()
        query[param] = str(number)
        return f"?{query.urlencode()}#{anchor}"

    return MediaList(
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


@role_required("administrator")
def photo_album_dashboard(request: HttpRequest) -> HttpResponse:
    report = StorageReport.objects.first()
    view = dashboard.overview(report)
    folder = request.GET.get("mappe", "")
    selected = next((row for row in view.folders if row.folder == folder), None)
    if selected is None:
        folder = ""
    by_file_time = request.GET.get("filtid") == "1"
    source = request.GET.get("kilde", "")
    if source not in UNDATED_FILTERS:
        source = ""
    outside = _media_list(
        request, dashboard.outside_media(folder, by_file_time=by_file_time), "uden_for", "uden-for"
    )
    undated = _media_list(request, dashboard.undated_media(folder, source), "uden_dato", "uden-dato")
    counts = selected.counts if selected else view.counts
    return render(
        request,
        "siteadmin/photo_album.html",
        {
            "now": timezone.now(),
            "view": view,
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
            "undated_total": counts.without_own_date,
            "source": source,
            "storage": dashboard.storage(view, report),
            "upkeep": dashboard.upkeep(),
        },
    )


@require_POST
@role_required("administrator")
def photo_album_measure(request: HttpRequest) -> HttpResponseRedirect:
    """Measure the storage now rather than tonight."""
    measure_photo_album_storage.delay()
    messages.success(request, "Lageret måles nu. Genindlæs siden om et par minutter.")
    return redirect(f"{reverse('siteadmin:photo_album')}#lager")
