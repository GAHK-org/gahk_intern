"""Den Hurtige — the intern feed of short-lived urgent messages that replaces the Messenger group.

Anyone who can reach it may post, comment and subscribe to push — but access itself is gated by
den_hurtige.access, and individual channels may narrow that further (den_hurtige.channels). Posts
self-destruct:
`feed` purges expired ones on the way in, so the thread cannot accumulate the off-topic history
admins struggled with on Messenger. Notification fan-out lives in services.py and runs off the
request thread.

Every view here is scoped to one channel, with one deliberate exception: the purge. See
`_active_posts`.
"""

from datetime import timedelta
from typing import NamedTuple

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import UploadedFile
from django.db.models import Count, Prefetch, QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.push import handle_subscription_request
from core.reactions import apply_toggle, reaction_rows
from core.uploads import attached_image
from residents.permissions import current_resident, effective_roles

from . import channels, services
from .access import access_required, can_moderate, is_limited, request_allowed
from .channels import Channel
from .forms import ReactionForm
from .models import (
    DURATION_CHOICES,
    QUICK_EMOJI,
    ChannelMute,
    QuickComment,
    QuickPost,
    QuickReaction,
)

# A "hurtig" message is a couple of lines, not an essay. Enforced server-side as well as via the
# textarea's maxlength so a crafted POST cannot turn the feed into a noticeboard.
MAX_CONTENT_CHARS = 500

VALID_DURATIONS = {minutes for minutes, _label in DURATION_CHOICES}

# Messages from the same person closer together than this are drawn as one group, the way any
# chat client collapses a burst from one sender.
GROUPING_WINDOW = timedelta(minutes=5)


# Reactions with their author already joined. select_related inside the Prefetch rather than a
# nested "reactions__author" on purpose: a nested prefetch is a SECOND query that only runs when the
# item actually has reactions, so adding the first reaction to a page would add a query — precisely
# what test_the_feed_costs_no_extra_query_per_reaction forbids. Joining keeps it at one query
# whether there are reactions or none. The author is needed because the reader panel names people.
REACTIONS = Prefetch("reactions", queryset=QuickReaction.objects.select_related("author"))


def _active_posts(channel: Channel) -> QuerySet[QuickPost]:
    """One channel's live posts, with expired ones purged on the way past. Traffic does the cleanup;
    the purge_quick_posts cron job (DEPLOY.md §4b) covers the weeks with none.

    The purge is deliberately NOT scoped to `channel`. Filtering it would mean a channel nobody has
    opened this week keeps its expired posts — and their images — until the half-hourly cron gets to
    them, which quietly turns "the post is gone in an hour" into "the post is gone in an hour, in
    the busy channel". Sweep everything, then read one channel.
    """
    QuickPost.objects.purge_expired()
    return (
        QuickPost.objects.filter(channel=channel.slug)
        .active()
        .select_related("author")
        .prefetch_related(REACTIONS)
        .annotate(reply_count=Count("comments"))
        # Chat order: oldest first, newest at the bottom by the composer. QuickPost.Meta.ordering
        # stays newest-first for the admin and everything else that lists posts as records.
        .order_by("created_at")
    )


def reactions_for(post: QuickPost, user_id: int) -> list[dict[str, object]]:
    """[{emoji, count, mine}] for one message, most-used first.

    A thin adapter over core.reactions.reaction_rows, which holds the counting and ordering shared
    with opslagstavlen. Kept as a named function here because the feed and the toggle both call it
    with a post, and because tests import it from this module.
    """
    return reaction_rows(post.reactions.all(), user_id)


