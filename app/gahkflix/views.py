"""GAHKFlix's pages, its file routes and the JSON the player and the editors talk to.

EVERY FILE GOES THROUGH A VIEW HERE. Pictures, streams and originals are all looked up by the video
they belong to, checked against access.can_see, and only then served from the local cache or
redirected to a short-lived signed URL — never a bucket URL rendered into a page (storage.py).
"""

import json
import logging
import math
import mimetypes
from datetime import timedelta
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Prefetch, Q, QuerySet
from django.db.models.functions import Lower
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
from django.utils.http import urlencode
from django.views.decorators.http import require_GET, require_POST

from arkiv.storage import content_disposition
from core.media import REDIRECT_CACHE_CONTROL
from residents.models import Resident
from residents.permissions import current_resident

from . import access, cache, dating, services
from .forms import DeletionRequestForm, SeriesForm, VideoDetailsForm, VoteForm
from .models import (
    CastMember,
    ChangeRequest,
    FileState,
    Series,
    Tag,
    Video,
    VideoEvent,
    VideoFrame,
    ViewEvent,
    key_of,
)
from .storage import STREAM_URL_TTL, get_video_storage, presigned_post, signed_url, stored_size
from .templatetags.gahkflix import picture_url
from .uploads import UploadTicket, max_upload_bytes

logger = logging.getLogger(__name__)

# A picture's URL carries its key (?v=…), and a key never changes meaning, so the browser may keep it.
PICTURE_CACHE_CONTROL = "private, max-age=604800"
ROW_LENGTH = 20
# How far back the upload page lists the uploader's published videos.
RECENT_UPLOADS = timedelta(days=7)
# Each picture kind: (what it is called in a label, "the new …" proposed, "the …" changed).
PICTURES = {
    "thumbnail": ("Miniature", "Den nye miniature", "Miniaturen"),
    "banner": ("Banner", "Det nye banner", "Banneret"),
}
# How many of the thumbnails and the banners a video had before the edit page offers to bring back;
# the history has every one.
PAST_PICTURES_SHOWN = 6


def _json_body(request: HttpRequest) -> dict[str, Any]:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _visible_video(request: HttpRequest, pk: int) -> Video:
    """The video, or a 404 — also for one the reader may not see, so its existence stays private."""
    video = get_object_or_404(Video.objects.select_related("file", "series", "uploaded_by"), pk=pk)
    if not access.can_see(request, video):
        raise Http404("no such video")
    return video


def _with_progress(request: HttpRequest, videos: list[Video]) -> list[Video]:
    progress = services.progress_by_video(current_resident(request), videos)
    for video in videos:
        video.progress = progress.get(video.pk)  # type: ignore[attr-defined]
    return videos


def _base_context(request: HttpRequest) -> dict[str, Any]:
    resident = current_resident(request)
    return {
        "is_administrator": access.is_administrator(request),
        "limited_rollout": access.gate.is_limited(),
        "pending_upload": services.pending_upload(resident),
        "open_requests": (
            ChangeRequest.objects.filter(state=ChangeRequest.State.OPEN)
            .exclude(requested_by=resident)
            .exclude(votes__voter=resident)
            .count()
            if access.is_administrator(request)
            else 0
        ),
    }


def _render(request: HttpRequest, template: str, context: dict[str, Any] | None = None) -> HttpResponse:
    return render(request, template, {**_base_context(request), **(context or {})})


# --- Browsing -----------------------------------------------------------------------------------


@access.access_required
def home(request: HttpRequest) -> HttpResponse:
    resident = current_resident(request)
    newest = list(services.published().order_by("-published_at", "-pk")[:ROW_LENGTH])
    trending = services.trending()
    series = list(services.series_with_episodes().order_by(Lower("title"))[:ROW_LENGTH])
    rows: list[dict[str, Any]] = []
    if resume := services.continue_watching(resident):
        rows.append({"title": "Fortsæt med at se", "videos": resume, "resume": True})
    if trending:
        rows.append({"title": "Top 10 på GAHKFlix lige nu", "videos": trending, "ranked": True})
    if newest:
        rows.append({"title": "Nyeste", "videos": newest})
    for tag, videos in services.tag_rows():
        rows.append(
            {"title": f"#{tag.name}", "videos": videos, "url": reverse("gahkflix:tag", args=[tag.name])}
        )
    for row in rows:
        _with_progress(request, row["videos"])
    return _render(
        request,
        "gahkflix/home.html",
        {
            "hero": services.hero(trending or newest),
            "rows": rows,
            "series": series,
            "covers": services.covers(series),
        },
    )


@access.access_required
def series_list(request: HttpRequest) -> HttpResponse:
    series = list(services.series_with_episodes().annotate(n=Count("videos", distinct=True)))
    own_empty = Series.objects.filter(created_by=current_resident(request)).exclude(
        pk__in=[s.pk for s in series]
    )
    return _render(
        request,
        "gahkflix/series_list.html",
        {"series": series, "covers": services.covers(series), "own_empty": own_empty},
    )


@access.access_required
def series_detail(request: HttpRequest, pk: int) -> HttpResponse:
    series = get_object_or_404(Series, pk=pk)
    seasons = services.episodes(series)
    for _season, videos in seasons:
        _with_progress(request, videos)
    return _render(
        request,
        "gahkflix/series_detail.html",
        {
            "series": series,
            "seasons": seasons,
            "cover": services.series_cover(series),
            "can_edit": access.can_edit_series(request, series),
            "can_delete": access.can_delete_series(request, series),
            "open_edit": services.open_request(series, ChangeRequest.Kind.EDIT),
        },
    )


