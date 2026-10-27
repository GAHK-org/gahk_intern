import logging
import mimetypes
import posixpath
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import compare_digest
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.core.files.base import File
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
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from arkiv.storage import content_disposition
from core.media import REDIRECT_CACHE_CONTROL
from residents.models import Resident
from residents.permissions import current_resident

from . import access, cache, direct_upload, featured, merge, services
from .forms import (
    AlbumForm,
    AlbumMergeForm,
    AlbumNameForm,
    DirectUploadSignForm,
    DirectZipImportForm,
    DownloadForm,
    ImportUploadForm,
    MediaUploadBeginForm,
    MediaUploadCommitForm,
    MediaUploadForm,
    SystemAlbumRenameForm,
    SystemSidecarDatesForm,
    SystemZipImportForm,
    ZipAlbumImportForm,
)
from .models import (
    MAX_ALBUM_DEPTH,
    NO_YEAR_FOLDER,
    Album,
    AlbumDownload,
    AlbumDownloadState,
    AlbumImport,
    AlbumImportState,
    AlbumMerge,
    DateSource,
    DerivativeState,
    DownloadVariant,
    FileTimeSource,
    Media,
    MediaStatus,
    Tag,
    normalize_name,
)
from .storage import PhotoAlbumS3Storage, get_photo_album_storage
from .uploads import (
    IMAGE_EXTENSIONS,
    RAW_EXTENSIONS,
    UPLOAD_ACCEPT,
    VIDEO_EXTENSIONS,
    check_media_content,
    check_media_upload,
    content_type_for,
)

logger = logging.getLogger(__name__)

# Tiles per page of the album grid and the timeline. Pages are loaded in both directions as the
# reader scrolls, and the frontend drops pages far off screen again, so this only sets the step.
PAGE_SIZE = 60
# A cached derivative's key never changes meaning (photo_album.cache), so the browser may keep it a
# while; `private` because it was served past an access check, like core.media's redirects.
CACHED_FILE_CACHE_CONTROL = "private, max-age=86400"


def album_folder_options() -> list[str]:
    """The folders to offer: the years that have albums and this year's, newest first, then
    NO_YEAR_FOLDER — as the album index lists them."""
    folders = set(Album.objects.order_by("folder").values_list("folder", flat=True).distinct())
    years = folders - {NO_YEAR_FOLDER} | {default_folder()}
    return [*sorted(years, reverse=True), NO_YEAR_FOLDER]


def default_folder() -> str:
    """This year's: where a new album most likely belongs."""
    return str(timezone.localdate().year)


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
    folder = normalize_name(request.GET.get("folder", ""))
    albums = Album.objects.filter(parent__isnull=True)
    if folder:
        albums = albums.filter(folder=folder)
    # The years newest first, then NO_YEAR_FOLDER.
    ordered = sorted(albums.order_by("name"), key=lambda album: _negated(album.folder))
    _summarise(ordered)
    return render(
        request,
        "photo_album/index.html",
        {
            "albums": ordered,
            "can_create": access.can_create_album(request),
            "can_view_all_bins": access.can_view_all_bins(request),
            "folder": folder,
            # The timeline's tile leads the full index; a folder page keeps the plain link.
            "mosaic": [] if folder else featured.timeline_mosaic(),
        },
    )


def _negated(folder: str) -> tuple[bool, int]:
    return (False, -int(folder)) if folder.isdecimal() else (True, 0)


@login_required
def timeline(request: HttpRequest) -> HttpResponse:
    """Everything the reader may see, newest first, across every album — or only what carries every
    `?tag=` given, on the item itself or on an album above it.
    """
    visible = access.visible_media(request)
    names = list(dict.fromkeys(name for name in request.GET.getlist("tag") if name))
    for name in names:
        tag = Tag.objects.filter(name__iexact=name).first()
        visible = services.tagged_media(visible, tag) if tag else visible.none()
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
            "tags": [
                {"name": name, "remove_url": _timeline_url([other for other in names if other != name])}
                for name in names
            ],
            "tag_query": urlencode([("tag", name) for name in names]),
            **context,
        },
    )