def posts_for(request: HttpRequest, channel: Channel) -> list[QuickPost]:
    """One channel's active messages, each carrying `reaction_rows` for the template.

    Attached here rather than resolved in the template because a Django template cannot call a
    function with arguments, and the "did *I* react?" flag depends on the current user.
    """
    user_id = current_resident(request).pk
    posts = list(_active_posts(channel))
    previous: QuickPost | None = None
    for post in posts:
        post.reaction_rows = reactions_for(post, user_id)  # type: ignore[attr-defined]
        # Same author, close in time → render as a continuation (no repeated name).
        post.grouped = bool(  # type: ignore[attr-defined]
            previous
            and previous.author_id == post.author_id
            and post.created_at - previous.created_at < GROUPING_WINDOW
        )
        # `grouped` alone cannot draw a bubble. It says "something of mine is above me", which is
        # enough to decide the NAME and the top corners, and nothing else: the avatar sits at the
        # bottom of a run and the tail hangs off its last bubble, so both need "nothing of mine is
        # below me" — a fact about the NEXT message, which a Django template cannot look ahead to.
        #
        # Set on the previous post from inside the same pass rather than in a second loop: the
        # answer for post N-1 is exactly `not posts[N].grouped`, which has just been computed.
        if previous is not None:
            previous.group_end = not post.grouped  # type: ignore[attr-defined]
        previous = post
    # The last message of the list ends its run by definition — there is no next message to break
    # it. Without this the newest message on the feed is the one with no avatar and no tail.
    if previous is not None:
        previous.group_end = True  # type: ignore[attr-defined]
    return posts


def _channel_or_404(request: HttpRequest, slug: str | None) -> Channel:
    """Resolve a channel for a full page view, or 404.

    An unknown slug and a channel this resident may not open answer the same way on purpose: a 403
    would confirm that "inspektion-internt" exists to someone who cannot read it.
    """
    channel = channels.lookup(slug)
    if channel is None or not channels.allowed(channel, effective_roles(request)):
        raise Http404("Ingen kanal med det navn.")
    return channel


def _post_or_404(request: HttpRequest, pk: int, *, active_only: bool = True) -> QuickPost:
    """One post this resident is actually allowed to see, or 404.

    The channel check is the point. Every per-post endpoint here resolves a post by primary key
    alone, which says nothing about whether the caller may read the CHANNEL it lives in -- and
    channels can be role-restricted (channels.Channel.roles). Without this, guessing a pk reaches a
    post in a channel the sidebar will not even advertise.

    It mattered less while the per-post endpoints were all writes: you could react to or comment on
    a post you could not find. den_hurtige:thread makes it a READ, which is the version that leaks.
    Routed through one helper so the four cannot drift apart on the answer.

    404 rather than 403 for the same reason as _channel_or_404: a 403 confirms the post exists.

    `active_only=False` is for delete_post alone, which has always accepted a post that has expired
    but not yet been swept (purge_expired runs on feed loads, so there is a window). Refusing there
    would answer 404 to a moderator pressing a delete button that is still on their screen.
    """
    manager = QuickPost.objects.active() if active_only else QuickPost.objects.all()
    post = get_object_or_404(manager, pk=pk)
    channel = channels.lookup(post.channel)
    if channel is None or not channels.allowed(channel, effective_roles(request)):
        raise Http404("Ingen besked med det id.")
    return post


def channel_counts() -> dict[str, int]:
    """{slug: live post count} for the tab strip, in one query.

    Not an unread count — that would need per-user last-seen state written on every feed load, and
    an out-of-band swap to keep the strip fresh against the 20-second poll. For a feed whose posts
    expire, "how much is live in there right now" is the more honest number anyway, and the push
    notification remains the signal that something actually happened.
    """
    rows = QuickPost.objects.active().values("channel").annotate(n=Count("id"))
    return {row["channel"]: row["n"] for row in rows}


class Tab(NamedTuple):
    """One entry in the channel strip. Built here rather than looked up in the template, because a
    Django template cannot index a dict by a variable key — the alternative was a templatetags
    package existing solely to provide `get_item`."""

    channel: Channel
    # Not `count`: a NamedTuple field of that name shadows tuple.count, which mypy rejects outright.
    live: int
    current: bool