@access.access_required
def series_create(request: HttpRequest) -> HttpResponse:
    """A new series. The edit page's dialog asks for JSON (`?format=json`) and picks the series
    itself; coming from a page of its own, the edit page is sent back with it picked (`?serie=`)."""
    form = SeriesForm(request.POST or None)
    wants_json = request.GET.get("format") == "json"
    if request.method == "POST" and form.is_valid():
        series = form.save(commit=False)
        series.created_by = current_resident(request)
        series.save()
        if wants_json:
            return JsonResponse({"id": series.pk, "title": series.title}, status=201)
        back = request.POST.get("next", "")
        if back.startswith("/intern/gahkflix/"):
            messages.success(request, f"Serien {series.title} er oprettet og valgt herunder. Husk at gemme.")
            return redirect(f"{back}?{urlencode({'serie': series.pk})}")
        messages.success(
            request, f"Serien {series.title} er oprettet. Læg dine videoer i den fra deres redigeringsside."
        )
        return redirect("gahkflix:series", series.pk)
    if wants_json:
        errors = " ".join(str(message) for field in form.errors.values() for message in field)
        return JsonResponse({"error": errors or "Serien kunne ikke oprettes."}, status=400)
    return _render(request, "gahkflix/series_form.html", {"form": form, "next": request.GET.get("next", "")})


@access.access_required
def series_edit(request: HttpRequest, pk: int) -> HttpResponse:
    series = get_object_or_404(Series, pk=pk)
    if not access.can_edit_series(request, series):
        raise PermissionDenied
    needs_approval = access.series_edit_needs_approval(series)
    form = SeriesForm(request.POST or None, instance=Series.objects.get(pk=pk))
    if request.method == "POST" and form.is_valid():
        try:
            outcome = services.edit_series(
                series,
                {
                    "title": form.cleaned_data["title"],
                    "description": form.cleaned_data["description"].strip(),
                },
                current_resident(request),
                needs_approval=needs_approval,
            )
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            _report(request, outcome)
            return redirect("gahkflix:series", series.pk)
    return _render(
        request,
        "gahkflix/series_form.html",
        {"form": form, "series": series, "needs_approval": needs_approval},
    )


@access.access_required
@require_POST
def series_delete(request: HttpRequest, pk: int) -> HttpResponse:
    series = get_object_or_404(Series, pk=pk)
    if not access.can_delete_series(request, series):
        messages.error(request, "En serie kan kun slettes, når der ingen videoer er i den.")
        return redirect("gahkflix:series", series.pk)
    series.delete()
    messages.success(request, "Serien er slettet.")
    return redirect("gahkflix:series_list")


@access.access_required
def timeline(request: HttpRequest) -> HttpResponse:
    years = services.timeline()
    for _year, months in years:
        for _month, videos in months:
            _with_progress(request, videos)
    return _render(request, "gahkflix/timeline.html", {"years": years})


@access.access_required
def search(request: HttpRequest) -> HttpResponse:
    query = request.GET.get("q", "").strip()[:100]
    context: dict[str, Any] = {"query": query}
    if query:
        context["videos"] = _with_progress(request, list(services.search(query)[:60]))
        context["series"] = list(
            services.series_with_episodes().filter(
                Q(title__icontains=query) | Q(description__icontains=query)
            )[:12]
        )
        context["covers"] = services.covers(context["series"])
        context["people"] = _people_matching(query)
    template = "gahkflix/_search_results.html" if request.GET.get("partial") else "gahkflix/search.html"
    return _render(request, template, context)


def _people_matching(query: str) -> list[dict[str, Any]]:
    """Credited people whose name matches: residents first, then external names."""
    credits = CastMember.objects.filter(video__in=Video.objects.published())
    people: list[dict[str, Any]] = []
    residents = Resident.objects.filter(pk__in=credits.exclude(resident=None).values("resident_id"))
    for word in query.split()[:4]:
        residents = residents.filter(Q(first_name__icontains=word) | Q(last_name__icontains=word))
    for resident in residents.order_by("first_name", "last_name")[:12]:
        people.append(
            {
                "name": resident.full_name,
                "url": reverse("gahkflix:cast", args=[resident.pk]),
                "resident": resident,
            }
        )
    names = credits.filter(resident=None, name__icontains=query).values_list("name", flat=True).distinct()
    for name in sorted(set(names), key=str.lower)[:12]:
        people.append({"name": name, "url": f"{reverse('gahkflix:cast_name')}?{urlencode({'navn': name})}"})
    return people


@access.access_required
def tag(request: HttpRequest, name: str) -> HttpResponse:
    tag = get_object_or_404(Tag, name__iexact=name)
    videos = _with_progress(
        request, list(services.published().filter(tags=tag).order_by(*services.sort_date_ordering()))
    )
    return _render(request, "gahkflix/grid.html", {"title": f"#{tag.name}", "videos": videos})


@access.access_required
def cast(request: HttpRequest, pk: int) -> HttpResponse:
    person = get_object_or_404(Resident, pk=pk)
    credits = services.credits_of(person).order_by(*services.sort_date_ordering())
    return _render(
        request,
        "gahkflix/grid.html",
        {
            "title": person.full_name,
            "person": person,
            "videos": _with_progress(request, list(credits)),
            "roles": _roles_by_video(CastMember.objects.filter(resident=person)),
        },
    )


@access.access_required
def cast_name(request: HttpRequest) -> HttpResponse:
    name = request.GET.get("navn", "").strip()
    if not name:
        raise Http404("no name")
    credits = list(services.credits_named(name).order_by(*services.sort_date_ordering()))
    if not credits:
        raise Http404("nobody by that name")
    return _render(
        request,
        "gahkflix/grid.html",
        {
            "title": name,
            "external": True,
            "videos": _with_progress(request, credits),
            "roles": _roles_by_video(CastMember.objects.filter(resident=None, name__iexact=name)),
        },
    )


def _roles_by_video(credits: QuerySet[CastMember]) -> dict[int, str]:
    return {credit.video_id: credit.role for credit in credits if credit.role}


