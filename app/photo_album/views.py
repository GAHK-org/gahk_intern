import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Case, Count, DateTimeField, IntegerField, Q, Value, When
from django.db.models.functions import Coalesce
from django.http import FileResponse, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from residents.permissions import current_resident

from . import access, services
from .forms import AlbumForm, MediaUploadForm
from .models import Album, Media


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
    for item in media_items:
        item.album = album  # the listing's items all share it; saves a refetch inside can_delete
        item.can_delete = access.can_delete(item, request, album_locked=album_locked)  # type: ignore[attr-defined]
        item.metadata_json = json.dumps(item.metadata)  # type: ignore[attr-defined]
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
        },
    )


@login_required
def create_album(request: HttpRequest) -> HttpResponse:
    if not access.can_create_album(request):
        raise PermissionDenied
    form = AlbumForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        album = form.save()
        return redirect("photo_album:detail", album.pk)
    return render(request, "photo_album/form.html", {"form": form, "title": "Opret album"})


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
        item.metadata_json = json.dumps(item.metadata)  # type: ignore[attr-defined]
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
    if not form.is_valid():
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
    return redirect("photo_album:detail", album.pk)


def _managed_media(request: HttpRequest, pk: int) -> Media:
    if not access.can_manage_media(request):
        raise PermissionDenied
    return get_object_or_404(Media, pk=pk)


@login_required
def download_original(request: HttpRequest, pk: int) -> FileResponse:
    media = get_object_or_404(Media.objects.select_related("album"), pk=pk)
    if (
        not access.can_manage_media(request)
        and not access.visible_media(request, media.album).filter(pk=pk).exists()
    ):
        raise PermissionDenied
    original_name = media.original.name or "original"
    return FileResponse(
        media.original.open("rb"),
        as_attachment=True,
        filename=original_name.rsplit("/", maxsplit=1)[-1],
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
    if media.status == "pending" and media.requested_by_id == requester.pk:
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