def _channel_context(request: HttpRequest, channel: Channel) -> dict[str, object]:
    """The tab strip's data. Read by the page and by nothing else — the poll swaps only the message
    list, so the counts refresh on navigation rather than every 20 seconds."""
    counts = channel_counts()
    return {
        "channel": channel,
        "tabs": [
            Tab(c, counts.get(c.slug, 0), c.slug == channel.slug)
            for c in channels.visible(effective_roles(request))
        ],
        "muted": ChannelMute.objects.filter(
            resident=current_resident(request), channel=channel.slug
        ).exists(),
    }


def _requested_thread_pk(request: HttpRequest) -> int | None:
    """The ?traad= pk to pre-open, or None. Junk is None, never an error."""
    raw = request.GET.get("traad")
    if not raw or not raw.isdigit():
        return None
    return int(raw)


@access_required
def feed(request: HttpRequest, channel: str | None = None) -> HttpResponse:
    resolved = _channel_or_404(request, channel)
    return render(
        request,
        "den_hurtige/feed.html",
        {
            "posts": posts_for(request, resolved),
            "duration_choices": DURATION_CHOICES,
            # Per channel: a plan for tonight and a lost bike key go stale on different schedules.
            "default_duration": resolved.default_duration,
            "max_content_chars": MAX_CONTENT_CHARS,
            "push_configured": services.is_configured(),
            "vapid_public_key": services.vapid_public_key(),
            # Whether ANY of this resident's devices wants this topic. The browser cannot answer it
            # (one endpoint serves every topic), so the toggle's initial state is rendered here.
            "push_subscribed": services.is_subscribed(current_resident(request)),
            "quick_emoji": QUICK_EMOJI,
            "can_moderate": can_moderate(request),
            # Tells the testers the page is not live yet, so they do not assume silence means
            # nobody cares. Disappears on its own when ACCESS_ROLES is set to None.
            "limited_rollout": is_limited(),
            # ?traad=<pk> opens that thread's panel on load. This is the URL a reply notification
            # deep-links to (services.notify_new_comment): the standalone thread page would also
            # work, but landing in the CHANNEL with the thread open is what somebody tapping
            # "Anders svarede" actually wants -- they get the conversation and the feed behind it.
            #
            # Not validated here on purpose: the panel's own request goes through views.thread,
            # which does the channel check and answers a "gone" notice for anything else. Rejecting
            # a stale pk here would mean 404ing a whole channel over a message that just expired.
            "open_thread_pk": _requested_thread_pk(request),
            **_channel_context(request, resolved),
        },
    )


def feed_items(request: HttpRequest) -> HttpResponse:
    """Just the post list, for the 20-second poll that keeps the feed live.

    Deliberately not decorated: @access_required redirects or raises, and htmx would swap the login
    page (or a 403 body) into the middle of the feed. A 204 makes htmx do nothing instead, and hands
    an unauthorised caller no data either way — so this is a quieter gate, not a weaker one.

    The channel arrives as a query parameter rather than a path segment: this is a partial, never a
    URL anyone shares, and keeping it off the path meant the poll wiring in feed.html changed by one
    attribute instead of the whole route. An unknown or forbidden channel takes the same 204 exit as
    an expired session, for the same reason — the alternative is a 404 body swapped into the feed.
    """
    if not request_allowed(request):
        return HttpResponse(status=204)
    channel = channels.lookup(request.GET.get("kanal"))
    if channel is None or not channels.allowed(channel, effective_roles(request)):
        return HttpResponse(status=204)
    return render(
        request,
        "den_hurtige/_posts.html",
        {
            "posts": posts_for(request, channel),
            "max_content_chars": MAX_CONTENT_CHARS,
            "quick_emoji": QUICK_EMOJI,
            "can_moderate": can_moderate(request),
        },
    )