def _timeline_url(tags: Iterable[str]) -> str:
    query = urlencode([("tag", name) for name in tags])
    return f"{reverse('photo_album:timeline')}?{query}" if query else reverse("photo_album:timeline")


def _tag_entries(own: Iterable[Tag], lineage: Iterable[Album]) -> list[dict[str, object]]:
    """Tags as the album page and the viewer show them: the item's own, then those it gets from the
    albums in `lineage` (root first), each linking to the timeline filtered on it.
    """
    entries: dict[str, dict[str, object]] = {}
    for tag in sorted(own, key=lambda tag: tag.name.lower()):
        entries[tag.name.lower()] = {"name": tag.name, "inherited": False, "from": ""}
    albums = {album.pk: album for album in lineage}
    rows = Album.tags.through.objects.filter(album_id__in=albums).select_related("tag")
    order = list(albums)
    for row in sorted(rows, key=lambda row: (order.index(row.album_id), row.tag.name.lower())):
        entries.setdefault(
            row.tag.name.lower(), {"name": row.tag.name, "inherited": True, "from": albums[row.album_id].name}
        )
    for entry in entries.values():
        entry["url"] = _timeline_url([str(entry["name"])])
    return list(entries.values())


@login_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    album = get_object_or_404(Album, pk=pk)
    include_sub_albums = request.GET.get("alle") == "1"
    visible = access.visible_media(request, album, subtree=include_sub_albums)
    media_page, fragment = _page(request, visible)
    can_manage = access.can_manage_media(request)
    can_tag = access.can_tag(request)
    context = {"can_manage": can_manage, "album": album}
    if fragment:
        return _tiles_response(request, media_page, **context)

    children = list(album.children.order_by("name"))
    _summarise(children)
    resident = current_resident(request)
    merge_job = album.active_merge()
    can_merge = merge_job is None and access.can_merge_album(request, album)
    if merge_job is None and access.is_administrator(request):
        # How the administrator's own merge into this album went, for a day after.
        merge_job = (
            album.merges.filter(
                requested_by=resident, completed_at__gte=timezone.now() - MERGE_NOTICE_SHOWN_FOR
            )
            .select_related("target")
            .first()
        )
    return render(
        request,
        "photo_album/detail.html",
        {
            **context,
            "album_tags": _tag_entries(album.tags.all(), album.ancestors()),
            "album_tag_url": reverse("photo_album:album_tags", args=[album.pk]) if can_tag else "",
            "can_tag": can_tag,
            "page": media_page,
            "children": children,
            "ancestors": album.ancestors(),
            "include_sub_albums": include_sub_albums,
            "has_subtree_media": bool(media_page.items)
            or access.visible_media(request, album, subtree=True).exists(),
            "album_locked": album.is_locked(),
            "locked_by": album.locked_by,
            "can_upload": access.can_upload(request, album),
            "upload_accept": UPLOAD_ACCEPT,
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
            "merge_job": merge_job,
            "merge_summary": merge.describe(merge_job.result) if merge_job is not None else "",
            "merge_target": merge.label(merge_job.target) if merge_job is not None else "",
            "merge_options": merge.merge_candidates(album) if can_merge else [],
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
            "default_folder": default_folder(),
        },
    )


def _form_error_messages(request: HttpRequest, form: AlbumNameForm | AlbumMergeForm) -> None:
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
        services.rename_album(album, form.cleaned_data["name"], by=current_resident(request))
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


# How long the album page tells the administrator who merged another album into it how that went.
MERGE_NOTICE_SHOWN_FOR = timedelta(days=1)