def _with_latest_file(videos: QuerySet[Video]) -> list[Video]:
    """The videos, each with `latest_file`: its newest upload, which says whether it is processing."""
    rows: list[Video] = list(
        videos.select_related("file", "series").prefetch_related(Prefetch("files", to_attr="all_files"))
    )
    for video in rows:
        files = getattr(video, "all_files", [])
        video.latest_file = files[0] if files else None  # type: ignore[attr-defined]
    return rows


@access.access_required
def mine(request: HttpRequest) -> HttpResponse:
    resident = current_resident(request)
    videos = _with_latest_file(Video.objects.filter(uploaded_by=resident).order_by("-created_at"))
    drafts: list[Video] = []
    live: list[Video] = []
    binned: list[Video] = []
    for video in videos:
        (binned if video.is_binned else drafts if video.is_draft else live).append(video)
    requests = ChangeRequest.objects.filter(requested_by=resident).select_related("video", "series")[:20]
    return _render(
        request,
        "gahkflix/mine.html",
        {"drafts": drafts, "live": _with_progress(request, live), "binned": binned, "requests": requests},
    )


# --- One video ----------------------------------------------------------------------------------


@access.access_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    video = _visible_video(request, pk)
    resident = current_resident(request)
    if video.file is not None:
        services.fill_rendition_sizes(video.file)
    counts = services.view_counts([video]).get(video.pk, (0, 0))
    seasons = services.episodes(video.series) if video.series_id and video.series else []
    for _season, episodes in seasons:
        _with_progress(request, episodes)
    return _render(
        request,
        "gahkflix/detail.html",
        {
            "video": video,
            "cast": video.cast.select_related("resident"),
            "tags": video.tags.all(),
            "views": counts[0],
            "viewers": counts[1],
            "resume_at": services.resume_position(video, resident),
            "seasons": seasons,
            "similar": _with_progress(request, services.similar(video)),
            "stats": services.view_stats(video) if access.can_view_stats(request, video) else None,
            "can_edit": access.can_edit(request, video),
            "can_restore": access.can_restore(request, video),
            "can_view_history": access.can_view_history(request, video),
        },
    )


QUALITY_LABELS = {"fullhd": "Full HD", "hd": "HD"}
UP_NEXT_DESCRIPTION = 200


def _up_next(following: Video | None) -> dict[str, Any] | None:
    """The end card's video: what it is, and a picture of it."""
    if following is None:
        return None
    description = following.description
    if len(description) > UP_NEXT_DESCRIPTION:
        description = description[:UP_NEXT_DESCRIPTION].rsplit(" ", 1)[0] + " …"
    label = ""
    if following.series:
        label = " · ".join(filter(None, [following.series.title, following.episode_label]))
    return {
        "url": reverse("gahkflix:play", args=[following.pk]),
        "title": following.title,
        "kind": "episode" if following.series else "film",
        "label": label,
        "description": description,
        "image": picture_url(following, "banner") or picture_url(following, "thumbnail"),
    }


def _player_config(video: Video, start_at: float, resident: Resident) -> dict[str, Any]:
    """Everything the player needs for one film: the play page renders it, the overlay fetches it."""
    assert video.file is not None  # noqa: S101  # nosec B101 — play() checks
    play_url = reverse("gahkflix:play", args=[video.pk])
    if video.series:
        title = f"{video.series.title} · {video.episode_label}" if video.episode_label else video.series.title
        subtitle = video.title
    else:
        title, subtitle = video.title, ""
    return {
        "title": title,
        "subtitle": subtitle,
        "streams": {q: reverse("gahkflix:stream", args=[video.pk, q]) for q in video.file.qualities},
        "qualityLabels": QUALITY_LABELS,
        "startAt": start_at,
        "viewUrl": reverse("gahkflix:record_view", args=[video.pk]),
        "positionUrl": reverse("gahkflix:position", args=[video.pk]),
        "playUrl": play_url,
        "backUrl": reverse("gahkflix:detail", args=[video.pk]),
        "next": _up_next(services.up_next(video, resident)),
    }


@access.access_required
def play(request: HttpRequest, pk: int) -> HttpResponse:
    """The player. As a page for a link opened directly; as JSON (`?format=json`) for the player that
    opens over the page the reader clicked "Afspil" on — which is what lets the intro start with
    sound, because a browser only allows that inside the click that asked for it."""
    video = _visible_video(request, pk)
    wants_json = request.GET.get("format") == "json"
    if video.file is None:
        if wants_json:
            return _error("Videoen er ikke færdigbehandlet endnu.", 409)
        messages.error(request, "Videoen er ikke færdigbehandlet endnu.")
        return redirect("gahkflix:detail", video.pk)
    start = request.GET.get("t", "")
    resident = current_resident(request)
    resume = services.resume_position(video, resident)
    config = _player_config(
        video, float(start) if start.replace(".", "", 1).isdecimal() else resume, resident
    )
    # Opened in the player: a viewing for the statistics, which the player reports on. A draft's
    # uploader trying it out is not one.
    config["viewing"] = services.open_viewing(video, resident).pk if video.is_published else None
    if wants_json:
        return JsonResponse(config)
    return _render(request, "gahkflix/play.html", {"video": video, "player": config})


def _serve(
    key: str, *, content_type: str, attachment: str = "", expire: int | None = None
) -> HttpResponseBase:
    parameters = {"ResponseContentType": content_type}
    if attachment:
        parameters["ResponseContentDisposition"] = content_disposition(attachment)
    target = signed_url(key, expire=expire, parameters=parameters)
    if target is not None:
        response = HttpResponseRedirect(target)
        response.headers["Cache-Control"] = REDIRECT_CACHE_CONTROL
        response.headers["Vary"] = "Cookie"
        return response
    storage = get_video_storage()
    try:
        handle = storage.open(key, "rb")
    except (FileNotFoundError, OSError) as exc:
        raise Http404("file not found") from exc
    if attachment:
        return FileResponse(handle, as_attachment=True, filename=attachment, content_type=content_type)
    return FileResponse(handle, content_type=content_type)


