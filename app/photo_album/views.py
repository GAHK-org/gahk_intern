import logging
import posixpath
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import compare_digest
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.db import transaction
from django.db.models import Count, Max, Q, QuerySet, Sum
from django.db.models.functions import Coalesce
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
)
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from arkiv.storage import content_disposition
from core.media import REDIRECT_CACHE_CONTROL
from residents.models import Resident
from residents.permissions import current_resident

from . import access, services
from .forms import AlbumForm, AlbumNameForm, DownloadForm, MediaUploadForm, ZipAlbumImportForm
from .models import (
    Album,
    AlbumDownload,
    AlbumDownloadState,
    AlbumImport,
    AlbumImportState,
    DerivativeState,
    DownloadVariant,
    Media,
    MediaStatus,
)
from .storage import PhotoAlbumS3Storage, get_photo_album_storage

logger = logging.getLogger(__name__)

# Tiles per page of the album grid and the timeline. Pages are loaded in both directions as the
# reader scrolls, and the frontend drops pages far off screen again, so this only sets the step.
PAGE_SIZE = 60


def album_folder_options() -> list[str]:
    folders = Album.objects.order_by("folder").values_list("folder", flat=True).distinct()
    return ["Andet", *(folder for folder in folders if folder != "Andet")]


def _token_import_resident(request: HttpRequest) -> Resident | None:
    token = settings.PHOTO_ALBUM_IMPORT_TOKEN
    authorization = request.headers.get("Authorization", "")
    if not token or not compare_digest(authorization, f"Bearer {token}"):
        return None
    resident, created = Resident.objects.get_or_create(
        email=settings.PHOTO_ALBUM_SYSTEM_IMPORT_EMAIL,
        defaults={"first_name": "System", "last_name": "", "is_active": True},
    )
    if created:
        resident.set_unusable_password()
        resident.save(update_fields=["password"])
    return resident


def _safe_next(request: HttpRequest, default: str) -> str:
    """Where a POST came from, when it says so and the place is on this site."""
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return default


def _wants_json(request: HttpRequest) -> bool:
    return "application/json" in request.headers.get("Accept", "")


# --- Album summaries ----------------------------------------------------------------------------


def _path_ids(album: Album) -> list[int]:
    return [int(part) for part in album.path.strip("/").split("/") if part]


def _summarise(albums: Iterable[Album]) -> None:
    """Give each album `media_count` and `cover_url` for its whole subtree, in three queries.

    Counted per album and added up the tree in Python rather than with a correlated subquery per
    card: `path LIKE outer.path || '%'` cannot use the path index, so the subquery read every media
    row once per album on the page.
    """
    albums = list(albums)
    if not albums:
        return
    wanted = {album.pk for album in albums}
    counts: dict[int, int] = defaultdict(int)
    newest: dict[int, int] = {}
    paths = dict(Album.objects.values_list("pk", "path"))
    approved = Media.objects.filter(status=MediaStatus.APPROVED, deleted_at__isnull=True)
    per_album = approved.values("album_id").annotate(
        n=Count("pk"), latest=Max("pk", filter=Q(derivative_state=DerivativeState.READY) & ~Q(thumbnail=""))
    )
    for row in per_album.order_by():
        for ancestor in (int(part) for part in paths.get(row["album_id"], "").strip("/").split("/") if part):
            if ancestor not in wanted:
                continue
            counts[ancestor] += row["n"]
            if row["latest"] is not None and row["latest"] > newest.get(ancestor, 0):
                newest[ancestor] = row["latest"]
    thumbnails = dict(Media.objects.filter(pk__in=newest.values()).values_list("pk", "thumbnail"))
    storage = get_photo_album_storage()
    for album in albums:
        album.media_count = counts.get(album.pk, 0)  # type: ignore[attr-defined]
        cover = thumbnails.get(newest.get(album.pk, 0))
        album.cover_url = storage.url(cover) if cover else ""  # type: ignore[attr-defined]


def _album_paths(albums: Iterable[Album]) -> dict[int, str]:
    """ "Root / Sub / Sub-sub" for each album, from one query for every name along the way."""
    albums = list(albums)
    ids = {pk for album in albums for pk in _path_ids(album)}
    names = dict(Album.objects.filter(pk__in=ids).values_list("pk", "name"))
    return {album.pk: " / ".join(names.get(pk, "?") for pk in _path_ids(album)) for album in albums}


# --- Paging -------------------------------------------------------------------------------------
#
# Keyset paging on (sort_at, pk), newest first — the order of the `media_sort_idx` index. A page is
# asked for relative to a tile the reader already has: `after=<pk>` is the next, older page below it,
# `before=<pk>` the newer page above it. Offsets would shift under the reader as uploads arrive, and
# would make "open the album at this photo" (a deep link, a year in the timeline) impossible to
# continue from in both directions.


@dataclass
class MediaPage:
    items: list[Media]
    newer_url: str = ""
    older_url: str = ""


