from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from residents.permissions import current_resident

from . import access, services
from .forms import AlbumForm, MediaUploadForm
from .models import Album, Media


@login_required
def index(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "photo_album/index.html",
        {"albums": Album.objects.all(), "can_create": access.can_create_album(request)},
    )


@login_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    album = get_object_or_404(Album, pk=pk)
    return render(
        request,
        "photo_album/detail.html",
        {
            "album": album,
            "media": access.visible_media(request, album),
            "can_manage": access.can_manage_media(request),
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
def upload(request: HttpRequest, pk: int) -> HttpResponse:
    album = get_object_or_404(Album, pk=pk)
    if not access.can_upload(request, album):
        raise PermissionDenied
    form = MediaUploadForm(request.POST, request.FILES)
    if form.is_valid():
        services.upload_media(
            album=album,
            uploaded_file=form.cleaned_data["file"],
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
    media = _managed_media(request, pk)
    if not access.can_delete(media, request):
        raise PermissionDenied
    album_id = media.album_id
    services.delete(media, current_resident(request))
    return redirect("photo_album:detail", album_id)


@login_required
@require_POST
def restore(request: HttpRequest, pk: int) -> HttpResponse:
    media = _managed_media(request, pk)
    media.deleted_at = None
    media.deleted_by = None
    media.save(update_fields=["deleted_at", "deleted_by"])
    return redirect("photo_album:detail", media.album_id)