@access.access_required
def stream(request: HttpRequest, pk: int, quality: str) -> HttpResponseBase:
    video = _visible_video(request, pk)
    field = {"fullhd": "full_hd", "hd": "hd"}.get(quality)
    if video.file is None or field is None or not getattr(video.file, field):
        raise Http404("no such rendition")
    return _serve(key_of(getattr(video.file, field)), content_type="video/mp4", expire=STREAM_URL_TTL)


# What "Hent" offers: variant -> (field on VideoFile, label).
DOWNLOADS = {"hd": ("hd", "HD"), "fullhd": ("full_hd", "Full HD"), "original": ("original", "Original")}


def _download_name(title: str, label: str) -> str:
    """A title as a filename on any system: no separators or other characters Windows refuses."""
    safe = "".join(c for c in title if c.isprintable() and c not in '<>:"/\\|?*').strip(" .")
    return f"{safe or 'video'} ({label}).mp4"


@access.access_required
def download(request: HttpRequest, pk: int, variant: str) -> HttpResponseBase:
    video = _visible_video(request, pk)
    if video.file is None or variant not in DOWNLOADS:
        raise Http404("no such download")
    field_name, label = DOWNLOADS[variant]
    field = getattr(video.file, field_name)
    if not field:
        raise Http404("no such rendition")
    if variant == "original":
        filename = video.file.original_name
        content_type = video.file.content_type or "application/octet-stream"
    else:
        filename, content_type = _download_name(video.title, label), "video/mp4"
    return _serve(key_of(field), content_type=content_type, attachment=filename)


def _picture_response(key: str) -> HttpResponseBase:
    if not key:
        raise Http404("no picture")
    cached = cache.cached_picture(key)
    if cached is not None:
        try:
            body = cached.read_bytes()
        except OSError:
            pass
        else:
            response = HttpResponse(body, content_type=mimetypes.guess_type(key)[0] or "image/jpeg")
            response.headers["Cache-Control"] = PICTURE_CACHE_CONTROL
            response.headers["Vary"] = "Cookie"
            return response
    return _serve(key, content_type="image/jpeg")


@access.access_required
def picture(request: HttpRequest, pk: int, kind: str) -> HttpResponseBase:
    video = _visible_video(request, pk)
    if kind not in PICTURES:
        raise Http404("no such picture")
    return _picture_response(key_of(getattr(video, kind)))


@access.access_required
def frame_image(request: HttpRequest, pk: int, frame: int) -> HttpResponseBase:
    video = _visible_video(request, pk)
    if not access.can_edit(request, video):
        raise Http404("no such frame")
    return _picture_response(key_of(get_object_or_404(VideoFrame, pk=frame, video=video).image))


def _viewing(request: HttpRequest, video: Video, data: dict[str, Any]) -> ViewEvent | None:
    """The viewing the player reports on (`viewing` in its config), if it is the reader's own."""
    pk = data.get("viewing")
    if not isinstance(pk, int) or isinstance(pk, bool):
        return None
    return ViewEvent.objects.filter(pk=pk, video=video, resident=current_resident(request)).first()


@access.access_required
@require_POST
def record_view(request: HttpRequest, pk: int) -> JsonResponse:
    video = _visible_video(request, pk)
    if not video.is_published:
        return JsonResponse({"counted": False})
    viewing = _viewing(request, video, _json_body(request))
    return JsonResponse({"counted": services.record_view(video, current_resident(request), viewing=viewing)})


@access.access_required
@require_POST
def save_position(request: HttpRequest, pk: int) -> JsonResponse:
    video = _visible_video(request, pk)
    data = _json_body(request)
    try:
        position = float(data.get("position", request.POST.get("position", 0)))
        watched = float(data.get("watched", 0) or 0)
    except (TypeError, ValueError):
        return _error("Ugyldig position.")
    if not all(math.isfinite(value) and value >= 0 for value in (position, watched)):
        return _error("Ugyldig position.")
    watch = services.save_position(
        video, current_resident(request), position, viewing=_viewing(request, video, data), watched=watched
    )
    return JsonResponse({"position": watch.position, "finished": watch.finished})


# --- Uploading ----------------------------------------------------------------------------------


def _upload_config(video: Video | None = None) -> dict[str, Any]:
    return {
        "beginUrl": reverse("gahkflix:upload_begin"),
        "commitUrl": reverse("gahkflix:upload_commit"),
        "video": video.pk if video else None,
        "maxBytes": max_upload_bytes(),
    }


@access.access_required
def upload(request: HttpRequest) -> HttpResponse:
    resident = current_resident(request)
    # Shown first, so a video already uploaded is not uploaded again: every unpublished one (still
    # processing, or waiting to be finished), and those uploaded in the last week.
    recent = _with_latest_file(
        Video.objects.filter(uploaded_by=resident, deleted_at__isnull=True)
        .filter(Q(published_at__isnull=True) | Q(created_at__gt=timezone.now() - RECENT_UPLOADS))
        .order_by("-created_at")
    )
    return _render(
        request,
        "gahkflix/upload.html",
        {
            "max_gb": max_upload_bytes() / 1024**3,
            "pending": services.pending_upload(resident),
            "upload_config": _upload_config(),
            "recent": recent,
        },
    )