def _page_url(request: HttpRequest, **params: object) -> str:
    query = request.GET.copy()
    for key in ("after", "before", "media", "year"):
        query.pop(key, None)
    for key, value in params.items():
        query[key] = str(value)
    return f"{request.path}?{query.urlencode()}"


def _int_param(request: HttpRequest, name: str) -> int | None:
    value = request.GET.get(name, "")
    return int(value) if value.isdecimal() else None


def _older(queryset: QuerySet[Media], condition: Q, limit: int = PAGE_SIZE) -> tuple[list[Media], bool]:
    rows = list(queryset.filter(condition).order_by("-sort_at", "-pk")[: limit + 1])
    return rows[:limit], len(rows) > limit


def _newer(queryset: QuerySet[Media], condition: Q, limit: int = PAGE_SIZE) -> tuple[list[Media], bool]:
    rows = list(queryset.filter(condition).order_by("sort_at", "pk")[: limit + 1])
    return rows[:limit][::-1], len(rows) > limit


def _older_than(sort_at: datetime, pk: int) -> Q:
    return Q(sort_at__lt=sort_at) | Q(sort_at=sort_at, pk__lt=pk)


def _newer_than(sort_at: datetime, pk: int) -> Q:
    return Q(sort_at__gt=sort_at) | Q(sort_at=sort_at, pk__gt=pk)


def _anchor(queryset: QuerySet[Media], pk: int | None) -> tuple[datetime, int] | None:
    """Where a tile sits in the order — looked up in the reader's own scope, so it leaks nothing."""
    if pk is None:
        return None
    sort_at = queryset.filter(pk=pk).values_list("sort_at", flat=True).first()
    return (sort_at, pk) if sort_at is not None else None


