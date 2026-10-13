"""Opslagstavlen — the noticeboard that replaces the kollegium's Facebook group.

Open to every logged-in resident: posting, commenting and reacting need no role, and Inspektionen
(plus administrator) can delete anyone's content and pin posts. See access.py for why there is no
staged-rollout gate.

**Deliberately not a chat.** Den Hurtige polls itself every 20 seconds, keeps the reader's scroll
position across swaps, re-opens threads afterwards and locks pinch-zoom — all of which exist because
its messages die in 30 minutes. None of it applies to a multi-year archive, and copying it would cost
real things: polling fights pagination, discards the reader's place, and re-renders every post's
Markdown server-side every 20 seconds per open tab. If "what is new since I last looked" ever turns
out to matter, the cheap honest version is a `last_seen` timestamp driving an "N nye opslag" banner —
not a poll.
"""

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import push
from core.emoji import EMOJI_SHORTLIST
from core.markdown import image_sources, render_markdown
from core.reactions import apply_toggle, reaction_rows
from core.uploads import attached_image, validate_image_upload
from residents.permissions import current_resident

from . import services
from .access import access_required, can_delete, can_delete_comment, can_edit, can_moderate, is_limited
from .forms import NoticeCommentForm, NoticeForm, ReactionForm
from .images import sync_images
from .models import (
    MAX_PINNED,
    Category,
    Notice,
    NoticeComment,
    NoticeImage,
    NoticeQuerySet,
    NoticeReaction,
)

# A post with a rendered body and an image is tall, so fewer per page than a table would take.
PAGE_SIZE = 15


def _decorate(notices: list[Notice], request: HttpRequest) -> list[Notice]:
    """Attach the per-request bits a template cannot work out for itself.

    A Django template cannot call a function with arguments, and every one of these depends on who
    is asking — the same reason den_hurtige.views.posts_for attaches `reaction_rows`.
    """
    # current_resident, not request.user: it narrows the type to a Resident (whose pk is an int),
    # which is the invariant every view here already relies on behind @access_required.
    user_id = current_resident(request).pk
    moderator = can_moderate(request)
    for notice in notices:
        notice.reaction_rows = reaction_rows(notice.reactions.all(), user_id)  # type: ignore[attr-defined]
        notice.can_edit = notice.author_id == user_id  # type: ignore[attr-defined]
        notice.can_delete = notice.author_id == user_id or moderator  # type: ignore[attr-defined]
        # Feed cards collapse a picture-heavy post: one thumbnail and a count instead of every image
        # inline, which otherwise pushes every other post below the fold. A single image is left
        # alone — it reads as part of the post rather than as a gallery. Computed here because a
        # template cannot call a function, and only used when `full` is unset.
        images = image_sources(notice.body)
        notice.image_count = len(images)  # type: ignore[attr-defined]
        notice.thumbnail = images[0] if len(images) > 1 else ""  # type: ignore[attr-defined]
    return notices


# Reactions with their author already joined. select_related inside the Prefetch rather than a
# nested "reactions__author": a nested prefetch is a SECOND query that only runs once an item has
# reactions, so the first reaction on a page would add a query — exactly what
# test_the_board_costs_no_extra_query_per_reaction forbids. The author is needed because the reader
# panel names people. Mirrors den_hurtige.views.REACTIONS.
REACTIONS = Prefetch("reactions", queryset=NoticeReaction.objects.select_related("author"))


def _list_queryset() -> NoticeQuerySet:
    """The columns and relations every listing needs. `prefetch_related("reactions")` is what keeps
    the reaction rows from being an N+1 across a page of posts."""
    return Notice.objects.select_related("author", "pinned_by").prefetch_related(REACTIONS)


@access_required
def board(request: HttpRequest) -> HttpResponse:
    """The board: pinned posts, then a page of the rest.

    Pinned posts are rendered above the paginator on EVERY page, not folded into the paginated
    queryset. Ordering them first inside the paginator would put them on page 1 only, which makes
    pinning useless the moment the board is a few pages deep — and it would also walk straight into
    the NULLS-ordering difference between Postgres (DESC => NULLS FIRST) and SQLite (NULLS LAST).
    Two queries, no portability trap, and the pin means the same thing wherever you are.
    """
    selected = request.GET.get("kategori") or ""
    if selected not in Category.values:
        selected = ""  # an unknown category shows everything rather than 404ing

    qs = _list_queryset()
    if selected:
        qs = qs.filter(category=selected)

    pinned = list(qs.pinned()[:MAX_PINNED])
    page = Paginator(qs.unpinned(), PAGE_SIZE).get_page(request.GET.get("page"))

    _decorate([*pinned, *page.object_list], request)
    return render(
        request,
        "opslagstavle/board.html",
        {
            "pinned": pinned,
            "page_obj": page,
            "categories": Category.choices,
            "selected_category": selected,
            "can_moderate": can_moderate(request),
            "events_allowed": events_allowed(request),
            "quick_emoji": EMOJI_SHORTLIST,
            "push_configured": push.is_configured(),
            "vapid_public_key": push.vapid_public_key(),
            "push_subscribed": services.is_subscribed(current_resident(request)),
            # Tells the trial group the board is not live yet, so they do not read the quiet as
            # nobody caring. Disappears on its own when ACCESS_ROLES is set to None.
            "limited_rollout": is_limited(),
        },
    )