@login_required
def merge_album(request: HttpRequest, pk: int) -> HttpResponse:
    """Merge the album chosen into this one, which is kept: first a page saying what will happen —
    which album goes, which stays, and where each sub-album ends up — then, posted from it, the
    merge itself, in the background (photo_album.merge)."""
    target = get_object_or_404(Album, pk=pk)
    if not access.can_merge_album(request, target):
        raise PermissionDenied
    form = AlbumMergeForm(request.POST if request.method == "POST" else request.GET)
    if not form.is_valid():
        _form_error_messages(request, form)
        return redirect("photo_album:detail", target.pk)
    source = form.cleaned_data["source"]
    if request.method != "POST":
        try:
            merge_plan = merge.plan(target, source)
        except ValidationError as exc:
            _error_messages(request, exc)
            return redirect("photo_album:detail", target.pk)
        return render(
            request,
            "photo_album/merge_confirm.html",
            {
                "target": target,
                "source": source,
                "target_label": merge.label(target),
                "source_label": merge.label(source),
                "target_contents": merge.contents(target),
                "source_contents": merge.contents(source),
                "merged": [
                    (merged.display_path(), kept.display_path()) for kept, merged in merge_plan.pairs[1:]
                ],
                "moved": [(album.name, under.display_path()) for album, under in merge_plan.moves],
            },
        )
    try:
        job = merge.request_merge(target, source, by=current_resident(request))
    except ValidationError as exc:
        _error_messages(request, exc)
    else:
        kept = merge.label(target)
        messages.success(
            request,
            f"{job.source_path} flettes ind i {kept}. {kept} beholdes, og {job.source_path} forsvinder, "
            "når fletningen er færdig.",
        )
    return redirect("photo_album:detail", target.pk)


@login_required
@require_GET
def merge_status(request: HttpRequest, pk: int) -> HttpResponse:
    """The album page's merge notice, which asks again until the merge is done, and then reloads
    the page — the merged album's, as the other one is gone."""
    job = get_object_or_404(AlbumMerge.objects.select_related("target"), pk=pk)
    if job.is_active:
        return render(
            request,
            "photo_album/_merge_notice.html",
            {"merge_job": job, "merge_target": merge.label(job.target)},
        )
    response = HttpResponse(status=204)
    response["HX-Redirect"] = reverse("photo_album:detail", args=[job.target_id])
    return response


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
    """Queue a ZIP of everything in this album the resident may see — with its sub-albums, unless
    the resident chose to leave them out."""
    album = get_object_or_404(Album, pk=pk)
    form = DownloadForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("unknown variant")
    subtree = form.cleaned_data["sub_albums"]
    media_ids = list(access.visible_media(request, album, subtree=subtree).values_list("pk", flat=True))
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
        # One Album instance per album, so its lock is worked out once rather than once per item
        # (each select_related copy has its own cache). Every item still gets its own checks.
        albums = {media.album_id: media.album for media in items}
        for media in items:
            media.album = albums[media.album_id]
            if access.can_withdraw(media, request):
                services.permanently_delete(media)
            elif access.can_curate_delete(media, request):
                services.delete(media, resident)
            else:
                continue
            for album in albums.values():
                album.media_removed(media)
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
    # With a ticket, the archive is already in the bucket (import_zip_upload), not in the request.
    direct = request.method == "POST" and "ticket" in request.POST
    form = (
        DirectZipImportForm(request.POST)
        if direct
        else ZipAlbumImportForm(request.POST or None, request.FILES or None)
    )
    if request.method == "POST" and form.is_valid():
        try:
            stored = (
                _finished_import_upload(form, current_resident(request).pk)
                if isinstance(form, DirectZipImportForm)
                else None
            )
        except ValidationError as exc:
            return JsonResponse({"errors": {"archive": [{"message": m} for m in exc.messages]}}, status=400)
        album_import = _queue_import(form, resident, stored=stored)
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
            "default_folder": default_folder(),
            "max_sub_album_levels": MAX_ALBUM_DEPTH,
        },
    )