def _page(request: HttpRequest, queryset: QuerySet[Media]) -> tuple[MediaPage, bool]:
    """The page this request asks for, and whether it asked for a fragment (a further page)."""

    def page(items: list[Media], *, newer: bool, older: bool) -> MediaPage:
        return MediaPage(
            items,
            newer_url=_page_url(request, before=items[0].pk) if newer and items else "",
            older_url=_page_url(request, after=items[-1].pk) if older and items else "",
        )

    if "after" in request.GET:
        anchor = _anchor(queryset, _int_param(request, "after"))
        if anchor is None:
            raise Http404("no such tile")
        items, more = _older(queryset, _older_than(*anchor))
        return page(items, newer=False, older=more), True
    if "before" in request.GET:
        anchor = _anchor(queryset, _int_param(request, "before"))
        if anchor is None:
            raise Http404("no such tile")
        items, more = _newer(queryset, _newer_than(*anchor))
        return page(items, newer=more, older=False), True
    if (anchor := _anchor(queryset, _int_param(request, "media"))) is not None:
        above, more_above = _newer(queryset, _newer_than(*anchor), PAGE_SIZE // 2)
        below, more_below = _older(queryset, Q(pk=anchor[1]) | _older_than(*anchor))
        return page([*above, *below], newer=more_above, older=more_below), False
    if (year := _int_param(request, "year")) is not None and 1900 <= year <= 9998:
        boundary = datetime(year + 1, 1, 1, tzinfo=timezone.get_current_timezone())
        items, more = _older(queryset, Q(sort_at__lt=boundary))
        return page(items, newer=queryset.filter(sort_at__gte=boundary).exists(), older=more), False
    items, more = _older(queryset, Q())
    return page(items, newer=False, older=more), False


def _tiles_response(request: HttpRequest, media_page: MediaPage, **context: object) -> HttpResponse:
    return render(request, "photo_album/_media_tiles.html", {"page": media_page, **context})


# --- Browsing -----------------------------------------------------------------------------------


@login_required
def index(request: HttpRequest) -> HttpResponse:
    folder = request.GET.get("folder", "")
    albums = Album.objects.filter(parent__isnull=True)
    if folder:
        albums = albums.filter(folder=folder)
    # "Andet" first, then the years newest first.
    ordered = sorted(
        albums.order_by("name"), key=lambda album: (album.folder != "Andet", _negated(album.folder))
    )
    _summarise(ordered)
    return render(
        request,
        "photo_album/index.html",
        {
            "albums": ordered,
            "can_create": access.can_create_album(request),
            "can_view_all_bins": access.can_view_all_bins(request),
            "folder": folder,
        },
    )


def _negated(folder: str) -> tuple[int, str]:
    return (-int(folder), "") if folder.isdecimal() else (0, folder)


@login_required
def timeline(request: HttpRequest) -> HttpResponse:
    """Everything the reader may see, newest first, across every album."""
    visible = access.visible_media(request)
    media_page, fragment = _page(request, visible)
    context = {"can_manage": access.can_manage_media(request)}
    if fragment:
        return _tiles_response(request, media_page, **context)
    years = [moment.year for moment in visible.datetimes("sort_at", "year", order="DESC")]
    return render(
        request,
        "photo_album/timeline.html",
        {
            "page": media_page,
            "years": years,
            "year": _int_param(request, "year"),
            "variants": DownloadVariant.choices,
            **context,
        },
    )


@login_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    album = get_object_or_404(Album, pk=pk)
    include_sub_albums = request.GET.get("alle") == "1"
    visible = access.visible_media(request, album, subtree=include_sub_albums)
    media_page, fragment = _page(request, visible)
    can_manage = access.can_manage_media(request)
    context = {"can_manage": can_manage, "album": album}
    if fragment:
        return _tiles_response(request, media_page, **context)

    children = list(album.children.order_by("name"))
    _summarise(children)
    resident = current_resident(request)
    return render(
        request,
        "photo_album/detail.html",
        {
            **context,
            "page": media_page,
            "children": children,
            "ancestors": album.ancestors(),
            "include_sub_albums": include_sub_albums,
            "has_subtree_media": bool(media_page.items)
            or access.visible_media(request, album, subtree=True).exists(),
            "album_locked": album.is_locked(),
            "locked_by": album.locked_by,
            "can_upload": access.can_upload(request, album),
            "can_lock": access.can_lock_album(request, album),
            "can_unlock": access.can_unlock_album(request, album),
            "can_rename": access.can_rename_album(request, album),
            "can_create_sub_album": access.can_create_sub_album(request, album),
            "can_view_bin": access.can_view_album_bin(request),
            # Cheapest test first: tiles on the page or sub-albums already answer it without a query.
            "can_delete_album": access.can_create_album(request)
            and not children
            and not media_page.items
            and not album.media.exists(),
            "download_job": AlbumDownload.objects.filter(album=album, requested_by=resident).first(),
            "variants": DownloadVariant.choices,
        },
    )


# --- Album management ---------------------------------------------------------------------------


@login_required
def create_album(request: HttpRequest) -> HttpResponse:
    if not access.can_create_album(request):
        raise PermissionDenied
    form = AlbumForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            album = services.create_album(name=form.cleaned_data["name"], folder=form.cleaned_data["folder"])
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            return redirect("photo_album:detail", album.pk)
    return render(
        request,
        "photo_album/form.html",
        {
            "form": form,
            "title": "Opret album",
            "folder_options": album_folder_options(),
        },
    )


def _form_error_messages(request: HttpRequest, form: AlbumNameForm) -> None:
    for errors in form.errors.values():
        for error in errors:
            messages.error(request, str(error))


def _error_messages(request: HttpRequest, error: ValidationError) -> None:
    for message in error.messages:
        messages.error(request, message)


@login_required
@require_POST
def create_sub_album(request: HttpRequest, pk: int) -> HttpResponse:
    parent = get_object_or_404(Album, pk=pk)
    if not access.can_create_sub_album(request, parent):
        raise PermissionDenied
    form = AlbumNameForm(request.POST)
    if not form.is_valid():
        _form_error_messages(request, form)
        return redirect("photo_album:detail", parent.pk)
    try:
        album = services.create_album(name=form.cleaned_data["name"], parent=parent)
    except ValidationError as exc:
        _error_messages(request, exc)
        return redirect("photo_album:detail", parent.pk)
    messages.success(request, f"Underalbummet {album.name} er oprettet.")
    return redirect("photo_album:detail", album.pk)


@login_required
@require_POST
def rename_album(request: HttpRequest, pk: int) -> HttpResponse:
    album = get_object_or_404(Album, pk=pk)
    if not access.can_rename_album(request, album):
        raise PermissionDenied
    form = AlbumNameForm(request.POST)
    if not form.is_valid():
        _form_error_messages(request, form)
        return redirect("photo_album:detail", album.pk)
    try:
        services.rename_album(album, form.cleaned_data["name"])
    except ValidationError as exc:
        _error_messages(request, exc)
    else:
        messages.success(request, f"Albummet hedder nu {album.name}.")
    return redirect("photo_album:detail", album.pk)


@login_required
@require_POST
def delete_album(request: HttpRequest, pk: int) -> HttpResponse:
    if not access.can_create_album(request):
        raise PermissionDenied
    album = get_object_or_404(Album, pk=pk)
    try:
        album.delete()
    except ValidationError:
        # The button is drawn only for an album with no media and no sub-albums, but binned items
        # count too. Say so instead of 500ing; the way out is to empty the bin (or wait for the
        # 30-day sweep) and come back.
        messages.error(
            request,
            "Albummet kan kun slettes, når det hverken har underalbummer eller medier — "
            "heller ikke i papirkurven.",
        )
        return redirect("photo_album:detail", album.pk)
    if album.parent_id is not None:
        return redirect("photo_album:detail", album.parent_id)
    return redirect("photo_album:index")


@login_required
@require_POST
def lock_album(request: HttpRequest, pk: int) -> HttpResponse:
    album = get_object_or_404(Album, pk=pk)
    if not access.can_lock_album(request, album):
        raise PermissionDenied
    album.manually_locked_at = timezone.now()
    album.unlocked_at = None
    album.save(update_fields=["manually_locked_at", "unlocked_at"])
    return redirect("photo_album:detail", album.pk)


@login_required
@require_POST
def unlock_album(request: HttpRequest, pk: int) -> HttpResponse:
    album = get_object_or_404(Album, pk=pk)
    if not access.can_manage_media(request):
        raise PermissionDenied
    if album.is_locked_by_ancestor:
        locking = album.locked_by
        holder = locking.name if locking else "et overalbum"
        messages.error(request, f"Albummet er låst, fordi {holder} er låst. Lås det op dér.")
        return redirect("photo_album:detail", album.pk)
    if not access.can_unlock_album(request, album):
        messages.error(request, "Albummet har været låst i mere end 6 måneder og kan ikke låses op.")
        return redirect("photo_album:detail", album.pk)
    album.manually_locked_at = None
    album.unlocked_at = timezone.now()
    album.save(update_fields=["manually_locked_at", "unlocked_at"])
    return redirect("photo_album:detail", album.pk)


# --- Bins ---------------------------------------------------------------------------------------


def _render_bin(request: HttpRequest, media: QuerySet[Media], album: Album | None) -> HttpResponse:
    media_items = list(media.filter(deleted_at__isnull=False).select_related("album").order_by("-deleted_at"))
    paths = _album_paths({item.album_id: item.album for item in media_items}.values())
    for item in media_items:
        item.can_permanently_delete = access.can_permanently_delete(item, request)  # type: ignore[attr-defined]
        item.album_path = paths.get(item.album_id, "")  # type: ignore[attr-defined]
    return render(request, "photo_album/bin.html", {"media": media_items, "album": album})


@login_required
def bin(request: HttpRequest) -> HttpResponse:
    """Every album's bin together — administrators only. Fotogruppen works one album at a time."""
    if not access.can_view_all_bins(request):
        raise PermissionDenied
    return _render_bin(request, Media.objects.all(), None)


@login_required
def album_bin(request: HttpRequest, pk: int) -> HttpResponse:
    """The bin of one album and everything under it."""
    if not access.can_view_album_bin(request):
        raise PermissionDenied
    album = get_object_or_404(Album, pk=pk)
    return _render_bin(request, album.subtree_media(), album)


def _bin_url(media: Media) -> str:
    return reverse("photo_album:album_bin", args=[media.album_id])


# --- Downloads ----------------------------------------------------------------------------------


def _queue_download(
    request: HttpRequest, *, album: Album | None, media_ids: list[int], variant: str, name: str
) -> HttpResponse:
    """Queue a private ZIP build, unless the very same one is already on its way or ready.

    A whole-album ZIP is slow enough to invite a second click, which used to queue a second complete
    archive and leave a second object in the bucket. The same files in the same variant are the same
    download, so that is reported rather than restarted.
    """
    resident = current_resident(request)
    wanted = sorted(media_ids)
    now = timezone.now()
    recent = AlbumDownload.objects.filter(
        requested_by=resident,
        album=album,
        variant=variant,
        state__in=(AlbumDownloadState.QUEUED, AlbumDownloadState.BUILDING, AlbumDownloadState.READY),
    )[:20]
    for existing in recent:
        if sorted(existing.media_ids) != wanted:
            continue
        if existing.state == AlbumDownloadState.READY:
            if existing.expires_at is not None and existing.expires_at > now:
                messages.info(request, "Den download ligger allerede klar til dig.")
                return redirect("photo_album:downloads")
            continue
        messages.info(request, "Din download er allerede ved at blive klargjort.")
        return redirect("photo_album:downloads")
    download = AlbumDownload.objects.create(
        album=album, name=name, requested_by=resident, media_ids=wanted, variant=variant
    )
    from .tasks import build_album_download

    def enqueue() -> None:
        result = build_album_download.delay(download.pk)
        AlbumDownload.objects.filter(pk=download.pk).update(task_id=result.id)

    transaction.on_commit(enqueue)
    messages.success(request, "Download bliver klargjort. Du finder den her, når den er klar.")
    return redirect("photo_album:downloads")


@login_required
@require_POST
def download_album(request: HttpRequest, pk: int) -> HttpResponseBase:
    """Queue a ZIP of everything in this album and its sub-albums the resident may see."""
    album = get_object_or_404(Album, pk=pk)
    form = DownloadForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("unknown variant")
    media_ids = list(access.visible_media(request, album, subtree=True).values_list("pk", flat=True))
    if not media_ids:
        messages.error(request, "Albummet har ingen medier, du kan hente.")
        return redirect("photo_album:detail", pk=album.pk)
    return _queue_download(
        request, album=album, media_ids=media_ids, variant=form.cleaned_data["variant"], name=album.name
    )


@login_required
def downloads(request: HttpRequest) -> HttpResponse:
    """The resident's own ZIP downloads: building, ready to fetch, or failed."""
    jobs = list(
        AlbumDownload.objects.filter(requested_by=current_resident(request)).select_related("album")[:50]
    )
    return render(
        request,
        "photo_album/downloads.html",
        {
            "downloads": jobs,
            "in_flight": any(
                job.state in (AlbumDownloadState.QUEUED, AlbumDownloadState.BUILDING) for job in jobs
            ),
        },
    )


@login_required
def download_album_archive(request: HttpRequest, token: str) -> HttpResponseBase:
    """Authorize the requesting resident and redirect their ready ZIP to object storage."""
    download = get_object_or_404(AlbumDownload, token=token, requested_by=current_resident(request))
    if download.state != AlbumDownloadState.READY or not download.archive_key:
        raise Http404("download is not ready")
    filename = download.filename
    storage = get_photo_album_storage()
    if isinstance(storage, PhotoAlbumS3Storage):
        return HttpResponseRedirect(
            storage.signed_url(
                download.archive_key,
                parameters={
                    "ResponseContentDisposition": content_disposition(filename),
                    "ResponseContentType": "application/zip",
                },
            )
        )
    return FileResponse(storage.open(download.archive_key, "rb"), as_attachment=True, filename=filename)


# --- Selections ---------------------------------------------------------------------------------


@login_required
@require_POST
def selection(request: HttpRequest) -> HttpResponse:
    """One action on several selected items: download them, approve, reject or delete them.

    Each item is checked on its own, exactly as the single-item action would check it; items the
    reader may not act on are left alone and counted, never a reason to refuse the rest.
    """
    ids = sorted({int(value) for value in request.POST.getlist("media") if value.isdecimal()})
    action = request.POST.get("action", "")
    album = None
    if (album_pk := request.POST.get("album", "")).isdecimal():
        album = Album.objects.filter(pk=int(album_pk)).first()
    default = reverse("photo_album:detail", args=[album.pk]) if album else reverse("photo_album:timeline")
    back = _safe_next(request, default)
    if not ids:
        messages.error(request, "Du har ikke valgt nogen medier.")
        return redirect(back)
    resident = current_resident(request)

    if action == "download":
        form = DownloadForm(request.POST)
        if not form.is_valid():
            return HttpResponseBadRequest("unknown variant")
        visible = access.visible_media(request).filter(pk__in=ids).select_related("album")
        chosen = list(visible)
        if not chosen:
            messages.error(request, "Ingen af de valgte medier kan hentes.")
            return redirect(back)
        # Rooted at the album the selection was made in when every item is inside it, so its
        # sub-albums become folders; otherwise every file goes under its full album path.
        if album is not None and not all(item.album.path.startswith(album.path) for item in chosen):
            album = None
        return _queue_download(
            request,
            album=album,
            media_ids=[item.pk for item in chosen],
            variant=form.cleaned_data["variant"],
            name=f"{album.name} (udvalg)" if album else "Fotoalbum (udvalg)",
        )

    if action in ("approve", "reject"):
        if not access.can_manage_media(request):
            raise PermissionDenied
        pending = list(Media.objects.filter(pk__in=ids, deleted_at__isnull=True, status=MediaStatus.PENDING))
        for media in pending:
            (services.approve if action == "approve" else services.reject)(media, resident)
        verb = "godkendt" if action == "approve" else "afvist"
        messages.success(request, f"{len(pending)} medier er {verb}.")
        if skipped := len(ids) - len(pending):
            messages.info(
                request, f"{skipped} af de valgte afventede ikke godkendelse og blev sprunget over."
            )
        return redirect(back)

    if action == "delete":
        done = 0
        items = list(Media.objects.filter(pk__in=ids).select_related("album"))
        for media in items:
            if access.can_withdraw(media, request):
                services.permanently_delete(media)
            elif access.can_curate_delete(media, request):
                services.delete(media, resident)
            else:
                continue
            done += 1
        messages.success(request, f"{done} medier er fjernet.")
        if skipped := len(ids) - done:
            messages.info(request, f"{skipped} af de valgte kunne du ikke fjerne, og de er urørte.")
        return redirect(back)

    return HttpResponseBadRequest("unknown action")


# --- ZIP import ---------------------------------------------------------------------------------


def import_zip(request: HttpRequest) -> HttpResponse:
    resident = current_resident(request) if request.user.is_authenticated else None
    if resident is None:
        return redirect_to_login(request.get_full_path())
    if not access.can_create_album(request):
        raise PermissionDenied
    form = ZipAlbumImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        album_import = _queue_import(form, resident)
        result_url = f"{reverse('photo_album:import_zip')}?job={album_import.token}"
        if _wants_json(request):
            return JsonResponse(
                {
                    "statusUrl": reverse("photo_album:import_zip_status", args=[album_import.token]),
                    "resultUrl": result_url,
                },
                status=202,
            )
        return redirect(result_url)
    job = None
    albums: list[Album] = []
    if token := request.GET.get("job"):
        job = get_object_or_404(AlbumImport, token=token, requested_by=resident)
        if job.state == AlbumImportState.READY:
            albums = list(Album.objects.filter(pk__in=job.album_ids).order_by("path"))
    return render(
        request,
        "photo_album/import_zip.html",
        {
            "zip_form": form,
            "job": job,
            "albums": albums,
            "skipped": job.skipped if job and job.state == AlbumImportState.READY else [],
            "errors": job.errors if job and job.state == AlbumImportState.FAILED else [],
            "folder_options": album_folder_options(),
        },
    )


def _queue_import(form: ZipAlbumImportForm, resident: Resident) -> AlbumImport:
    archive = form.cleaned_data["archive"]
    album_import = AlbumImport.objects.create(
        requested_by=resident,
        folder=form.cleaned_data["folder"],
        archive_name=archive.name,
        archive=archive,
    )
    from .tasks import process_album_import

    def enqueue() -> None:
        result = process_album_import.delay(album_import.pk)
        AlbumImport.objects.filter(pk=album_import.pk).update(task_id=result.id)

    transaction.on_commit(enqueue)
    return album_import


@csrf_exempt
@require_POST
def system_import_zip(request: HttpRequest) -> JsonResponse:
    resident = _token_import_resident(request)
    if resident is None:
        raise PermissionDenied
    form = ZipAlbumImportForm(request.POST, request.FILES)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
    album_import = _queue_import(form, resident)
    return JsonResponse(
        {
            "statusUrl": reverse("photo_album:import_zip_status", args=[album_import.token]),
            "resultUrl": f"{reverse('photo_album:import_zip')}?job={album_import.token}",
        },
        status=202,
    )


@require_GET
def system_albums(request: HttpRequest) -> JsonResponse:
    """The root albums of one folder, for the import script to tell what is already there."""
    if _token_import_resident(request) is None:
        raise PermissionDenied
    roots = Album.objects.filter(parent__isnull=True, folder=request.GET.get("folder", "")).order_by("name")
    return JsonResponse(
        {
            "albums": [
                {"id": album.pk, "name": album.name, "media": album.subtree_media().count()}
                for album in roots
            ]
        }
    )


def import_zip_status(request: HttpRequest, token: str) -> JsonResponse:
    resident = _token_import_resident(request) or (
        current_resident(request) if request.user.is_authenticated else None
    )
    if resident is None:
        return JsonResponse({"detail": "Authentication required."}, status=401)
    album_import = get_object_or_404(AlbumImport, token=token, requested_by=resident)
    albums = list(Album.objects.filter(pk__in=album_import.album_ids).order_by("path"))
    paths = _album_paths(albums)
    return JsonResponse(
        {
            "state": album_import.state,
            "error": album_import.error,
            "errors": album_import.errors,
            "skipped": album_import.skipped,
            "albums": [
                {
                    "id": album.pk,
                    "path": paths[album.pk],
                    "url": reverse("photo_album:detail", args=[album.pk]),
                }
                for album in albums
            ],
        }
    )


# --- Uploads ------------------------------------------------------------------------------------


def _file_modified_at(value: str | None) -> datetime | None:
    """A browser's File.lastModified (milliseconds since the epoch), if it is a believable one."""
    if not value or not value.isdecimal():
        return None
    try:
        moment = datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    # Browsers report "now" for a file with no date of its own; neither that nor the future means much.
    if moment.year < 1990 or moment > timezone.now() + timedelta(days=1):
        return None
    return moment


@login_required
@require_POST
def upload(request: HttpRequest, pk: int) -> HttpResponse:
    album = get_object_or_404(Album, pk=pk)
    if not access.can_upload(request, album):
        raise PermissionDenied
    form = MediaUploadForm(request.POST, request.FILES)
    wants_json = _wants_json(request)
    if not form.is_valid():
        if wants_json:
            return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
        # Without this the redirect below looked exactly like a successful upload and the photos
        # simply were not there.
        for error in form.errors.get("uploads", ["Filerne kunne ikke uploades."]):
            messages.error(request, str(error))
        return redirect("photo_album:detail", album.pk)
    resident = current_resident(request)
    modified = request.POST.getlist("last_modified")
    uploaded = 0
    skipped: list[str] = []
    warnings: list[str] = []
    for index, uploaded_file in enumerate(form.cleaned_data["uploads"]):
        try:
            outcome = services.store_upload(
                album=album,
                uploaded_file=uploaded_file,
                resident=resident,
                title=form.cleaned_data["title"],
                approved=access.can_manage_media(request),
                file_modified_at=_file_modified_at(modified[index] if index < len(modified) else None),
            )
        except ValueError as exc:  # locked between the check above and now
            if wants_json:
                return JsonResponse({"errors": {"uploads": [{"message": str(exc)}]}}, status=409)
            messages.error(request, str(exc))
            return redirect("photo_album:detail", album.pk)
        name = uploaded_file.name
        if outcome.media is None:
            logger.info("Skipped duplicate upload %r to album %s by resident %s", name, album.pk, resident.pk)
            skipped.append(f"{name} findes allerede i albummet og blev ikke uploadet igen.")
            continue
        uploaded += 1
        if outcome.also_in:
            paths = _album_paths(outcome.also_in)
            warnings.append(f"{name} findes også i {', '.join(sorted(paths.values()))}.")
    if wants_json:
        return JsonResponse({"uploaded": uploaded, "skipped": skipped, "warnings": warnings}, status=201)
    for note in [*skipped, *warnings]:
        messages.warning(request, note)
    return redirect("photo_album:detail", album.pk)


# --- One item -----------------------------------------------------------------------------------


def _is_visible(request: HttpRequest, media: Media) -> bool:
    """Whether this request may look at `media` — the one rule both the viewer and serve_media use."""
    if access.can_manage_media(request):
        return True
    return access.visible_media(request, media.album).filter(pk=media.pk).exists()


def _viewable_media(request: HttpRequest, pk: int) -> Media:
    """One media item this request is allowed to look at, or 404/403.

    Same rule as `download_original`, which is the point: the viewer's details and the original it
    offers to download must agree about who may see what.
    """
    media = get_object_or_404(Media.objects.select_related("album", "requested_by"), pk=pk)
    if not _is_visible(request, media):
        raise PermissionDenied
    return media


def _view_counts(media: Media) -> dict[str, int]:
    counts = media.views.aggregate(unique=Count("pk"), total=Coalesce(Sum("count"), 0))
    return {"uniqueViews": counts["unique"], "totalViews": counts["total"]}


def _map_url(location: str) -> str:
    """An OpenStreetMap link for the "lat, lon" services.extract_metadata stores as "Sted"."""
    try:
        latitude, longitude = (float(part) for part in location.split(","))
    except ValueError:
        return ""
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return ""
    return f"https://www.openstreetmap.org/?mlat={latitude:.6f}&mlon={longitude:.6f}#map=16/{latitude:.6f}/{longitude:.6f}"


@login_required
def media_detail(request: HttpRequest, pk: int) -> JsonResponse:
    """Everything the viewer shows for one item, fetched when it is opened.

    NOT rendered into the grid. A tile used to carry the HD url, the download url, the uploader,
    both timestamps, the delete url and the whole EXIF blob as data- attributes, so opening an
    album shipped all of that for every photograph in it — hundreds of times over for a party
    album, to show one of them. The grid keeps only what it draws with: the thumbnail, the title,
    and the two badges.
    """
    media = _viewable_media(request, pk)
    can_delete = access.can_delete(media, request)
    album_url = reverse("photo_album:detail", args=[media.album_id])
    return JsonResponse(
        {
            "kind": "video" if media.content_type.startswith("video/") else "image",
            "title": media.title,
            "filename": media.display_name,
            "full": media.high_definition.url if media.has_derivatives else "",
            "album": " / ".join(media.album.display_path()),
            "albumUrl": album_url,
            "link": f"{album_url}?media={media.pk}",
            "downloadUrl": reverse("photo_album:download_original", args=[media.pk]),
            "deleteUrl": reverse("photo_album:delete", args=[media.pk]) if can_delete else "",
            "viewUrl": reverse("photo_album:record_view", args=[media.pk]),
            "mapUrl": _map_url(str(media.metadata.get("Sted", ""))),
            "uploadedBy": media.requested_by.full_name,
            "uploadedAt": _formatted(media.added_at),
            "capturedAt": _formatted(media.captured_at),
            "metadata": media.metadata,
            **_view_counts(media),
        }
    )


@login_required
@require_POST
def record_view(request: HttpRequest, pk: int) -> JsonResponse:
    """The viewer reports here once the full-size picture (or the video) has actually loaded.

    Explicit rather than inferred from serve_media, which also answers prefetches and thumbnails.
    """
    media = _viewable_media(request, pk)
    counted = services.record_view(media, current_resident(request))
    return JsonResponse({"counted": counted, **_view_counts(media)})


def _formatted(value: datetime | None) -> str:
    """The same wording the templates used to render, so the viewer reads identically."""
    return date_format(timezone.localtime(value), "j. F Y, H:i") if value else ""


def _managed_media(request: HttpRequest, pk: int) -> Media:
    if not access.can_manage_media(request):
        raise PermissionDenied
    return get_object_or_404(Media, pk=pk)


def _clean_key(path: str) -> str | None:
    """The storage key a request is really asking for, or None if it is not asking honestly.

    Same guard as core.media._clean, and for the same reason: normalising before anything else
    means `photo-album/../etc/passwd` collapses to the ordinary (unmatched) key `etc/passwd` rather
    than tripping the storage's own safe_join check further down — which a URL dispatcher can't rely
    on alone, since it never runs against the untouched path.
    """
    if "\\" in path:
        return None
    name = posixpath.normpath("/" + path).lstrip("/")
    if not name or name == "." or name.startswith("../"):
        return None
    return name


def _media_for_key(key: str) -> Media | None:
    """The row a storage key belongs to.

    Current keys carry the item's uuid (`photo-album/<album>/<uuid>/<variant>/<name>`), an indexed
    lookup; the key must then really be one of that row's three files. Keys from before the uuid
    (`photo-album/<album>/<variant>/<name>`) fall back to matching all three file columns.
    """
    media_rows = Media.objects.select_related("album", "requested_by")
    parts = key.split("/")
    if len(parts) >= 5 and parts[0] == "photo-album":
        try:
            uuid = UUID(parts[2])
        except ValueError:
            pass
        else:
            media = media_rows.filter(uuid=uuid).first()
            if media is not None and key in (
                media.original.name,
                media.high_definition.name,
                media.thumbnail.name,
            ):
                return media
            return None
    return media_rows.filter(Q(original=key) | Q(high_definition=key) | Q(thumbnail=key)).first()


def serve_media(request: HttpRequest, path: str) -> HttpResponseBase:
    """/photo-album/<path> — the only way a browser ever reaches a photo-album object.

    This is what `photo_album.storage.PhotoAlbumS3Storage.url()` points at instead of a bare
    presigned URL: a presigned URL is a bearer token, good for an hour with no further check, and
    `photo_album.access.visible_media` has a rule a bearer token can't enforce — a PENDING upload is
    visible only to its uploader and Fotogruppen. So every request re-derives the `Media` row from
    the key and re-checks that rule, exactly like `_viewable_media` does for the pk-keyed views,
    before redirecting to a freshly presigned URL. Same shape as core.media.serve_media, checking
    this app's own rule instead of just "is logged in".
    """
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    key = _clean_key(path)
    if key is None:
        raise Http404("not a photo-album media path")
    media = _media_for_key(key)
    if media is None:
        raise Http404("no such photo-album media")
    if not _is_visible(request, media):
        raise PermissionDenied

    storage = media.original.storage
    signer = getattr(storage, "signed_url", None)
    if signer is not None:
        try:
            target = signer(key)
        except SuspiciousOperation as exc:
            raise Http404("media path outside the storage root") from exc
        response = HttpResponseRedirect(target)
        response.headers["Cache-Control"] = REDIRECT_CACHE_CONTROL
        response.headers["Vary"] = "Cookie"
        return response

    # Local disk, the test suite's own storage (see photo_album.storage) — stream, same as
    # core.media.serve_media's non-S3 branch.
    try:
        handle = storage.open(key)
    except (FileNotFoundError, IsADirectoryError, PermissionError, ValueError, SuspiciousOperation) as exc:
        raise Http404("photo-album media not found") from exc
    return FileResponse(handle)


@login_required
def download_original(request: HttpRequest, pk: int) -> HttpResponseBase:
    """Redirect straight to the bucket when the original is on S3, rather than streaming it here.

    A video is up to PHOTO_ALBUM_VIDEO_MAX_MB (1 GB): reading that through the app server on its way from
    S3 to the browser ties up one of a handful of sync workers for as long as the slowest client's
    connection takes, for no reason — the bytes never need to pass through this process at all. The
    streaming branch below only runs under the test suite, which forces plain FileSystemStorage
    (tests/conftest.py) precisely so it never has to talk to a real bucket.

    The file is offered under the exact name it was uploaded with, not the storage key's copy.
    """
    media = _viewable_media(request, pk)
    original_name = media.original.name or "original"
    filename = media.display_name or "original"
    storage = media.original.storage
    if isinstance(storage, PhotoAlbumS3Storage):
        params = {"ResponseContentDisposition": content_disposition(filename)}
        if media.content_type:
            params["ResponseContentType"] = media.content_type
        return HttpResponseRedirect(storage.signed_url(original_name, parameters=params))
    return FileResponse(
        media.original.open("rb"),
        as_attachment=True,
        filename=filename,
        content_type=media.content_type or None,
    )


def _album_url(media: Media) -> str:
    return reverse("photo_album:detail", args=[media.album_id])


@login_required
@require_POST
def approve(request: HttpRequest, pk: int) -> HttpResponse:
    media = _managed_media(request, pk)
    services.approve(media, current_resident(request))
    return redirect(_safe_next(request, _album_url(media)))


@login_required
@require_POST
def reject(request: HttpRequest, pk: int) -> HttpResponse:
    media = _managed_media(request, pk)
    services.reject(media, current_resident(request))
    return redirect(_safe_next(request, _album_url(media)))


@login_required
@require_POST
def delete(request: HttpRequest, pk: int) -> HttpResponse:
    media = get_object_or_404(Media.objects.select_related("album"), pk=pk)
    if not access.can_delete(media, request):
        raise PermissionDenied
    back = _safe_next(request, _album_url(media))
    requester = current_resident(request)
    # Asked of access rather than re-derived here. This branch used to restate can_delete's own
    # "your own pending upload" rule, in a second copy that spelled the status as a bare string —
    # so the two could disagree about what a withdrawal is, and only one of them gated the action.
    if access.can_withdraw(media, request):
        services.permanently_delete(media)
    else:
        services.delete(media, requester)
    return redirect(back)


@login_required
@require_POST
def restore(request: HttpRequest, pk: int) -> HttpResponse:
    media = _managed_media(request, pk)
    services.restore(media)
    return redirect(_safe_next(request, _bin_url(media)))


@login_required
@require_POST
def permanently_delete(request: HttpRequest, pk: int) -> HttpResponse:
    media = _managed_media(request, pk)
    if not access.can_permanently_delete(media, request):
        raise PermissionDenied
    back = _safe_next(request, _bin_url(media))
    services.permanently_delete(media)
    return redirect(back)