def thread(request: HttpRequest, pk: int) -> HttpResponseBase:
    """One message and its replies: the side panel, or a standalone page without htmx.

    TWO exits, on purpose, and they differ in how they FAIL as much as in what they render:

      * htmx (HX-Request) gets the _thread.html fragment, swapped into #js-thread -- and, when the
        gate says no, a 204. Same reason as feed_items: @access_required redirects an expired
        session to the login page, htmx follows the redirect, and the login form lands inside the
        panel. A 204 makes htmx do nothing, and hands an unauthorised caller no data either way.
      * anything else goes through @access_required like every other page, so a plain resident gets
        the 403 the rollout gate promises. That is the no-JS path behind the "N svar" anchor's
        href, and what a reply push notification deep-links to.
    """
    if request.headers.get("HX-Request"):
        if not request_allowed(request):
            return HttpResponse(status=204)
        return _render_thread(request, pk, fragment=True)
    return _thread_page(request, pk)


@access_required
def _thread_page(request: HttpRequest, pk: int) -> HttpResponse:
    """The page half of `thread`, split out only so the decorator applies to it alone."""
    return _render_thread(request, pk, fragment=False)


def _render_thread(request: HttpRequest, pk: int, *, fragment: bool) -> HttpResponse:
    """Shared body of both halves.

    An EXPIRED (or forbidden) post splits them again. The fragment renders a short "this is gone"
    notice with NO hx-trigger on it, so the panel's own poll stops rather than asking for a deleted
    message every five seconds at a reader who is still looking at it. The page raises 404, because
    a deep link to a message that no longer exists genuinely leads nowhere.

    Replies are prefetched HERE rather than in _active_posts: one post's worth instead of every
    post's, on a request that only happens when somebody opens a thread.
    """
    post = (
        QuickPost.objects.active()
        .filter(pk=pk)
        .select_related("author")
        .prefetch_related("comments__author", REACTIONS)
        .first()
    )
    if post is not None:
        channel = channels.lookup(post.channel)
        # Same answer as "gone" for a channel this resident may not read: never confirm that a
        # restricted channel has a message with this id. Mirrors _post_or_404.
        if channel is None or not channels.allowed(channel, effective_roles(request)):
            post = None

    if post is None:
        if fragment:
            return render(request, "den_hurtige/_thread.html", {"post": None})
        raise Http404("Ingen besked med det id.")

    post.reaction_rows = reactions_for(post, current_resident(request).pk)  # type: ignore[attr-defined]
    context = {
        "post": post,
        "channel": channels.lookup(post.channel),
        "max_content_chars": MAX_CONTENT_CHARS,
        "quick_emoji": QUICK_EMOJI,
        "can_moderate": can_moderate(request),
    }
    template = "den_hurtige/_thread.html" if fragment else "den_hurtige/thread.html"
    return render(request, template, context)


def _validated_image(request: HttpRequest) -> UploadedFile | None:
    """The uploaded image, or None with a warning shown — this feature's ceiling applied.

    A backstop, not the main defence: imageupload.ts already downscales in the browser. This rejects
    a crafted or oversized upload, and warns rather than failing the whole submission — losing an
    urgent message because the photo was wrong is the worse outcome. Shared by messages and replies
    so the two can never drift apart on what they accept.

    The body moved to core.uploads.attached_image when opslagstavlen and begivenheder turned out to
    have copied it; what is left here is the name its two callers use and QUICK_POST_MAX_MB. Kept as
    a wrapper rather than inlined at both call sites so the "messages and replies cannot drift"
    guarantee above stays a single line of code rather than a convention.
    """
    return attached_image(request, settings.QUICK_POST_MAX_MB)


def _channel_of(post: QuickPost) -> str:
    """Where to send someone back to after acting on `post`. A post filed under a channel that has
    since been retired from the registry lands on the default feed rather than a dead URL."""
    channel = channels.lookup(post.channel) or channels.DEFAULT
    return channel.url


def _posting_channel(request: HttpRequest) -> Channel:
    """The channel a submitted form belongs to.

    Falls back to the default rather than erroring, exactly as an unrecognised `duration` is coerced
    below: a resident who has typed an urgent message should not lose it to a hidden field they
    never saw. A channel they may not post in does not silently fall back, though — that would move
    their message somewhere they did not choose.
    """
    channel = channels.lookup(request.POST.get("kanal")) or channels.DEFAULT
    if not channels.allowed(channel, effective_roles(request)):
        raise PermissionDenied
    return channel


