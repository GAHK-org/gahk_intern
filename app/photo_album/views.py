import posixpath
from datetime import datetime
from secrets import compare_digest

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.db import transaction
from django.db.models import Case, Count, DateTimeField, IntegerField, Q, Value, When
from django.db.models.functions import Coalesce
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
)
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from arkiv.storage import content_disposition
from core.media import REDIRECT_CACHE_CONTROL
from residents.models import Resident
from residents.permissions import current_resident

from . import access, services
from .forms import AlbumForm, MediaUploadForm, ZipAlbumImportForm
from .models import Album, AlbumDownload, AlbumDownloadState, AlbumImport, AlbumImportState, Media
from .storage import PhotoAlbumS3Storage, get_photo_album_storage


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


@login_required
def index(request: HttpRequest) -> HttpResponse:
    folder = request.GET.get("folder", "")
    albums = Album.objects.annotate(
        media_count=Count("media", filter=Q(media__deleted_at__isnull=True, media__status="approved")),
        folder_position=Case(
            When(folder="Andet", then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        ),
    )
    if folder:
        albums = albums.filter(folder=folder)
    albums = albums.order_by("folder_position", "-folder", "name")
    return render(
        request,
        "photo_album/index.html",
        {"albums": albums, "can_create": access.can_create_album(request), "folder": folder},
    )


@login_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    album = get_object_or_404(Album, pk=pk)
    media_items = list(
        access.visible_media(request, album)
        .select_related("requested_by")
        .annotate(displayed_at=Coalesce("captured_at", "added_at", output_field=DateTimeField()))
        .order_by("-displayed_at", "-pk")
    )
    album_locked = album.is_locked()
    return render(
        request,
        "photo_album/detail.html",
        {
            "album": album,
            "album_locked": album_locked,
            "media": media_items,
            "can_manage": access.can_manage_media(request),
            "can_lock": access.can_lock_album(request, album),
            "can_unlock": access.can_unlock_album(request, album),
            "download_job": AlbumDownload.objects.filter(
                album=album, requested_by=current_resident(request)
            ).first(),
        },
    )


@login_required
@require_POST
def download_album(request: HttpRequest, pk: int) -> HttpResponseBase:
    """Queue a private ZIP build of the originals currently visible to this resident."""
    album = get_object_or_404(Album, pk=pk)
    resident = current_resident(request)
    media_ids = list(access.visible_media(request, album).values_list("pk", flat=True))
    if not media_ids:
        messages.error(request, "Albummet har ingen medier, du kan hente.")
        return redirect("photo_album:detail", pk=album.pk)
    # ONE BUILD PER RESIDENT PER ALBUM AT A TIME. The button stays on the page while a build runs,
    # and a whole-album ZIP is slow enough to invite a second click — which used to queue a second
    # complete archive of the same album and leave a second object in the bucket, while `detail`
    # only ever surfaces the newest row anyway. So an in-flight request is reported, not restarted.
    in_flight = AlbumDownload.objects.filter(
        album=album,
        requested_by=resident,
        state__in=(AlbumDownloadState.QUEUED, AlbumDownloadState.BUILDING),
    ).first()
    if in_flight is not None:
        messages.info(request, "Din download er allerede ved at blive klargjort.")
        return redirect("photo_album:detail", pk=album.pk)
    download = AlbumDownload.objects.create(album=album, requested_by=resident, media_ids=media_ids)
    from .tasks import build_album_download

    def enqueue() -> None:
        result = build_album_download.delay(download.pk)
        AlbumDownload.objects.filter(pk=download.pk).update(task_id=result.id)

    transaction.on_commit(enqueue)
    messages.success(request, "Download bliver klargjort.")
    return redirect("photo_album:detail", pk=album.pk)


@login_required
def download_album_archive(request: HttpRequest, token: str) -> HttpResponseBase:
    """Authorize the requesting resident and redirect their ready ZIP to object storage."""
    download = get_object_or_404(AlbumDownload, token=token, requested_by=current_resident(request))
    if download.state != AlbumDownloadState.READY or not download.archive_key:
        raise Http404("download is not ready")
    filename = f"{download.album.name}.zip"
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


@login_required
def create_album(request: HttpRequest) -> HttpResponse:
    if not access.can_create_album(request):
        raise PermissionDenied
    form = AlbumForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        album = form.save()
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


def import_zip(request: HttpRequest) -> HttpResponse:
    resident = current_resident(request)
    if resident is None:
        return redirect_to_login(request.get_full_path())
    if not access.can_create_album(request):
        raise PermissionDenied
    form = ZipAlbumImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
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
        result_url = f"{reverse('photo_album:import_zip')}?job={album_import.token}"
        if "application/json" in request.headers.get("Accept", ""):
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
    skipped: list[str] = []
    if token := request.GET.get("job"):
        job = get_object_or_404(AlbumImport, token=token, requested_by=current_resident(request))
        if job.state == AlbumImportState.READY:
            albums = list(Album.objects.filter(pk__in=job.album_ids).order_by("name"))
            skipped = job.skipped
    return render(
        request,
        "photo_album/import_zip.html",
        {
            "zip_form": form,
            "job": job,
            "albums": albums,
            "skipped": skipped,
            "folder_options": album_folder_options(),
        },
    )


@csrf_exempt
@require_POST
def system_import_zip(request: HttpRequest) -> JsonResponse:
    resident = _token_import_resident(request)
    if resident is None:
        raise PermissionDenied
    form = ZipAlbumImportForm(request.POST, request.FILES)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)

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
    return JsonResponse(
        {
            "statusUrl": reverse("photo_album:import_zip_status", args=[album_import.token]),
            "resultUrl": f"{reverse('photo_album:import_zip')}?job={album_import.token}",
        },
        status=202,
    )