@login_required
@require_POST
def import_zip_upload(request: HttpRequest) -> JsonResponse:
    """Where the import page is to send its ZIP: straight to the bucket in parts, past the proxy
    that cuts an upload after a minute (photo_album.direct_upload); `{"direct": false}` without one."""
    if not access.can_create_album(request):
        raise PermissionDenied
    return _begin_import_upload(request, browser=True)


def _begin_import_upload(request: HttpRequest, *, browser: bool) -> JsonResponse:
    """The import script's upload is set up here and its parts presigned; a browser's is driven by
    Uppy through direct_upload_sign, so it only gets its ticket."""
    form = ImportUploadForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
    name, size = form.cleaned_data["name"], form.cleaned_data["size"]
    key, purpose, content_type = direct_upload.import_key(name), direct_upload.IMPORT, "application/zip"
    upload = (
        direct_upload.ticket(
            key,
            size,
            purpose=purpose,
            owner=current_resident(request).pk,
            content_type=content_type,
            name=name,
        )
        if browser
        else direct_upload.begin(key, size, purpose=purpose, content_type=content_type, name=name)
    )
    return JsonResponse({"direct": False} if upload is None else {"direct": True, **upload})


def _finished_import_upload(form: DirectZipImportForm, owner: int | None) -> tuple[str, str]:
    """(key, name) of the archive a DirectZipImportForm's ticket sent to the bucket."""
    upload = direct_upload.finish(
        form.cleaned_data["ticket"],
        form.cleaned_data["parts"] or None,
        purpose=direct_upload.IMPORT,
        owner=owner,
    )
    return upload["key"], upload["name"]


@login_required
@require_POST
def direct_upload_sign(request: HttpRequest) -> JsonResponse:
    """Uppy's signer (@uppy/aws-s3 `signRequest`): a presigned URL for one request of an upload this
    resident was given a ticket for. See photo_album.direct_upload.sign for what it will sign."""
    form = DirectUploadSignForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
    try:
        signed = direct_upload.sign(
            form.cleaned_data["ticket"],
            owner=current_resident(request).pk,
            method=form.cleaned_data["method"],
            key=form.cleaned_data["key"],
            upload_id=form.cleaned_data["upload_id"],
            part_number=form.cleaned_data["part_number"],
        )
    except ValidationError as exc:
        return _upload_errors(*exc.messages)
    return JsonResponse(signed)


def _queue_import(
    form: ZipAlbumImportForm | DirectZipImportForm,
    resident: Resident,
    *,
    stored: tuple[str, str] | None = None,
    lock: bool = False,
    merge: bool = False,
) -> AlbumImport:
    """Queue the import of the form's archive, or with `stored`, (key, name), of one already sent
    to the bucket (photo_album.direct_upload)."""
    archive, archive_name = stored or (form.cleaned_data["archive"], form.cleaned_data["archive"].name)
    try:
        album_import = AlbumImport.objects.create(
            requested_by=resident,
            folder=form.cleaned_data["folder"],
            archive_name=archive_name,
            archive=archive,
            lock_when_done=lock,
            merge_into_existing=merge,
            album_name=form.cleaned_data.get("album", ""),
        )
    except Exception:
        # An archive already in the bucket that no import will ever read: without this, every
        # attempt of the Dropbox script left a whole album behind (the ~1 GB ZIP of the album whose
        # key was too long for the column, once per try).
        if stored is not None:
            try:
                direct_upload.delete(stored[0])
            except Exception:  # the error that matters is the one being raised
                logger.warning(
                    "Could not delete %s after its import failed to start", stored[0], exc_info=True
                )
        raise
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
    # With a ticket, the archive is already in the bucket (system_import_upload), not in the request.
    direct = "ticket" in request.POST
    form = DirectZipImportForm(request.POST) if direct else SystemZipImportForm(request.POST, request.FILES)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
    try:
        stored = _finished_import_upload(form, None) if isinstance(form, DirectZipImportForm) else None
    except ValidationError as exc:
        return JsonResponse({"errors": {"archive": [{"message": m} for m in exc.messages]}}, status=400)
    # Everything the import script brings in is an archive of years gone by: lock it on arrival, so
    # it is not edited by accident. Fotogruppen can unlock an album that does need a change.
    # An album that already exists is added to rather than refused, so the script can send the files
    # an earlier attempt left out — once it has been unlocked; a locked album is still refused.
    album_import = _queue_import(form, resident, stored=stored, lock=True, merge=True)
    return JsonResponse(
        {
            "statusUrl": reverse("photo_album:import_zip_status", args=[album_import.token]),
            "resultUrl": f"{reverse('photo_album:import_zip')}?job={album_import.token}",
        },
        status=202,
    )