@require_POST
@access_required
def create_post(request: HttpRequest) -> HttpResponseRedirect:
    author = current_resident(request)
    channel = _posting_channel(request)
    content = (request.POST.get("content") or "").strip()
    if not content:
        messages.error(request, "Skriv en besked før du slår op.")
        return redirect(channel.url)
    if len(content) > MAX_CONTENT_CHARS:
        messages.error(request, f"Beskeden må højst fylde {MAX_CONTENT_CHARS} tegn.")
        return redirect(channel.url)

    try:
        minutes = int(request.POST.get("duration", channel.default_duration))
    except ValueError:
        minutes = channel.default_duration
    if minutes not in VALID_DURATIONS:
        minutes = channel.default_duration

    post = QuickPost.objects.create(
        author=author,
        channel=channel.slug,
        content=content,
        image=_validated_image(request) or "",
        expires_at=timezone.now() + timedelta(minutes=minutes),
    )
    services.notify_new_post(post)
    # No success message on purpose: the message appearing at the bottom of the feed *is* the
    # confirmation, and no chat app interrupts you to say a send worked. The warnings above (a
    # rejected image, over-long text) still surface, because those change what was actually posted.
    return redirect(channel.url)


def _comment_response(request: HttpRequest, post: QuickPost, back: str) -> HttpResponse:
    """What a reply POST answers with: the reply LIST for htmx, a redirect otherwise.

    The list, not the whole panel. The reply form carries data-morph-skip so the panel's own 5s
    poll cannot wipe half-typed text (see frontend/src/feed.ts) -- and a response that replaced the
    whole panel would therefore skip the form too, leaving the text sitting in the box after it had
    been sent. Targeting the list keeps the form outside the swap entirely, so hx-on::after-request
    can reset it.

    The redirect goes to the thread, not the channel: without JS the reply was written on the
    standalone thread page, and landing back at the bottom of the feed loses the conversation.
    `back` (the channel URL) is still the fallback for a post that vanished under us.

    Messages are rendered inside the fragment, so "Skriv en kommentar" and a rejected image warning
    land in the panel instead of being stranded in the session until the next full page load.
    """
    if request.headers.get("HX-Request"):
        return render(
            request,
            "den_hurtige/_replies.html",
            {"post": post, "max_content_chars": MAX_CONTENT_CHARS},
        )
    if post.pk is None:  # pragma: no cover - defensive; a deleted post has no thread to return to
        return redirect(back)
    return redirect("den_hurtige:thread", pk=post.pk)


@require_POST
@access_required
def create_comment(request: HttpRequest, pk: int) -> HttpResponse:
    author = current_resident(request)
    post = _post_or_404(request, pk)
    # Replies, deletions and reactions take the channel from the post, never from the request: the
    # post already knows where it lives, so there is no hidden field to disagree with.
    back = _channel_of(post)
    content = (request.POST.get("content") or "").strip()
    if len(content) > MAX_CONTENT_CHARS:
        messages.error(request, f"Kommentaren må højst fylde {MAX_CONTENT_CHARS} tegn.")
        return _comment_response(request, post, back)

    # The image is resolved BEFORE the emptiness check, and that ordering is the feature: a reply
    # may be a photo on its own, so "is there anything here?" cannot be answered from the text
    # alone. It used to reject blank content outright and only then look for a file, which made a
    # photo-only reply impossible however it was sent.
    #
    # _validated_image returns None both when nothing was attached and when what was attached was
    # rejected, having queued its own warning. Collapsing those two is right here: either way there
    # is no image to save, so a reply with no text and no usable image is empty and says so — and
    # the warning explaining WHY the photo did not count is already on its way to the same panel.
    image = _validated_image(request)
    if not content and image is None:
        messages.error(request, "Skriv et svar, eller vedhæft et billede.")
        return _comment_response(request, post, back)

    comment = QuickComment.objects.create(
        post=post,
        author=author,
        content=content,
        image=image or "",
        notify_everyone=request.POST.get("notify") == "alle",
    )
    services.notify_new_comment(comment)
    return _comment_response(request, post, back)