def import_zip_status(request: HttpRequest, token: str) -> JsonResponse:
    resident = _token_import_resident(request) or current_resident(request)
    if resident is None:
        return JsonResponse({"detail": "Authentication required."}, status=401)
    album_import = get_object_or_404(AlbumImport, token=token, requested_by=resident)
    return JsonResponse({"state": album_import.state, "error": album_import.error})


@login_required
@require_POST
def delete_album(request: HttpRequest, pk: int) -> HttpResponse:
    if not access.can_create_album(request):
        raise PermissionDenied
    album = get_object_or_404(Album, pk=pk)
    try:
        album.delete()
    except ValidationError:
        # The button is drawn when no VISIBLE media remain, but Album.delete() refuses while any
        # row survives — binned items included. Say so instead of 500ing; the manager's way out is
        # to empty the bin (or wait for the 30-day sweep) and come back.
        messages.error(request, "Albummet kan ikke slettes, så længe det har medier i papirkurven.")
        return redirect("photo_album:detail", album.pk)
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
    if not access.can_unlock_album(request, album):
        messages.error(request, "Albummet har været låst i mere end 6 måneder og kan ikke låses op.")
        return redirect("photo_album:detail", album.pk)
    album.manually_locked_at = None
    album.unlocked_at = timezone.now()
    album.save(update_fields=["manually_locked_at", "unlocked_at"])
    return redirect("photo_album:detail", album.pk)


@login_required
def bin(request: HttpRequest) -> HttpResponse:
    if not access.can_manage_media(request):
        raise PermissionDenied
    media_items = list(
        Media.objects.filter(deleted_at__isnull=False)
        .select_related("album", "requested_by")
        .order_by("-deleted_at")
    )
    for item in media_items:
        item.can_permanently_delete = access.can_permanently_delete(item, request)  # type: ignore[attr-defined]
    return render(
        request,
        "photo_album/bin.html",
        {"media": media_items},
    )