@access.access_required
@require_POST
def upload_begin(request: HttpRequest) -> JsonResponse:
    data = _json_body(request)
    resident = current_resident(request)
    video = None
    if data.get("video"):
        video = Video.objects.filter(pk=data["video"]).first()
        if video is None or not access.can_upload_revision(request, video):
            return _error("Du kan ikke uploade en ny version af den video.", 403)
    try:
        size = int(data.get("size", 0) or 0)
    except (TypeError, ValueError):
        return _error("Ugyldig filstørrelse.")
    try:
        ticket = services.begin_upload(
            resident,
            name=str(data.get("name", "")),
            size=size,
            content_type=str(data.get("type", "") or ""),
            fingerprint=str(data.get("fingerprint", "")),
            last_modified=str(data.get("lastModified", "") or ""),
            video=video,
        )
    except ValidationError as exc:
        return _error(" ".join(exc.messages), 409 if "allerede" in " ".join(exc.messages) else 400)
    policy = presigned_post(ticket.key, max_upload_bytes(), ticket.content_type)
    plan = (
        {"mode": "s3", "url": policy["url"], "fields": policy["fields"]}
        if policy is not None
        else {"mode": "direct", "url": reverse("gahkflix:upload_direct")}
    )
    return JsonResponse({"token": ticket.sign(), "upload": plan})


@access.access_required
@require_POST
def upload_direct(request: HttpRequest) -> JsonResponse:
    """The no-bucket path (the test suite): the file comes through Django. Refused when there is a
    bucket, or this would become the quiet way a 5 GB film goes through the app server after all."""
    if presigned_post("probe", 1, "") is not None:
        raise PermissionDenied("direct upload is only for local storage")
    ticket = UploadTicket.load(request.POST.get("token", ""))
    upload_file = request.FILES.get("file")
    if ticket is None or ticket.resident != current_resident(request).pk or upload_file is None:
        return _error("Ugyldig upload.")
    if (upload_file.size or 0) > max_upload_bytes():
        return _error("Videoen er for stor.")
    get_video_storage().save(ticket.key, upload_file)
    return JsonResponse({"ok": True})


@access.access_required
@require_POST
def upload_commit(request: HttpRequest) -> JsonResponse:
    ticket = UploadTicket.load(str(_json_body(request).get("token", "")))
    if ticket is None:
        return _error("Uploaden er udløbet eller ugyldig. Start forfra.")
    try:
        video = services.commit_upload(ticket, current_resident(request), stored_size(ticket.key))
    except ValidationError as exc:
        return _error(" ".join(exc.messages), 409)
    return JsonResponse({"ok": True, "editUrl": reverse("gahkflix:edit", args=[video.pk])}, status=201)


@access.access_required
def status(request: HttpRequest, pk: int) -> JsonResponse:
    """What the edit page polls while the file is processed, or new frames are drawn."""
    video = _visible_video(request, pk)
    if not access.can_edit(request, video):
        raise Http404("no such video")
    latest = video.files.order_by("-created_at", "-pk").first()
    return JsonResponse(
        {
            "state": latest.state if latest else "",
            "progress": latest.progress if latest else 0,
            "error": latest.error if latest else "",
            # While it waits its turn: how many go before it.
            "ahead": services.queue_ahead(latest) if latest and latest.state == FileState.QUEUED else 0,
            "live": video.file_id is not None,
            "frames": len(services.current_frames(video)),
            "batch": services.frame_batch(video),
        }
    )


# --- Editing ------------------------------------------------------------------------------------


def _episode_numbers(request: HttpRequest, video: Video) -> dict[str, list[dict[str, Any]]]:
    """Every series' seasons and episodes as numbers, for the pickers on the edit page — so the next
    free number is plain to see. A draft the reader may not see is listed as taken, untitled."""
    resident = current_resident(request)
    administrator = access.is_administrator(request)
    rows = (
        Video.objects.filter(series__isnull=False, deleted_at__isnull=True)
        .exclude(pk=video.pk)
        .values("series_id", "season", "episode", "title", "published_at", "uploaded_by_id")
    )
    numbers: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        visible = row["published_at"] is not None or administrator or row["uploaded_by_id"] == resident.pk
        numbers.setdefault(str(row["series_id"]), []).append(
            {
                "season": row["season"],
                "episode": row["episode"],
                "title": row["title"] if visible else "Kladde",
            }
        )
    return numbers


def _editable(request: HttpRequest, pk: int) -> Video:
    video = _visible_video(request, pk)
    if not access.can_edit(request, video):
        raise PermissionDenied
    return video


def _report(request: HttpRequest, outcome: services.Outcome) -> None:
    if outcome.proposal is not None:
        messages.info(request, "Ændringen er sendt til godkendelse hos en anden administrator.")
    elif outcome.applied:
        messages.success(request, "Ændringerne er gemt.")
    else:
        messages.info(request, "Der var ingen ændringer at gemme.")
    for note in outcome.notes:
        messages.warning(request, note)


@access.access_required
def edit(request: HttpRequest, pk: int) -> HttpResponse:
    video = _editable(request, pk)
    if request.method == "POST":
        form = VideoDetailsForm(request.POST, video=video)
        if form.is_valid():
            try:
                outcome = services.edit_video(
                    video,
                    form.details(),
                    current_resident(request),
                    needs_approval=access.edit_needs_approval(video),
                )
            except ValidationError as exc:
                for message in exc.messages:
                    messages.error(request, message)
            else:
                _report(request, outcome)
                return redirect("gahkflix:edit", video.pk)
        return _edit_page(request, video, form)
    initial = services.snapshot(video)
    initial.update(VideoDetailsForm.release_initial(initial.pop("released_on")))
    # The editors (gahkflix.ts) read and write these two as JSON in their hidden inputs.
    initial["cast"] = json.dumps(initial["cast"], ensure_ascii=False)
    initial["tags"] = json.dumps(initial["tags"], ensure_ascii=False)
    # A series just created on a page of its own (series_create), picked, to be saved with the rest.
    if (created := request.GET.get("serie", "")).isdecimal() and Series.objects.filter(pk=created).exists():
        initial["series"] = int(created)
    return _edit_page(request, video, VideoDetailsForm(initial=initial, video=video))