@csrf_exempt
@require_POST
def system_import_upload(request: HttpRequest) -> JsonResponse:
    """Where the import script is to send an archive of `size` bytes: straight to the bucket in parts
    (photo_album.direct_upload), or — `{"direct": false}`, without a bucket — with the import itself."""
    if _token_import_resident(request) is None:
        raise PermissionDenied
    return _begin_import_upload(request, browser=False)


@csrf_exempt
@require_POST
def system_rename_album(request: HttpRequest) -> JsonResponse:
    """The import script renaming a root album it imported — one the server named after a folder it
    descended into, say: Dropbox's "2014/GAHK Color Run" held only "101GOPRO/", and the album was
    called "101GOPRO". Locked or not, if the script imported it: services.rename_album decides."""
    resident = _token_import_resident(request)
    if resident is None:
        raise PermissionDenied
    form = SystemAlbumRenameForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
    folder, current = form.cleaned_data["folder"], normalize_name(form.cleaned_data["album"])
    album = Album.objects.filter(parent__isnull=True, folder=folder, name__iexact=current).first()
    if album is None:
        return JsonResponse(
            {"errors": {"album": [{"message": f'Der er intet album "{current}" i {folder}.'}]}}, status=404
        )
    try:
        services.rename_album(album, form.cleaned_data["name"], by=resident)
    except ValidationError as exc:
        return JsonResponse(
            {"errors": {"name": [{"message": message} for message in exc.messages]}}, status=400
        )
    logger.info(
        "The import script renamed album %s in %s from %s to %s.", album.pk, folder, current, album.name
    )
    return JsonResponse({"id": album.pk, "folder": album.folder, "name": album.name})


@csrf_exempt
@require_POST
def system_sidecar_dates(request: HttpRequest) -> JsonResponse:
    """The import script's sidecars for an album it imported before the import read them
    (services.apply_sidecars). A few hundred kilobytes, so answered at once, not queued."""
    if _token_import_resident(request) is None:
        raise PermissionDenied
    form = SystemSidecarDatesForm(request.POST, request.FILES)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
    try:
        result = services.apply_sidecars(
            archive=form.cleaned_data["archive"],
            folder=form.cleaned_data["folder"],
            root_name=form.cleaned_data["album"],
        )
    except ValidationError as exc:
        return JsonResponse(
            {"errors": {"album": [{"message": message} for message in exc.messages]}}, status=400
        )
    return JsonResponse(
        {
            "stored": result.stored,
            "updated": result.updated,
            "unchanged": result.unchanged,
            "undated": result.undated,
            "unmatched": result.unmatched,
            "ambiguous": result.ambiguous,
            "kept": result.kept,
            "redated": result.redated,
            "againstCapture": result.against_capture,
        }
    )


