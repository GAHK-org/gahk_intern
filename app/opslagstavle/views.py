"""Opslagstavlen — the noticeboard that replaces the kollegium's Facebook group.

Open to every logged-in resident: posting, commenting and reacting need no role, and Inspektionen
(plus administrator) can delete anyone's content and pin posts. See access.py for why there is no
staged-rollout gate.

**Deliberately not a chat.** Den Hurtige locks pinch-zoom and follows the bottom of the
conversation, because its messages die in hours. None of that applies to a multi-year archive.

**It is live, and it does not poll.** A poll was rejected here for good reasons — it fights the
pager, discards the reader's place, and re-renders every post's Markdown every few seconds per open
tab whether or not anything happened. A WebSocket signal (opslagstavle.live) answers all three: a
page re-fetches only when something changed, only the cards it is showing, and a NEW post is an
"N nye opslag" banner rather than an insertion, so nothing moves under the reader.
"""

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max, Prefetch
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.emoji import EMOJI_SHORTLIST
from core.markdown import embed_references, image_sources, render_markdown
from core.reactions import CREATED, apply_toggle, reaction_rows
from core.uploads import attached_image, validate_image_upload
from residents.permissions import current_resident, is_kitchen

from . import embeds, live, services
from .access import (
    access_required,
    can_delete,
    can_delete_comment,
    can_edit,
    can_moderate,
    is_limited,
    request_allowed,
)
from .forms import NoticeCommentForm, NoticeForm, ReactionForm
from .images import sync_images
from .models import (
    MAX_BODY_CHARS,
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
    # The widgets for the albums and films the posts embed, as this reader may see them: for the
    # whole page at once, so they cost the same few queries however many posts embed something.
    embeds.attach(request, notices)
    return notices


# Reactions with their author already joined. select_related inside the Prefetch rather than a
# nested "reactions__author": a nested prefetch is a SECOND query that only runs once an item has
# reactions, so the first reaction on a page would add a query — exactly what
# test_the_board_costs_no_extra_query_per_reaction forbids. The author is needed because the reader
# panel names people. Mirrors den_hurtige.views.REACTIONS.
REACTIONS = Prefetch("reactions", queryset=NoticeReaction.objects.select_related("author"))


def _visible(request: HttpRequest) -> NoticeQuerySet:
    """The posts this reader may see: all of them, except that the kitchen account sees only its own."""
    if is_kitchen(request):
        return Notice.objects.filter(author=current_resident(request))
    return Notice.objects.all()


def _list_queryset(request: HttpRequest) -> NoticeQuerySet:
    """The columns and relations every listing needs. `prefetch_related("reactions")` is what keeps
    the reaction rows from being an N+1 across a page of posts."""
    return _visible(request).select_related("author", "pinned_by").prefetch_related(REACTIONS)


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

    qs = _list_queryset(request)
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
            # The newest post that existed when this page was drawn, whatever the filter: the cursor
            # a reconnecting board hands live_cards to learn what it missed (`?siden=`).
            "newest_pk": _visible(request).aggregate(newest=Max("pk"))["newest"] or 0,
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

    # Begivenheder is closed to the kitchen account by core.middleware, which the gate cannot see.
    return request_allowed(request) and not is_kitchen(request)


@access_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    """One post with its comments. Exists as a stable permalink, which is what a notification can
    link to — `?page=4` is not an address for a thing."""
    return render(request, "opslagstavle/detail.html", _detail_context(request, pk))


def _detail_context(request: HttpRequest, pk: int) -> dict[str, object]:
    """Everything the detail page draws, shared with `live_detail` so the two cannot differ."""
    notice = get_object_or_404(_list_queryset(request).prefetch_related("comments__author"), pk=pk)
    _decorate([notice], request)
    comments = list(notice.comments.all())
    for comment in comments:
        comment.can_delete = can_delete_comment(request, comment)  # type: ignore[attr-defined]
    return {
        "notice": notice,
        "comments": comments,
        "comment_form": NoticeCommentForm(),
        "can_moderate": can_moderate(request),
        "events_allowed": events_allowed(request),
        "quick_emoji": EMOJI_SHORTLIST,
    }


# --- what a live signal re-fetches (opslagstavle.live, frontend/src/opslagstavle-live.ts) ---------
#
# Both are fragments and both are NOT decorated, for the reason den_hurtige.views.feed_items gives:
# @access_required redirects an expired session to the login page, fetch follows the redirect, and
# the page would be handed a login form to read cards out of. They answer 204 instead, which the
# client takes as "leave everything as it is".
#
# Neither renders `messages`, and that is load-bearing rather than tidy. Django's message storage is
# consumed by whatever iterates it, and these requests fire on their own, from a socket, at moments
# the reader did not choose — including the instant after they deleted a comment, when "Kommentaren
# er slettet." is waiting for the redirect's page. A fragment that drew the messages would eat it.

# The most cards one request draws: everything a board page can show at once.
MAX_LIVE_CARDS = MAX_PINNED + PAGE_SIZE

# The most "new since" pks one answer lists. Past this the banner reads "99+ nye opslag", which is
# as precise as anyone needs a board they have not looked at for that long to be.
MAX_NEW_SINCE = 99


def live_cards(request: HttpRequest) -> HttpResponse:
    """The cards for `?id=…&id=…`, drawn exactly as the board draws them.

    A card that is not in the answer has been deleted, and the board removes it — which is how a
    board that was asleep catches up on deletions it never heard about. So the ids are filtered,
    never refused: a junk id simply is not a card.

    `?siden=<pk>` (with the board's `?kategori=`) also lists the posts created after that one, for
    the same reconnecting board: what it missed while asleep was never signalled, so it asks, and
    fills its "N nye opslag" banner from the answer.
    """
    if not request_allowed(request):
        return HttpResponse(status=204)
    ids = [int(raw) for raw in request.GET.getlist("id") if raw.isdigit()][:MAX_LIVE_CARDS]
    notices = _decorate(list(_list_queryset(request).filter(pk__in=ids)), request)
    since = request.GET.get("siden", "")
    new_since: list[int] | None = None
    if since.isdigit():
        newer = _visible(request).filter(pk__gt=int(since))
        category = request.GET.get("kategori") or ""
        if category in Category.values:
            newer = newer.filter(category=category)
        new_since = list(newer.order_by("-pk").values_list("pk", flat=True)[:MAX_NEW_SINCE])
    return render(
        request,
        "opslagstavle/_live_cards.html",
        {
            "notices": notices,
            "new_since": new_since,
            "can_moderate": can_moderate(request),
            "events_allowed": events_allowed(request),
            "quick_emoji": EMOJI_SHORTLIST,
        },
    )


def live_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """The detail page's live region: the post and its comments. 404 once the post is gone, which
    the page answers by saying so (opslagstavle-live.ts)."""
    if not request_allowed(request):
        return HttpResponse(status=204)
    return render(request, "opslagstavle/_detail_live.html", _detail_context(request, pk))


@access_required
def create(request: HttpRequest) -> HttpResponse:
    """Compose on its own page, not inline: title + category + Markdown + toolbar + preview is a
    form, and an inline composer on a paginated list would lose a long draft on any navigation.

    `?album=` or `?gahkflix=` is "Del på opslagstavlen" on that album's or film's page: the body starts
    with it embedded, below an empty line, and the cursor sits in that line for the text.
    """
    shared = embeds.shared_body(request) if request.method == "GET" else ""
    form = NoticeForm(
        request.POST or None,
        initial={"body": shared} if shared else None,
        events_allowed=events_allowed(request),
    )
    if shared:
        form.fields["body"].widget.attrs["autofocus"] = True
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            notice = form.save(commit=False)
            notice.author = current_resident(request)
            notice.save()
            sync_images(notice)
        mentioned = services.notify_mentions_in_notice(notice)
        services.notify_new_notice(notice, skip=mentioned)
        live.notice_created(notice)
        messages.success(request, "Opslaget er slået op.")
        return redirect("opslagstavle:detail", pk=notice.pk)
    return _form_page(request, form, None)


@access_required
def edit(request: HttpRequest, pk: int) -> HttpResponse:
    """The author fixes their own typos. Moderators may delete but never rewrite — see access.py."""
    notice = get_object_or_404(_visible(request), pk=pk)
    if not can_edit(request, notice):
        raise PermissionDenied
    old_body = notice.body
    form = NoticeForm(request.POST or None, instance=notice, events_allowed=events_allowed(request))
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            notice = form.save(commit=False)
            # Set explicitly, never auto_now: pinning also saves the row, and stamping "Redigeret"
            # with Inspektionen's action would be a lie to every reader.
            notice.edited_at = timezone.now()
            notice.save()
            sync_images(notice)
        services.notify_mentions_in_notice(notice, old_body)
        live.notice_changed(notice.pk)
        messages.success(request, "Opslaget er opdateret.")
        return redirect("opslagstavle:detail", pk=notice.pk)
    return _form_page(request, form, notice)


def _form_page(request: HttpRequest, form: NoticeForm, notice: Notice | None) -> HttpResponse:
    return render(
        request,
        "opslagstavle/form.html",
        {
            "form": form,
            "notice": notice,
            # Under the textarea, re-drawn as it is typed (views.embedded).
            "embedded": embeds.for_body(request, form["body"].value() or "", compose=True),
            "albums_allowed": embeds.albums_allowed(request),
            "videos_allowed": embeds.videos_allowed(request),
        },
    )


@require_POST
@access_required
def delete(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    notice = get_object_or_404(_visible(request), pk=pk)
    if not can_delete(request, notice):
        raise PermissionDenied
    notice_pk = notice.pk  # delete() clears it
    notice.delete()  # images follow by CASCADE + the post_delete receiver
    live.notice_deleted(notice_pk)
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
    notice = get_object_or_404(_visible(request), pk=pk)

    if notice.is_pinned:
        notice.pinned_at = None
        notice.pinned_by = None
        notice.save(update_fields=["pinned_at", "pinned_by"])
        live.notice_changed(notice.pk)
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
        live.notice_changed(notice.pk)
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
    notice = get_object_or_404(_visible(request), pk=pk)
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
    live.notice_changed(notice.pk)
    return redirect("opslagstavle:detail", pk=notice.pk)


@require_POST
@access_required
def delete_comment(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    comment = get_object_or_404(NoticeComment, pk=pk)
    if not can_delete_comment(request, comment):
        raise PermissionDenied
    notice_pk = comment.notice_id
    comment.delete()
    live.notice_changed(notice_pk)
    messages.success(request, "Kommentaren er slettet.")
    return redirect("opslagstavle:detail", pk=notice_pk)


@require_POST
@access_required
def toggle_reaction(request: HttpRequest, pk: int) -> HttpResponse:
    """Set, change or clear this person's one emoji, returning just the reaction row.

    Renders only the partial so a tap never re-renders the page — which would collapse the comment
    form and lose anything half-typed in it. A NEW reaction tells the post's author (grouped, see
    services.notify_reaction); changing or clearing one tells nobody.
    """
    notice = get_object_or_404(_visible(request), pk=pk)
    resident = current_resident(request)
    form = ReactionForm(request.POST)
    if form.is_valid():
        emoji = form.cleaned_data["emoji"]
        if apply_toggle(NoticeReaction.objects, author=resident, emoji=emoji, notice=notice) == CREATED:
            services.notify_reaction(notice, resident, emoji)
        live.notice_changed(notice.pk)
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
    and there is a test asserting exactly that. The albums and films it embeds are drawn where they
    stand, as on the post's own page.
    """
    body = request.POST.get("body", "")[:MAX_BODY_CHARS]
    widgets = embeds.rendered(request, embed_references(body), compose=True)
    return render(request, "opslagstavle/_preview.html", {"html": render_markdown(body, embeds=widgets)})


@require_POST
@access_required
def embedded(request: HttpRequest) -> HttpResponse:
    """The widgets the compose form's body embeds, re-drawn under the textarea as it is typed, each
    with a "Fjern" that takes its embed out of the text again (opslagstavle.ts)."""
    body = request.POST.get("body", "")[:MAX_BODY_CHARS]
    return render(
        request, "opslagstavle/_embedded.html", {"embedded": embeds.for_body(request, body, compose=True)}
    )


@access_required
def embed_choices(request: HttpRequest, kind: str) -> HttpResponse:
    """The picker's list (form.html): the newest albums or films, or those matching `?q=`."""
    query = request.GET.get("q", "").strip()[:100]
    found = embeds.KINDS[kind]
    choices = found.choices(request, query) if found.allowed(request) else []
    return render(
        request, "opslagstavle/_embed_choices.html", {"kind": kind, "query": query, "choices": choices}
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
        upload = validate_image_upload(upload, settings.NOTICE_IMAGE_MAX_MB)
    except ValidationError as exc:
        return JsonResponse({"error": " ".join(exc.messages)}, status=400)

    image = NoticeImage.objects.create(
        file=upload,
        alt=(request.POST.get("alt") or "").strip(),
        uploaded_by=current_resident(request),
    )
    # notice stays NULL until a save claims it — see images.sync_images.
    return JsonResponse({"url": image.url, "alt": image.alt}, status=201)