def events_allowed(request: HttpRequest) -> bool:
    """Whether this reader can open begivenheder, which decides if a post shows its event chip.

    The mirror of what the event page does with its "Omtalt på opslagstavlen" section: two features
    with independent rollout gates must each check the OTHER before linking into it, or one of them
    ends up handing residents a link that 403s them. The two gates happen to hold the same roles
    today; they are separate globals and the whole point of them is that they move apart.

    Imported inside the function so the two apps stay acyclic at import time — opslagstavle.forms
    reaches into events the same way.
    """
    from events.access import request_allowed

    return request_allowed(request)


@access_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    """One post with its comments. Exists as a stable permalink, which is what a notification can
    link to — `?page=4` is not an address for a thing."""
    notice = get_object_or_404(_list_queryset().prefetch_related("comments__author"), pk=pk)
    _decorate([notice], request)
    comments = list(notice.comments.all())
    for comment in comments:
        comment.can_delete = can_delete_comment(request, comment)  # type: ignore[attr-defined]
    return render(
        request,
        "opslagstavle/detail.html",
        {
            "notice": notice,
            "comments": comments,
            "comment_form": NoticeCommentForm(),
            "can_moderate": can_moderate(request),
            "events_allowed": events_allowed(request),
            "quick_emoji": EMOJI_SHORTLIST,
        },
    )


@access_required
def create(request: HttpRequest) -> HttpResponse:
    """Compose on its own page, not inline: title + category + Markdown + toolbar + preview is a
    form, and an inline composer on a paginated list would lose a long draft on any navigation."""
    form = NoticeForm(request.POST or None, events_allowed=events_allowed(request))
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            notice = form.save(commit=False)
            notice.author = current_resident(request)
            notice.save()
            sync_images(notice)
        services.notify_new_notice(notice)
        messages.success(request, "Opslaget er slået op.")
        return redirect("opslagstavle:detail", pk=notice.pk)
    return render(request, "opslagstavle/form.html", {"form": form, "notice": None})


@access_required
def edit(request: HttpRequest, pk: int) -> HttpResponse:
    """The author fixes their own typos. Moderators may delete but never rewrite — see access.py."""
    notice = get_object_or_404(Notice, pk=pk)
    if not can_edit(request, notice):
        raise PermissionDenied
    form = NoticeForm(request.POST or None, instance=notice, events_allowed=events_allowed(request))
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            notice = form.save(commit=False)
            # Set explicitly, never auto_now: pinning also saves the row, and stamping "Redigeret"
            # with Inspektionen's action would be a lie to every reader.
            notice.edited_at = timezone.now()
            notice.save()
            sync_images(notice)
        messages.success(request, "Opslaget er opdateret.")
        return redirect("opslagstavle:detail", pk=notice.pk)
    return render(request, "opslagstavle/form.html", {"form": form, "notice": notice})