def _edit_page(request: HttpRequest, video: Video, form: VideoDetailsForm) -> HttpResponse:
    latest = video.files.order_by("-created_at", "-pk").first()
    now = timezone.now()
    return _render(
        request,
        "gahkflix/edit.html",
        {
            "video": video,
            "form": form,
            "latest_file": latest,
            "frames": services.current_frames(video),
            "needs_approval": access.edit_needs_approval(video),
            "open_edit": services.open_request(video, ChangeRequest.Kind.EDIT),
            "open_delete": services.open_request(video, ChangeRequest.Kind.DELETE),
            "missing": services.missing_for_publishing(video) if video.is_draft else [],
            "past_pictures": [
                picture
                for kind in PICTURES
                for picture in services.past_pictures(video, kind)[:PAST_PICTURES_SHOWN]
            ],
            "can_upload_revision": access.can_upload_revision(request, video),
            "can_delete": access.can_delete_directly(request, video),
            "can_request_deletion": access.can_request_deletion(request, video),
            "days_left": {"edit": max(0, (video.edit_window_ends - now).days)},
            "processing": latest is not None and latest.is_pending,
            "episode_numbers": _episode_numbers(request, video),
            "upload_config": _upload_config(video),
            "max_gb": max_upload_bytes() / 1024**3,
        },
    )


@access.access_required
@require_POST
def choose_picture(request: HttpRequest, pk: int) -> HttpResponse:
    """Use a frame, a moment picked in the player, an uploaded picture or one the video had before
    as the thumbnail or the banner."""
    video = _editable(request, pk)
    kind = request.POST.get("kind", "")
    if kind not in PICTURES:
        return HttpResponseBadRequest("unknown picture")
    frame = None
    origin = ""
    try:
        if frame_id := request.POST.get("frame", ""):
            frame = get_object_or_404(
                VideoFrame, pk=int(frame_id) if frame_id.isdecimal() else 0, video=video
            )
            data = services.picture_from_frame(frame, kind)
        elif (at := request.POST.get("at", "")) and at.replace(".", "", 1).isdecimal():
            # A moment the uploader picked in the player themselves.
            data = services.picture_at(video, float(at), kind)
        elif (upload_file := request.FILES.get("picture")) is not None:
            data = services.picture_from_upload(upload_file, kind)
        elif ref := request.POST.get("previous", ""):
            # One the video had before, as its history names it.
            earlier = services.past_picture(video, kind, ref)
            data, origin = services.copy_of(earlier), earlier.origin
            frame = (
                VideoFrame.objects.filter(pk=earlier.frame, video=video).first() if earlier.frame else None
            )
        else:
            messages.error(request, "Vælg et billede.")
            return redirect("gahkflix:edit", video.pk)
        key = services.store_picture(video, kind, data)
        outcome = services.set_picture(
            video,
            kind,
            key,
            frame,
            current_resident(request),
            needs_approval=access.edit_needs_approval(video),
            origin=origin,
        )
    except ValidationError as exc:
        for message in exc.messages:
            messages.error(request, message)
        return redirect("gahkflix:edit", video.pk)
    if outcome.proposal is not None:
        messages.info(request, f"{PICTURES[kind][1]} er sendt til godkendelse.")
    elif origin:
        messages.success(request, f"{PICTURES[kind][2]} er sat tilbage til et tidligere billede.")
    else:
        messages.success(request, f"{PICTURES[kind][2]} er skiftet.")
    return redirect(f"{reverse('gahkflix:edit', args=[video.pk])}#billeder")


@access.access_required
@require_POST
def new_frames(request: HttpRequest, pk: int) -> HttpResponse:
    """Ten new frames to pick from, drawn by the worker. The edit page asks for JSON (`?format=json`):
    the batch there is now, to wait for the next one and swap it in without reloading."""
    video = _editable(request, pk)
    wants_json = request.GET.get("format") == "json"
    if video.file is None:
        if wants_json:
            return _error("Videoen er ikke færdigbehandlet endnu.", 409)
        messages.error(request, "Videoen er ikke færdigbehandlet endnu.")
        return redirect(f"{reverse('gahkflix:edit', args=[video.pk])}#billeder")
    from .tasks import redraw_frames

    batch = services.frame_batch(video)
    redraw_frames.delay(video.pk)
    if wants_json:
        return JsonResponse({"batch": batch})
    messages.info(
        request, "Der tages 10 nye billeder fra videoen. Genindlæs siden om et øjeblik for at se dem."
    )
    return redirect(f"{reverse('gahkflix:edit', args=[video.pk])}#billeder")


@access.access_required
@require_POST
def publish(request: HttpRequest, pk: int) -> HttpResponse:
    """Publish a draft. The edit page sends its details along (the button belongs to that form), and
    what is typed there is saved first: a draft is never published under what it said before."""
    video = _editable(request, pk)
    if not video.is_draft:
        return redirect("gahkflix:detail", video.pk)
    resident = current_resident(request)
    if "title" in request.POST:
        form = VideoDetailsForm(request.POST, video=video)
        if not form.is_valid():
            return _edit_page(request, video, form)
        try:
            saved = services.edit_video(
                video, form.details(), resident, needs_approval=access.edit_needs_approval(video)
            )
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
            return _edit_page(request, video, form)
        for note in saved.notes:
            messages.warning(request, note)
        video.refresh_from_db()
    try:
        services.publish(video, resident)
    except ValidationError as exc:
        for message in exc.messages:
            messages.error(request, message)
        return redirect("gahkflix:edit", video.pk)
    messages.success(request, f"{video.title} er udgivet.")
    return redirect("gahkflix:detail", video.pk)