@require_POST
@access_required
def toggle_reaction(request: HttpRequest, pk: int) -> HttpResponse:
    """Set, change or clear this person's one emoji on a message, returning just its reaction row.

    Each resident has at most one reaction per message: a new emoji replaces theirs, and re-tapping
    the current one clears it. Deliberately silent — no notification is sent (see QuickReaction).

    Renders only the partial so a tap never re-renders the feed, which would collapse open threads
    and fight the 20-second poll.
    """
    post = _post_or_404(request, pk)
    resident = current_resident(request)
    form = ReactionForm(request.POST)
    if form.is_valid():
        # Set / move / clear lives in core.reactions so Den Hurtige and opslagstavlen cannot drift
        # into different semantics for the same widget.
        apply_toggle(QuickReaction.objects, author=resident, emoji=form.cleaned_data["emoji"], post=post)
    # An invalid emoji falls through to a plain re-render: the row is still correct, and a one-tap
    # control has nowhere useful to put a validation error.
    # NOT prefetched, and NOT reactions_for(): a prefetch is evaluated when the item is fetched,
    # which here is *before* apply_toggle writes. Reading .reactions.all() would then hit a cache
    # built a moment too early and re-render the row exactly as it was before the tap. Ask the
    # database again, joining the author in one query for the reader panel.
    return render(
        request,
        "den_hurtige/_reactions.html",
        {
            "post": post,
            "reactions": reaction_rows(post.reactions.select_related("author"), resident.pk),
            "quick_emoji": QUICK_EMOJI,
        },
    )


@require_POST
@access_required
def delete_post(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Authors clean up after themselves; administrators and Inspektionen moderate. Everyone else
    gets a 403."""
    post = _post_or_404(request, pk, active_only=False)
    if post.author_id != current_resident(request).pk and not can_moderate(request):
        raise PermissionDenied
    back = _channel_of(post)
    post.delete()
    messages.success(request, "Opslaget er slettet.")
    return redirect(back)


@require_POST
@access_required
def toggle_mute(request: HttpRequest, channel: str) -> HttpResponseRedirect:
    """Silence, or un-silence, push from one channel for this resident.

    A row exists only while the channel is muted, so the absence of a row is "notify me" — every
    channel is on until someone turns it off (see ChannelMute for why that direction). Idempotent in
    both directions: a double-tap on a slow connection cannot end up with two rows or an exception.
    """
    resolved = _channel_or_404(request, channel)
    resident = current_resident(request)
    removed, _per_model = ChannelMute.objects.filter(resident=resident, channel=resolved.slug).delete()
    if removed:
        messages.success(request, f"Du får igen notifikationer fra {resolved.name}.")
    else:
        # get_or_create, not create: two taps racing each other would otherwise hit the
        # uniq_channel_mute constraint and 500 on what is meant to be a toggle.
        ChannelMute.objects.get_or_create(resident=resident, channel=resolved.slug)
        messages.success(request, f"Notifikationer fra {resolved.name} er slået fra.")
    return redirect(resolved.url)


@require_POST
@login_required
def save_subscription(request: HttpRequest) -> HttpResponse:
    """Store (or drop) this browser's opt-in to Den Hurtige notifications.

    @login_required rather than @access_required, with the access gate re-applied *inside*: the
    endpoint is shared with opslagstavlen (which every resident may use), so gating the whole view
    on ACCESS_ROLES would lock plain residents out of subscribing to the noticeboard.

    That inner check is a no-op today — ACCESS_ROLES is None now that the trial is over — but it is
    what makes re-gating the feature (access.py's documented one-line edit) actually re-gate it.
    Without it, narrowing ACCESS_ROLES would still leave anyone able to register for its
    notifications.

    The per-topic upsert/teardown itself is core.push.handle_subscription_request.
    """
    if not request_allowed(request):
        return HttpResponse(status=403)
    return handle_subscription_request(request, services.TOPIC)