@require_POST
@access_required
def delete(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    notice = get_object_or_404(Notice, pk=pk)
    if not can_delete(request, notice):
        raise PermissionDenied
    notice.delete()  # images follow by CASCADE + the post_delete receiver
    messages.success(request, "Opslaget er slettet.")
    return redirect("opslagstavle:board")


@require_POST
@access_required
def toggle_pin(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Pin or unpin. Inspektionen and administrator only.

    Capped at MAX_PINNED because a pinned post sits permanently above everything else: without a
    cap the top of the board fills up and pinning stops meaning anything. (It used to also exempt a
    post from the retention purge; there is no retention now, so pinning is purely prominence.)
    Enforced here rather than as a constraint — a cross-row rule would need an exclusion constraint
    or a trigger, which is a lot of machinery for one `if`.
    """
    if not can_moderate(request):
        raise PermissionDenied
    notice = get_object_or_404(Notice, pk=pk)

    if notice.is_pinned:
        notice.pinned_at = None
        notice.pinned_by = None
        notice.save(update_fields=["pinned_at", "pinned_by"])
        messages.success(request, "Opslaget er ikke længere fastgjort.")
    else:
        if Notice.objects.pinned().count() >= MAX_PINNED:
            messages.error(
                request,
                f"Der kan højst være {MAX_PINNED} fastgjorte opslag. Frigør et andet først.",
            )
            return redirect("opslagstavle:detail", pk=notice.pk)
        notice.pinned_at = timezone.now()
        notice.pinned_by = current_resident(request)
        notice.save(update_fields=["pinned_at", "pinned_by"])
        messages.success(request, "Opslaget er fastgjort.")
    return redirect("opslagstavle:detail", pk=notice.pk)


@require_POST
@access_required
def create_comment(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """A comment, optionally with one photo — and a photo on its own is a whole comment.

    THE IMAGE IS VALIDATED OUTSIDE THE FORM, deliberately, and it is why the form no longer
    rejects an empty body. A refused picture (an SVG, something over the limit) must not throw
    away the text somebody typed beside it, so `core.uploads.attached_image` warns and the picture is
    dropped — the same call den_hurtige.views makes, for the same reason.

    That leaves one case the form cannot see: a photo-only comment whose photo was refused now has
    nothing in it at all. It lands in the empty branch below, which is the right place, but the two
    messages have to arrive TOGETHER or the picture appears to have silently not counted. Django's
    message framework carries both through the redirect.
    """
    notice = get_object_or_404(Notice, pk=pk)
    form = NoticeCommentForm(request.POST)
    # NOTICE_IMAGE_MAX_MB, so a comment photo and a post photo share one ceiling.
    image = attached_image(request, settings.NOTICE_IMAGE_MAX_MB)

    if not form.is_valid():
        messages.error(request, "Kommentaren kunne ikke gemmes.")
        return redirect("opslagstavle:detail", pk=notice.pk)

    comment = form.save(commit=False)
    if not comment.body and image is None:
        messages.error(request, "Skriv en kommentar, eller vedhæft et billede.")
        return redirect("opslagstavle:detail", pk=notice.pk)

    comment.notice = notice
    comment.author = current_resident(request)
    if image is not None:
        comment.image = image
    comment.save()
    services.notify_new_comment(comment)
    return redirect("opslagstavle:detail", pk=notice.pk)


@require_POST
@access_required
def delete_comment(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    comment = get_object_or_404(NoticeComment, pk=pk)
    if not can_delete_comment(request, comment):
        raise PermissionDenied
    notice_pk = comment.notice_id
    comment.delete()
    messages.success(request, "Kommentaren er slettet.")
    return redirect("opslagstavle:detail", pk=notice_pk)


@require_POST
@access_required
def toggle_reaction(request: HttpRequest, pk: int) -> HttpResponse:
    """Set, change or clear this person's one emoji, returning just the reaction row.

    Renders only the partial so a tap never re-renders the page — which would collapse the comment
    form and lose anything half-typed in it. Deliberately silent: no notification, ever.
    """
    notice = get_object_or_404(Notice, pk=pk)
    resident = current_resident(request)
    form = ReactionForm(request.POST)
    if form.is_valid():
        apply_toggle(NoticeReaction.objects, author=resident, emoji=form.cleaned_data["emoji"], notice=notice)
    # An invalid emoji falls through to a plain re-render: the row is still correct, and a one-tap
    # control has nowhere useful to put a validation error.
    # NOT prefetched, and NOT reactions_for(): a prefetch is evaluated when the item is fetched,
    # which here is *before* apply_toggle writes. Reading .reactions.all() would then hit a cache
    # built a moment too early and re-render the row exactly as it was before the tap. Ask the
    # database again, joining the author in one query for the reader panel.
    return render(
        request,
        "opslagstavle/_reactions.html",
        {
            "notice": notice,
            "reactions": reaction_rows(notice.reactions.select_related("author"), resident.pk),
            "quick_emoji": EMOJI_SHORTLIST,
        },
    )


@require_POST
@access_required
def preview(request: HttpRequest) -> HttpResponse:
    """Render the compose form's Markdown exactly as the reader will see it.

    Server-side on purpose. A client-side Markdown renderer would be a *second* implementation with
    a second allowlist, and the first time they disagreed the author would see one thing and the
    board would show another — the classic "the preview kept my HTML, the saved post stripped it"
    bug. Going through core.markdown makes the preview byte-identical to the save by construction,
    and there is a test asserting exactly that.
    """
    return render(
        request, "opslagstavle/_preview.html", {"html": render_markdown(request.POST.get("body", ""))}
    )


@require_POST
@access_required
def upload_image(request: HttpRequest) -> JsonResponse:
    """Store one image and hand back the URL for the toolbar to insert.

    Same JSON shape and status codes as the CMS toolbar (cms/admin.py): 201 {url, alt}, 400 {error}.
    Strict validation, unlike Den Hurtige's warn-and-drop — a rejection here is a 400 to a fetch that
    the author sees immediately, not a lost message.
    """
    upload = request.FILES.get("file")
    if not upload:
        return JsonResponse({"error": "Vælg en fil."}, status=400)
    try:
        validate_image_upload(upload, settings.NOTICE_IMAGE_MAX_MB)
    except ValidationError as exc:
        return JsonResponse({"error": " ".join(exc.messages)}, status=400)

    image = NoticeImage.objects.create(
        file=upload,
        alt=(request.POST.get("alt") or "").strip(),
        uploaded_by=current_resident(request),
    )
    # notice stays NULL until a save claims it — see images.sync_images.
    return JsonResponse({"url": image.url, "alt": image.alt}, status=201)


@require_POST
@access_required
def save_subscription(request: HttpRequest) -> HttpResponse:
    """Store (or drop) this browser's opt-in to board notifications.

    No role gate beyond login: every resident may use the board, which is exactly why the shared
    endpoint could not stay behind Den Hurtige's rollout check.
    """
    return push.handle_subscription_request(request, services.TOPIC)