# --- History ------------------------------------------------------------------------------------


def _change_rows(
    event: VideoEvent, series_names: dict[int, str], in_use: dict[str, set[str]] | None
) -> list[dict[str, Any]]:
    """What a line of history changed, as rows of (what, before, after). A picture other than the
    one in use now (services.pictures_in_use) can be brought back from its row, by a reader who may
    edit the video (`in_use` given)."""
    rows: list[dict[str, Any]] = []
    for key, entry in event.changes.items():
        if not isinstance(entry, dict) or "after" not in entry:
            continue  # what the line says about itself: the file, the error
        if key in PICTURES:
            sides = {picture.side: picture for picture in services.pictures_named(event, key)}
            rows.append(
                {
                    "label": DETAIL_LABELS[key],
                    "picture": True,
                    "before": sides.get("before"),
                    "after": sides.get("after"),
                    "restorable": [
                        side
                        for side, picture in sides.items()
                        if in_use is not None and picture.origin not in in_use[key]
                    ],
                }
            )
            continue
        rows.append(
            {
                "label": DETAIL_LABELS.get(key, key),
                "before": _shown(key, entry.get("before"), series_names),
                "after": _shown(key, entry["after"], series_names),
            }
        )
    return rows


@access.access_required
def history(request: HttpRequest, pk: int) -> HttpResponse:
    """Everything that happened to a video: who changed what, and when, with every picture it had."""
    video = _visible_video(request, pk)
    if not access.can_view_history(request, video):
        raise Http404("no such video")
    can_edit = access.can_edit(request, video)
    in_use = {kind: services.pictures_in_use(video, kind) for kind in PICTURES} if can_edit else None
    series_names = dict(Series.objects.values_list("pk", "title"))
    events = list(video.events.select_related("actor", "request"))
    requests = video.change_requests.select_related("requested_by")
    if not access.is_administrator(request):
        # What other administrators asked or decided about it is theirs to read.
        requests = requests.filter(requested_by=current_resident(request))
    return _render(
        request,
        "gahkflix/history.html",
        {
            "video": video,
            "events": [
                {"event": event, "rows": _change_rows(event, series_names, in_use)} for event in events
            ],
            # A video uploaded before there was a history has only what happened since.
            "predates_history": not any(event.kind == VideoEvent.Kind.UPLOADED for event in events),
            "requests": requests,
            "can_edit": can_edit,
        },
    )


@access.access_required
def history_picture(request: HttpRequest, pk: int, event: int, kind: str, side: str) -> HttpResponseBase:
    video = _visible_video(request, pk)
    if not access.can_view_history(request, video) or kind not in PICTURES:
        raise Http404("no such picture")
    line = get_object_or_404(VideoEvent, pk=event, video=video)
    for picture in services.pictures_named(line, kind):
        if picture.side == side:
            return _picture_response(picture.key)
    raise Http404("no such picture")


# --- Deleting -----------------------------------------------------------------------------------


@access.access_required
@require_POST
def delete(request: HttpRequest, pk: int) -> HttpResponse:
    video = _visible_video(request, pk)
    if not access.can_delete_directly(request, video):
        raise PermissionDenied
    if video.is_draft:
        services.delete_permanently(video)
        messages.success(request, "Kladden er slettet.")
        return redirect("gahkflix:mine")
    services.bin_video(video, current_resident(request))
    messages.success(request, f"{video.title} er lagt i papirkurven. Den slettes helt om 30 dage.")
    return redirect("gahkflix:home")


@access.access_required
def request_deletion(request: HttpRequest, pk: int) -> HttpResponse:
    video = _visible_video(request, pk)
    if not access.can_request_deletion(request, video):
        raise PermissionDenied
    form = DeletionRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            change = services.request_deletion(video, current_resident(request), form.cleaned_data["reason"])
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Anmodningen er sendt. To andre administratorer skal godkende den.")
            return redirect("gahkflix:request", change.pk)
    return _render(request, "gahkflix/request_deletion.html", {"video": video, "form": form})


@access.access_required
@require_POST
def restore(request: HttpRequest, pk: int) -> HttpResponse:
    video = get_object_or_404(Video, pk=pk)
    if not access.can_restore(request, video):
        raise PermissionDenied
    services.restore(video, current_resident(request))
    messages.success(request, f"{video.title} er gendannet.")
    return redirect("gahkflix:bin")


@access.access_required
def drafts(request: HttpRequest) -> HttpResponse:
    """Every unpublished draft, for the administrators: who has one lying, and when it goes."""
    if not access.is_administrator(request):
        raise PermissionDenied
    videos = _with_latest_file(
        Video.objects.drafts().select_related("uploaded_by").order_by("created_at", "pk")
    )
    return _render(request, "gahkflix/drafts.html", {"videos": videos})


@access.access_required
def bin(request: HttpRequest) -> HttpResponse:
    if not access.is_administrator(request):
        raise PermissionDenied
    videos = (
        Video.objects.binned().select_related("file", "uploaded_by", "deleted_by").order_by("-deleted_at")
    )
    return _render(request, "gahkflix/bin.html", {"videos": videos})


# --- Requests -----------------------------------------------------------------------------------


def _readable_request(request: HttpRequest, pk: int) -> ChangeRequest:
    change = get_object_or_404(ChangeRequest.objects.select_related("video", "series", "requested_by"), pk=pk)
    if not (access.is_administrator(request) or change.requested_by_id == current_resident(request).pk):
        raise Http404("no such request")
    return change