@login_required
@require_POST
def upload(request: HttpRequest, pk: int) -> HttpResponse:
    album = get_object_or_404(Album, pk=pk)
    if not access.can_upload(request, album):
        raise PermissionDenied
    form = MediaUploadForm(request.POST, request.FILES)
    wants_json = "application/json" in request.headers.get("Accept", "")
    if not form.is_valid():
        if wants_json:
            return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
        # Without this the redirect below looked exactly like a successful upload and the photos
        # simply were not there.
        for error in form.errors.get("uploads", ["Filerne kunne ikke uploades."]):
            messages.error(request, str(error))
        return redirect("photo_album:detail", album.pk)
    for uploaded_file in form.cleaned_data["uploads"]:
        services.upload_media(
            album=album,
            uploaded_file=uploaded_file,
            resident=current_resident(request),
            title=form.cleaned_data["title"],
            approved=access.can_manage_media(request),
        )
    if wants_json:
        return JsonResponse({"uploaded": len(form.cleaned_data["uploads"])}, status=201)
    return redirect("photo_album:detail", album.pk)


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
    return JsonResponse(
        {
            "kind": "video" if media.content_type.startswith("video/") else "image",
            "title": media.title,
            "full": media.high_definition.url if media.has_derivatives else "",
            "album": media.album.name,
            "downloadUrl": reverse("photo_album:download_original", args=[media.pk]),
            "deleteUrl": reverse("photo_album:delete", args=[media.pk]) if can_delete else "",
            "uploadedBy": media.requested_by.full_name,
            "uploadedAt": _formatted(media.added_at),
            "capturedAt": _formatted(media.captured_at),
            "metadata": media.metadata,
        }
    )


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
    """The row a storage key belongs to, checking all three variants — the key alone can't say which."""
    return (
        Media.objects.select_related("album", "requested_by")
        .filter(Q(original=key) | Q(high_definition=key) | Q(thumbnail=key))
        .first()
    )


def serve_media(request: HttpRequest, path: str) -> HttpResponseBase:
    """/fotoalbum-media/<path> — the only way a browser ever reaches a photo-album object.

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
    """
    media = _viewable_media(request, pk)
    original_name = media.original.name or "original"
    filename = original_name.rsplit("/", maxsplit=1)[-1]
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


@login_required
@require_POST
def approve(request: HttpRequest, pk: int) -> HttpResponse:
    media = _managed_media(request, pk)
    services.approve(media, current_resident(request))
    return redirect("photo_album:detail", media.album_id)


@login_required
@require_POST
def reject(request: HttpRequest, pk: int) -> HttpResponse:
    media = _managed_media(request, pk)
    services.reject(media, current_resident(request))
    return redirect("photo_album:detail", media.album_id)


@login_required
@require_POST
def delete(request: HttpRequest, pk: int) -> HttpResponse:
    media = get_object_or_404(Media, pk=pk)
    if not access.can_delete(media, request):
        raise PermissionDenied
    album_id = media.album_id
    requester = current_resident(request)
    # Asked of access rather than re-derived here. This branch used to restate can_delete's own
    # "your own pending upload" rule, in a second copy that spelled the status as a bare string —
    # so the two could disagree about what a withdrawal is, and only one of them gated the action.
    if access.can_withdraw(media, request):
        services.permanently_delete(media)
    else:
        services.delete(media, requester)
    return redirect("photo_album:detail", album_id)


@login_required
@require_POST
def restore(request: HttpRequest, pk: int) -> HttpResponse:
    media = _managed_media(request, pk)
    services.restore(media)
    return redirect("photo_album:detail", media.album_id)


@login_required
@require_POST
def permanently_delete(request: HttpRequest, pk: int) -> HttpResponse:
    media = _managed_media(request, pk)
    if not access.can_permanently_delete(media, request):
        raise PermissionDenied
    services.permanently_delete(media)
    return redirect("photo_album:bin")