@require_GET
def system_albums(request: HttpRequest) -> JsonResponse:
    """The root albums of one folder, for the import script to tell what is already there.

    Also the rules an archive is checked against, so the script can refuse an album from its
    Dropbox listing before downloading gigabytes the import would turn down. They are this server's
    own, so the script never carries a copy that drifts; the import itself still checks everything.
    The RAW extensions are among the image ones too, for a script from before RAW had a limit of
    its own; videos from the script have none (`videoMaxMb` null).
    """
    if _token_import_resident(request) is None:
        raise PermissionDenied
    folder = normalize_name(request.GET.get("folder", ""))
    roots = Album.objects.filter(parent__isnull=True, folder=folder).order_by("name")
    return JsonResponse(
        {
            "albums": [
                {"id": album.pk, "name": album.name, "media": album.subtree_media().count()}
                for album in roots
            ],
            "rules": {
                "imageExtensions": sorted(IMAGE_EXTENSIONS),
                "rawExtensions": sorted(RAW_EXTENSIONS),
                "videoExtensions": sorted(VIDEO_EXTENSIONS),
                "imageMaxMb": settings.PHOTO_ALBUM_IMAGE_MAX_MB,
                "rawMaxMb": settings.PHOTO_ALBUM_RAW_MAX_MB,
                "videoMaxMb": None,
                # Folder levels allowed below the album the archive becomes.
                "maxSubAlbumLevels": MAX_ALBUM_DEPTH,
                "macosMetadataMaxBytes": services.MACOS_METADATA_MAX_BYTES,
                "macosMetadataAnySize": sorted(services.MACOS_METADATA_ANY_SIZE),
            },
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
            "locked": bool(albums) and albums[0].is_locked(),
            "merged": album_import.merged,
            # {"step": "reading" | "checking" | "storing", "done", "total"} while it runs; {} before.
            "progress": album_import.progress if album_import.state == AlbumImportState.BUILDING else {},
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
                file_time_source=FileTimeSource.UPLOAD,
            )
        except ValueError as exc:  # locked between the check above and now
            if wants_json:
                return JsonResponse({"errors": {"uploads": [{"message": str(exc)}]}}, status=409)
            messages.error(request, str(exc))
            return redirect("photo_album:detail", album.pk)
        uploaded += _noted(outcome, uploaded_file.name, album, resident, skipped, warnings)
    if uploaded:
        services.settle_dates(album)
    if wants_json:
        return JsonResponse({"uploaded": uploaded, "skipped": skipped, "warnings": warnings}, status=201)
    for note in [*skipped, *warnings]:
        messages.warning(request, note)
    return redirect("photo_album:detail", album.pk)


def _noted(
    outcome: services.UploadOutcome,
    name: str,
    album: Album,
    resident: Resident,
    skipped: list[str],
    warnings: list[str],
) -> bool:
    """Note what the resident should read about one stored upload; returns whether it was stored."""
    if outcome.media is None:
        logger.info("Skipped duplicate upload %r to album %s by resident %s", name, album.pk, resident.pk)
        skipped.append(f"{name} findes allerede i albummet og blev ikke uploadet igen.")
        return False
    if outcome.also_in:
        paths = _album_paths(outcome.also_in)
        warnings.append(f"{name} findes også i {', '.join(sorted(paths.values()))}.")
    return True


def _upload_errors(*messages_: str, status: int = 400) -> JsonResponse:
    """The shape `upload` answers a refused file with, which the upload dialog reads."""
    return JsonResponse(
        {"errors": {"uploads": [{"message": message} for message in messages_]}}, status=status
    )


class _Downloaded(File):
    """A file sent straight to the bucket and read back to a local file to be checked, which
    ffmpeg can open where it lies (uploads.local_copy) instead of copying it once more."""

    content_type = ""

    def temporary_file_path(self) -> str:
        return str(getattr(self.file, "name", ""))


@login_required
@require_POST
def upload_begin(request: HttpRequest, pk: int) -> JsonResponse:
    """Where the upload dialog is to send a large photo or video: straight to the bucket, in parts,
    by Uppy through direct_upload_sign.

    It is checked by name, type and size before a byte is sent, as the upload form would; the bytes
    are checked in upload_commit. `{"direct": false}` without a bucket, and the file goes to
    `upload` as before. The key is the original's own, so committing stores nothing a second time.
    """
    album = get_object_or_404(Album, pk=pk)
    if not access.can_upload(request, album):
        raise PermissionDenied
    form = MediaUploadBeginForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
    name, size = normalize_name(Path(form.cleaned_data["name"]).name), form.cleaned_data["size"]
    if error := check_media_upload(
        SimpleNamespace(name=name, size=size, content_type=form.cleaned_data["content_type"])
    ):
        return _upload_errors(error)
    if not direct_upload.available():
        return JsonResponse({"direct": False})
    media = Media(album=album)
    key = Media._meta.get_field("original").generate_filename(media, name)
    upload = direct_upload.ticket(
        key,
        size,
        purpose=direct_upload.MEDIA,
        owner=current_resident(request).pk,
        content_type=content_type_for(name) or "application/octet-stream",
        album=album.pk,
        uuid=str(media.uuid),
        name=name,
    )
    return JsonResponse({"direct": True, **(upload or {})})


@login_required
@require_POST
def upload_commit(request: HttpRequest, pk: int) -> JsonResponse:
    """A photo or video Uppy sent to the bucket for upload_begin: checked and stored as `upload`
    would have.

    Read back from the bucket into a local file — inside Hetzner, so quickly — and through exactly
    the checks, the hashing and the duplicate test an upload through the form gets. A file that
    fails any of them is refused as loudly, and removed from the bucket again.
    """
    album = get_object_or_404(Album, pk=pk)
    if not access.can_upload(request, album):
        raise PermissionDenied
    form = MediaUploadCommitForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
    try:
        upload = direct_upload.finish(
            form.cleaned_data["ticket"], None, purpose=direct_upload.MEDIA, owner=current_resident(request).pk
        )
    except ValidationError as exc:
        return _upload_errors(*exc.messages)
    if upload["album"] != album.pk:
        direct_upload.delete(upload["key"])
        return _upload_errors("Filen blev sendt til et andet album.")
    resident = current_resident(request)
    skipped: list[str] = []
    warnings: list[str] = []
    with NamedTemporaryFile(prefix="photo-album-", suffix=Path(upload["name"]).suffix) as local:
        direct_upload.read_into(upload["key"], local)
        received = _Downloaded(local, name=upload["name"])
        received.content_type = content_type_for(upload["name"])
        if error := check_media_upload(received) or check_media_content(received):
            direct_upload.delete(upload["key"])
            return _upload_errors(error)
        try:
            outcome = services.store_upload(
                album=album,
                uploaded_file=received,
                resident=resident,
                title=form.cleaned_data["title"],
                approved=access.can_manage_media(request),
                file_modified_at=_file_modified_at(form.cleaned_data["last_modified"] or None),
                file_time_source=FileTimeSource.UPLOAD,
                stored=(upload["key"], UUID(upload["uuid"])),
            )
        except ValueError as exc:  # locked since it was begun
            direct_upload.delete(upload["key"])
            return _upload_errors(str(exc), status=409)
    stored = _noted(outcome, upload["name"], album, resident, skipped, warnings)
    if stored:
        services.settle_dates(album)  # as `upload` does
    else:
        direct_upload.delete(upload["key"])
    return JsonResponse({"uploaded": int(stored), "skipped": skipped, "warnings": warnings}, status=201)


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
    can_tag = access.can_tag(request)
    return JsonResponse(
        {
            "kind": "video" if media.content_type.startswith("video/") else "image",
            "title": media.title,
            "filename": media.display_name,
            "raw": media.is_raw,
            "full": media.high_definition.url if media.has_derivatives else "",
            "album": " / ".join(media.album.display_path()),
            "albumUrl": reverse("photo_album:detail", args=[media.album_id]),
            "downloadUrl": reverse("photo_album:download_original", args=[media.pk]),
            "deleteUrl": reverse("photo_album:delete", args=[media.pk]) if can_delete else "",
            "viewUrl": reverse("photo_album:record_view", args=[media.pk]),
            "mapUrl": _map_url(str(media.metadata.get("Sted", ""))),
            "uploadedBy": media.requested_by.full_name,
            "uploadedAt": _formatted(media.added_at),
            "capturedAt": _formatted(media.captured_at),
            **_approximate_date(media),
            "metadata": media.metadata,
            "tags": _tag_entries(media.tags.all(), media.album.lineage()),
            "tagUrl": reverse("photo_album:media_tags", args=[media.pk]) if can_tag else "",
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


# --- Tags ---------------------------------------------------------------------------------------


@login_required
@require_GET
def tag_search(request: HttpRequest) -> JsonResponse:
    """The tags in use matching `?q=`, for the dropdown under every tag field."""
    return JsonResponse({"tags": services.search_tags(request.GET.get("q", ""))})


def _change_tags(request: HttpRequest, target: Album | Media) -> str | None:
    """Add or remove the tag POSTed as `name`; an error message for the reader, or None."""
    if not access.can_tag(request):
        raise PermissionDenied
    name = request.POST.get("name", "")
    if request.POST.get("action") == "remove":
        services.remove_tag(target, name)
        return None
    try:
        services.add_tag(target, name)
    except ValidationError as exc:
        return " ".join(exc.messages)
    return None


@login_required
@require_POST
def album_tags(request: HttpRequest, pk: int) -> JsonResponse:
    album = get_object_or_404(Album, pk=pk)
    error = _change_tags(request, album)
    return JsonResponse(
        {"tags": _tag_entries(album.tags.all(), album.ancestors()), **({"error": error} if error else {})},
        status=400 if error else 200,
    )


@login_required
@require_POST
def media_tags(request: HttpRequest, pk: int) -> JsonResponse:
    media = _viewable_media(request, pk)
    error = _change_tags(request, media)
    return JsonResponse(
        {
            "tags": _tag_entries(media.tags.all(), media.album.lineage()),
            **({"error": error} if error else {}),
        },
        status=400 if error else 200,
    )


def _formatted(value: datetime | None) -> str:
    """The same wording the templates used to render, so the viewer reads identically."""
    return date_format(timezone.localtime(value), "j. F Y, H:i") if value else ""


_APPROXIMATION_BASIS = {
    DateSource.FILE: "ud fra filens dato",
    DateSource.NEIGHBOURS: "ud fra de andre billeder i albummet",
    DateSource.ALBUM_YEAR: "ud fra albummets årstal",
    DateSource.ADDED: "ud fra hvornår det blev uploadet",
}


def _approximate_date(media: Media) -> dict[str, str]:
    """`ca. september 2004` and why, for an item without a capture date; blank for one with.

    To the month only, or the year: an approximation is never as exact as a day would claim.
    """
    if not media.date_is_approximate or media.sort_at is None:
        return {"approximateDate": "", "approximateDateBasis": ""}
    precision = "Y" if media.date_source == DateSource.ALBUM_YEAR else "F Y"
    return {
        "approximateDate": f"ca. {date_format(timezone.localtime(media.sort_at), precision)}",
        "approximateDateBasis": _APPROXIMATION_BASIS[DateSource(media.date_source)],
    }


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

    cached = cache.cached_file(media, key)
    if cached is not None:
        try:
            local = cached.open("rb")
        except OSError:
            pass  # evicted between the two lines; the bucket still has it
        else:
            # Streamed rather than read into memory first: an animated GIF's HD copy can run to
            # many megabytes. Once it is open, an eviction no longer matters — the handle keeps it.
            copy = FileResponse(
                local, content_type=mimetypes.guess_type(key)[0] or "application/octet-stream"
            )
            copy.headers["Cache-Control"] = CACHED_FILE_CACHE_CONTROL
            copy.headers["Vary"] = "Cookie"
            return copy

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

    A video is up to PHOTO_ALBUM_VIDEO_MAX_MB (3 GB): reading that through the app server on its way from
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