@access.access_required
def approvals(request: HttpRequest) -> HttpResponse:
    resident = current_resident(request)
    if not access.is_administrator(request):
        return redirect("gahkflix:mine")
    open_requests = list(
        ChangeRequest.objects.filter(state=ChangeRequest.State.OPEN)
        .select_related("video", "series", "requested_by")
        .prefetch_related("votes__voter")
    )
    for change in open_requests:
        change.can_vote = access.can_vote(request, change)  # type: ignore[attr-defined]
        change.rows = describe(change)  # type: ignore[attr-defined]
    recent = (
        ChangeRequest.objects.exclude(state=ChangeRequest.State.OPEN)
        .filter(decided_at__gte=timezone.now() - timedelta(days=30))
        .select_related("video", "series", "requested_by")[:30]
    )
    return _render(
        request,
        "gahkflix/approvals.html",
        {"open_requests": open_requests, "recent": recent, "resident": resident},
    )


@access.access_required
def request_detail(request: HttpRequest, pk: int) -> HttpResponse:
    change = _readable_request(request, pk)
    return _render(
        request,
        "gahkflix/request_detail.html",
        {
            "change": change,
            "rows": describe(change),
            "votes": change.votes.select_related("voter"),
            "can_vote": access.can_vote(request, change),
            "can_withdraw": access.can_withdraw(request, change),
            "form": VoteForm(),
        },
    )


@access.access_required
@require_POST
def request_vote(request: HttpRequest, pk: int) -> HttpResponse:
    change = _readable_request(request, pk)
    if not access.can_vote(request, change):
        raise PermissionDenied
    form = VoteForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("unknown decision")
    try:
        change = services.vote(
            change,
            current_resident(request),
            approve=form.cleaned_data["decision"] == "approve",
            comment=form.cleaned_data["comment"],
        )
    except ValidationError as exc:
        for message in exc.messages:
            messages.error(request, message)
    else:
        if change.is_open:
            messages.success(request, "Din godkendelse er registreret. Der mangler stadig en.")
        else:
            messages.success(request, f"Anmodningen er {change.get_state_display().lower()}.")
    back = request.POST.get("next", "")
    return redirect(
        back if back.startswith("/intern/gahkflix/") else reverse("gahkflix:request", args=[change.pk])
    )


@access.access_required
@require_POST
def request_withdraw(request: HttpRequest, pk: int) -> HttpResponse:
    change = _readable_request(request, pk)
    if not access.can_withdraw(request, change):
        raise PermissionDenied
    services.withdraw(change)
    messages.success(request, "Anmodningen er trukket tilbage.")
    return redirect("gahkflix:request", change.pk)


@access.access_required
def request_picture(request: HttpRequest, pk: int, kind: str) -> HttpResponseBase:
    change = _readable_request(request, pk)
    proposed = change.changes.get(kind) if kind in PICTURES else None
    if not isinstance(proposed, dict):
        raise Http404("no such picture")
    return _picture_response(proposed["key"])


# What each detail is called where a change to it is shown: a proposal, the history.
DETAIL_LABELS = {
    "title": "Titel",
    "description": "Beskrivelse",
    "released_on": "Udgivelsesdato",
    "series": "Serie",
    "season": "Sæson",
    "episode": "Afsnit",
    "cast": "Medvirkende",
    "tags": "Tags",
    **{kind: names[0] for kind, names in PICTURES.items()},
}


def _shown(key: str, value: Any, series_names: dict[int, str]) -> str:  # noqa: ANN401 — any detail value
    """A detail's value (as services.snapshot has it) for people to read."""
    if value in (None, "", []):
        return "—"
    if key == "series":
        return series_names.get(value, "(slettet serie)")
    if key == "cast":
        return ", ".join(f"{m['name']} ({m['role']})" if m.get("role") else m["name"] for m in value)
    if key == "tags":
        return ", ".join(f"#{name}" for name in value)
    if key == "released_on":
        return dating.label(value) or str(value)
    return str(value)


def describe(change: ChangeRequest) -> list[dict[str, Any]]:
    """A proposal as rows of (what, before, after) for people to judge."""
    if change.kind == ChangeRequest.Kind.DELETE:
        return []
    if change.series is not None:
        current: dict[str, Any] = {"title": change.series.title, "description": change.series.description}
    elif change.video is not None:
        current = services.snapshot(change.video)
    else:
        return []
    series_names = dict(Series.objects.values_list("pk", "title"))
    rows = []
    for key, value in change.changes.items():
        if key in PICTURES:
            rows.append(
                {
                    "label": DETAIL_LABELS[key],
                    "picture": True,
                    "before": reverse("gahkflix:picture", args=[change.video_id, key])
                    if change.video_id
                    else "",
                    "after": reverse("gahkflix:request_picture", args=[change.pk, key]),
                }
            )
            continue
        rows.append(
            {
                "label": DETAIL_LABELS.get(key, key),
                "before": _shown(key, current.get(key), series_names),
                "after": _shown(key, value, series_names),
            }
        )
    return rows


# --- Lookups for the editors --------------------------------------------------------------------


@access.access_required
@require_GET
def residents_lookup(request: HttpRequest) -> JsonResponse:
    query = request.GET.get("q", "").strip()
    residents = Resident.objects.filter(is_active=True).exclude(first_name="System")
    for word in query.split()[:4]:
        residents = residents.filter(Q(first_name__icontains=word) | Q(last_name__icontains=word))
    found = residents.order_by("first_name", "last_name")[:10] if query else []
    return JsonResponse({"residents": [{"id": r.pk, "name": r.full_name} for r in found]})


@access.access_required
@require_GET
def tags_lookup(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"tags": services.search_tags(request.GET.get("q", ""))})


@access.access_required
def processing_states(request: HttpRequest) -> JsonResponse:
    """For "Mine videoer": the progress of the resident's uploads still being processed."""
    files = (
        current_resident(request)
        .gahkflix_files.filter(state__in=(FileState.QUEUED, FileState.PROCESSING))
        .values("video_id", "state", "progress")
    )
    return JsonResponse({"files": list(files)})
