"""Den Hurtige — the intern feed of short-lived urgent messages that replaces the Messenger group.

Anyone who can reach it may post, comment and subscribe to push — but access itself is gated by
den_hurtige.access, and individual channels may narrow that further (den_hurtige.channels).
Notification fan-out lives in services.py and runs off the request thread.

MESSAGES LEAVE THE FEED ON A TIMER AND ARE THEN ARCHIVED, NEVER DELETED (see models.py for the
reversal and why). This module is where that is enforced, because "archived" is a read of the clock
and not a column anyone could constrain:

  * READS resolve against every post the caller may see. `archive` lists them; `thread` renders an
    archived conversation read-only, which is what keeps a months-old reply notification's deep link
    working instead of landing on "this is gone".
  * WRITES resolve against `active()` alone — `_post_or_404` defaults to it, so replying to or
    reacting to an archived post cannot succeed by forgetting a check. It has to be opted out of,
    once, in the two places that need to tell the caller WHY rather than answer 404.
  * DELETION is refused outright on an archived post, for author and moderator alike. The admin is
    the one exception and the reason it is one is in admin.py.

Every view here is scoped to one channel.
"""

from datetime import date, datetime, timedelta
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
from django.utils.dateparse import parse_datetime
from django.utils.formats import date_format
from django.views.decorators.http import require_POST

from core.clock import current_date, current_datetime
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
    """One channel's live posts.

    IT NO LONGER PURGES ANYTHING, and the absence is the feature. This function used to open with
    `QuickPost.objects.purge_expired()` — a cross-channel hard delete on every feed load and every
    5s poll, so that a channel nobody had opened could not hoard expired posts. Archiving deleted
    the problem rather than the code: a post leaves this queryset the instant the clock passes
    `expires_at`, with nothing written and nothing swept, and it is in the archive by the same
    comparison. There is correspondingly no cron job any more (DEPLOY.md §4b).
    """
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


def mark_runs(posts: list[QuickPost], user_id: int) -> list[QuickPost]:
    """Attach `reaction_rows`, `grouped` and `group_end` to a list of messages IN READING ORDER.

    Attached here rather than resolved in the template because a Django template cannot call a
    function with arguments, and the "did *I* react?" flag depends on the current user.

    Shared by the feed and the archive so the two draw identical bubbles. That sharing has one
    precondition, and it is the reason this takes a list rather than a queryset: `posts` must be in
    the order it will be RENDERED, oldest first. The archive fetches descending and turns the chunk
    round before calling this (see `_archive_chunk`) — called on the descending list it would group
    every run backwards. It is called once per DAY rather than once per chunk, because a date
    heading interrupts a run visually and a burst straddling midnight must not be drawn as one
    group with the heading wedged into the middle of it.
    """
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


def posts_for(request: HttpRequest, channel: Channel) -> list[QuickPost]:
    """One channel's active messages, ready to render."""
    return mark_runs(list(_active_posts(channel)), current_resident(request).pk)


def _channel_or_404(request: HttpRequest, slug: str | None) -> Channel:
    """Resolve a channel for a full page view, or 404.

    An unknown slug and a channel this resident may not open answer the same way on purpose: a 403
    would confirm that "inspektion-internt" exists to someone who cannot read it.
    """
    channel = channels.lookup(slug)
    if channel is None or not channels.allowed(channel, effective_roles(request)):
        raise Http404("Ingen kanal med det navn.")
    return channel


def _post_or_404(request: HttpRequest, pk: int, *, archived_ok: bool = False) -> QuickPost:
    """One post this resident is actually allowed to see, or 404.

    The channel check is the point. Every per-post endpoint here resolves a post by primary key
    alone, which says nothing about whether the caller may read the CHANNEL it lives in -- and
    channels can be role-restricted (channels.Channel.roles). Without this, guessing a pk reaches a
    post in a channel the sidebar will not even advertise.

    It mattered less while the per-post endpoints were all writes: you could react to or comment on
    a post you could not find. den_hurtige:thread makes it a READ, which is the version that leaks.
    Routed through one helper so they cannot drift apart on the answer.

    404 rather than 403 for the same reason as _channel_or_404: a 403 confirms the post exists.

    ARCHIVED POSTS ARE EXCLUDED BY DEFAULT, and that default is the read-only rule. Every write
    endpoint calls this bare, so "you cannot reply to or react to an archived message" holds because
    the post is not found rather than because each endpoint remembered to check — the failure mode
    of a forgotten check is a 404, not a write to the archive. `archived_ok=True` is for the two
    callers that must tell the reader WHY (delete_post) or must read it at all (the thread panel);
    both of them ask `is_archived` immediately afterwards.
    """
    manager = QuickPost.objects.all() if archived_ok else QuickPost.objects.active()
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
    # Tombstones excluded, though the feed still draws them: a deleted message is not something to
    # go and read.
    rows = (
        QuickPost.objects.active().filter(deleted_at__isnull=True).values("channel").annotate(n=Count("id"))
    )
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
            # Whether anything has ever archived in this channel. It decides whether the feed draws
            # the "rul op" divider and the sentinel above it at all: on a channel whose first
            # message is still live, an invitation to scroll up into an empty archive is a worse
            # first impression than no invitation. One EXISTS on (channel, expires_at), which is
            # indexed.
            "has_archive": QuickPost.objects.filter(channel=resolved.slug).archived().exists(),
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


# --- the archive --------------------------------------------------------------------------------
#
# Messages that have left the feed, which is now every message the channel has ever held.
#
# IT IS THE FEED, SCROLLED UP. Not a panel, not a separate page with its own layout: the archived
# messages render with the SAME partial, in the same column, in the same order, directly above the
# live ones, and the only thing that marks the boundary is a divider and a slightly cooler bubble
# (.msg-archived in styles.css). It was a side panel first, and that was wrong for the obvious
# reason once you see the two side by side — an archive of a chat that does not look like the chat
# makes you re-learn how to read your own history.
#
# So the chunks below are built to be PREPENDED. They come back oldest-first, the older chunk goes
# above the newer one, and the browser's scroll position has to be corrected for the height that
# arrives above the viewport — which frontend/src/feed.ts does, because Safari has no scroll
# anchoring to do it for us.

# Messages per chunk. Sized for the scroll rather than for the query: one chunk is fetched before
# the reader reaches the top of what they have, so the number only has to be "more than a screen".
ARCHIVE_PAGE = 30


class ArchiveDay(NamedTuple):
    """One day's archived messages, oldest first, under one date heading.

    Built here rather than with {% ifchanged %} in the template, for a reason that only shows up on
    the second chunk: `ifchanged` resets with each render, so the first message of every fetched
    chunk would emit a date heading whether or not the chunk below it had just emitted the same one.
    Grouping server-side also lets the day carry its own label, which needs "i dag"/"i går" and the
    Danish month names, and lets each day's runs be marked against its own neighbours.
    """

    date: date
    label: str
    posts: list[QuickPost]


def _archive_label(day: date, today: date) -> str:
    """A date heading a resident reads without decoding: "i dag", "i går", "12. marts", "12. marts
    2024". The year appears only when it is not this one, which is the only time it carries any
    information and is otherwise most of the width of the heading."""
    if day == today:
        return "i dag"
    if day == today - timedelta(days=1):
        return "i går"
    return date_format(day, "j. F" if day.year == today.year else "j. F Y")


def _archive_before(request: HttpRequest) -> datetime | None:
    """The paging cursor: fetch messages written strictly before this instant, or None to start at
    the newest archived message. Junk is None, never an error — a mangled cursor re-starts the
    archive at its newest end, which is a reader's worst case rather than a 400 in the middle of a
    scroll.

    A CURSOR, NOT A PAGE NUMBER, and the archive is exactly the shape that makes the difference
    visible: messages enter it continuously at the newest end, so `?side=2` slides by however many
    archived between one fetch and the next and shows a message twice. An instant does not move.
    """
    raw = request.GET.get("inden")
    if not raw:
        return None
    return parse_datetime(raw)


def _archive_chunk(
    channel: Channel, before: datetime | None, user_id: int
) -> tuple[list[ArchiveDay], datetime | None]:
    """One chunk of the channel's archive, oldest first, plus the cursor for the chunk before it.

    OLDEST FIRST, because this is prepended above the live messages and has to read downwards into
    them like the rest of the conversation. The QUERY still walks backwards — it has to, since the
    interesting end of an archive is the recent one — so it descends, takes a chunk, and turns it
    round. The cursor points at the oldest message kept, which is where the next fetch resumes.

    A DAY IS NEVER SPLIT ACROSS TWO CHUNKS. The chunk is trimmed back to the last whole day, so a
    date heading appears once with everything that belongs under it. The exception is a day holding
    more messages than a whole chunk, where trimming would leave nothing and the scroll would stall
    forever: that one does split and gets its heading twice, which is the better of the two failures
    and rare enough to be worth the asymmetry.

    Replies are COUNTED, not fetched — the same bargain the live feed struck. A chunk briefly
    prefetched `comments__author` so it could render every thread inline; the threads went back to
    the side panel (see _message.html), so all a chunk needs is the number on the "N svar" link, and
    the panel fetches the conversation itself when somebody actually opens one. On a channel with a
    long history that is the difference between one query per chunk and one per chunk plus every
    reply anybody ever wrote in it.
    """
    rows = QuickPost.objects.filter(channel=channel.slug).archived()
    if before is not None:
        rows = rows.filter(created_at__lt=before)
    # One more than a chunk, purely to answer "is there an older chunk?" without a second COUNT.
    page = list(
        rows.select_related("author")
        .prefetch_related(REACTIONS)
        .annotate(reply_count=Count("comments"))
        .order_by("-created_at", "-pk")[: ARCHIVE_PAGE + 1]
    )
    more = len(page) > ARCHIVE_PAGE
    page = page[:ARCHIVE_PAGE]
    if more:
        oldest = timezone.localdate(page[-1].created_at)
        whole_days = [post for post in page if timezone.localdate(post.created_at) != oldest]
        if whole_days:
            page = whole_days
    if not page:
        return [], None

    # Taken BEFORE the reverse, while page[-1] is still the oldest message. Exclusive, so the
    # message it names is not fetched twice; two posts sharing a timestamp to the microsecond would
    # lose one, which auto_now_add makes unreachable in practice.
    older_than = page[-1].created_at if more else None
    page.reverse()

    today = current_date()
    days: list[ArchiveDay] = []
    for post in page:
        day = timezone.localdate(post.created_at)
        if not days or days[-1].date != day:
            days.append(ArchiveDay(day, _archive_label(day, today), []))
        days[-1].posts.append(post)
    # Per day, not across the chunk: a date heading interrupts a run visually, so a burst that
    # straddles midnight must not be drawn as one group with the heading wedged into the middle
    # of it. Same call the live feed makes, on each day's own list.
    for entry in days:
        mark_runs(entry.posts, user_id)
    return days, older_than


def _archive_context(request: HttpRequest, channel: Channel) -> dict[str, object]:
    """Everything both exits of `archive` render from."""
    before = _archive_before(request)
    days, older_than = _archive_chunk(channel, before, current_resident(request).pk)
    return {
        "channel": channel,
        "days": days,
        "older_than": older_than.isoformat() if older_than else "",
        # Whether this is the newest end of the archive. The standalone page uses it to decide
        # whether "nyeste" is a link or the place you already are.
        "at_newest": before is None,
        "quick_emoji": QUICK_EMOJI,
        # Not a flag the templates could work out for themselves: _message.html and _reactions.html
        # are the SAME partials the live feed uses, and what makes a message read-only is which
        # list it was fetched into, not anything on the row.
        "archived": True,
    }


def archive(request: HttpRequest) -> HttpResponseBase:
    """One chunk of the archive: prepended into the feed by htmx, or a standalone page.

    TWO EXITS, and they differ in how they FAIL as much as in what they render:

      * htmx gets the bare chunk — the sentinel at the top of the feed replaces itself with it as
        the reader scrolls up — and, when the gate says no, a 204. Same reason as feed_items:
        @access_required redirects an expired session to the login page, htmx follows the redirect,
        and the login form lands in the middle of the conversation. A 204 makes htmx do nothing,
        and hands an unauthorised caller no data either way.
      * anything else is a real page, gated normally, paged with plain links. That is the no-JS
        path — there is no scrolling-to-load without JavaScript — and a URL somebody can bookmark.

    The channel arrives as `?kanal=`, as it does for the poll: the archive is fetched for whatever
    channel is on screen, and a second URL shape for the same choice is one more thing to keep in
    step. An unknown or forbidden channel takes the 204 exit on the htmx path and 404s on the page.
    """
    if request.headers.get("HX-Request"):
        if not request_allowed(request):
            return HttpResponse(status=204)
        channel = channels.lookup(request.GET.get("kanal"))
        if channel is None or not channels.allowed(channel, effective_roles(request)):
            return HttpResponse(status=204)
        return render(request, "den_hurtige/_archive_chunk.html", _archive_context(request, channel))
    return _archive_page(request)


@access_required
def _archive_page(request: HttpRequest) -> HttpResponse:
    """The page half of `archive`, split out only so the decorator applies to it alone."""
    channel = _channel_or_404(request, request.GET.get("kanal"))
    return render(request, "den_hurtige/archive.html", _archive_context(request, channel))


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

    ARCHIVED THREADS RENDER, READ-ONLY. This used to resolve against `active()`, so a thread whose
    message had expired answered "gone" — which was true then and is a lie now. It is also the case
    that matters most for the deep link: services.notify_new_comment sends a `?traad=<pk>` URL, and
    a push notification is routinely opened the next morning, by which time the message may well
    have archived. The panel now shows the conversation with no reply form and no reaction controls
    (`archived` below), so the link keeps working for as long as the archive does.

    ONLY A MISSING OR FORBIDDEN POST still splits the two halves. The fragment renders a short
    notice with NO hx-trigger on it, so the panel's poll stops rather than asking for a message that
    is not there every five seconds; the page raises 404, because a deep link to a message that was
    genuinely deleted leads nowhere.

    An archived panel does not poll either, and that is the same reasoning one step on: nothing
    about it can change — no reply can arrive, no reaction can move — so a timer on it is a request
    every five seconds for a byte-identical answer, forever, on a page somebody has left open.

    Replies are prefetched HERE rather than in _active_posts: one post's worth instead of every
    post's, on a request that only happens when somebody opens a thread.
    """
    post = (
        QuickPost.objects.filter(pk=pk)
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
        "archived": post.is_archived,
        # Separate from `archived` because the panel names which state it is; both can be true.
        "deleted": post.is_deleted,
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
        expires_at=current_datetime() + timedelta(minutes=minutes),
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
    post = _post_or_404(request, pk, archived_ok=True)
    # Replies, deletions and reactions take the channel from the post, never from the request: the
    # post already knows where it lives, so there is no hidden field to disagree with.
    back = _channel_of(post)
    # Deleted or archived while the reply was being typed — the panel renders no form in either
    # case, so this is the race and not a crafted request. Answered with the reply LIST, as every
    # other outcome here is, so the explanation lands in the panel the person is looking at rather
    # than in a session message nothing will surface until the next full page load.
    #
    # Deletion is checked first because a tombstone eventually archives too, and then it is the
    # answer that explains why the reply box went away.
    if post.is_deleted:
        messages.error(request, "Beskeden er slettet, og der kan ikke længere svares på den.")
        return _comment_response(request, post, back)
    if post.is_archived:
        messages.error(request, "Beskeden er arkiveret, og der kan ikke længere svares på den.")
        return _comment_response(request, post, back)
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
    post = _post_or_404(request, pk, archived_ok=True)
    resident = current_resident(request)
    # Archived: re-render the row exactly as it stands and write nothing. The archive draws no
    # picker and no pressable pills, so reaching here at all means the page went stale under
    # somebody's thumb -- the message archived between the poll that drew it and the tap. Swapping
    # the read-only row in answers that truthfully: the counts they were looking at, now inert. A
    # 404 would leave the live-looking row sitting there and the tap appearing to have been lost.
    # A tombstone answers the same way: soft_delete cleared the reactions, so this returns the empty
    # read-only row the next poll would draw anyway.
    if post.is_deleted or post.is_archived:
        return _reaction_row(request, post, resident.pk, archived=True)
    form = ReactionForm(request.POST)
    if form.is_valid():
        # Set / move / clear lives in core.reactions so Den Hurtige and opslagstavlen cannot drift
        # into different semantics for the same widget.
        apply_toggle(QuickReaction.objects, author=resident, emoji=form.cleaned_data["emoji"], post=post)
    # An invalid emoji falls through to a plain re-render: the row is still correct, and a one-tap
    # control has nowhere useful to put a validation error.
    return _reaction_row(request, post, resident.pk)


def _reaction_row(
    request: HttpRequest, post: QuickPost, user_id: int, *, archived: bool = False
) -> HttpResponse:
    """Just one message's reaction row, re-read from the database.

    NOT prefetched, and NOT reactions_for(): a prefetch is evaluated when the item is fetched, which
    in toggle_reaction is *before* apply_toggle writes. Reading .reactions.all() would then hit a
    cache built a moment too early and re-render the row exactly as it was before the tap. Ask the
    database again, joining the author in one query for the reader panel.
    """
    return render(
        request,
        "den_hurtige/_reactions.html",
        {
            "post": post,
            "reactions": reaction_rows(post.reactions.select_related("author"), user_id),
            "quick_emoji": QUICK_EMOJI,
            "archived": archived,
        },
    )


@require_POST
@access_required
def delete_post(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Authors clean up after themselves; administrators and Inspektionen moderate. Everyone else
    gets a 403 — and nobody at all may delete a message once it has archived.

    Which of the two deletions you get is decided here from the row's own age, identically for an
    author and a moderator; neither the form nor the swipe gesture chooses. See models.DELETE_GRACE.

    THE ARCHIVED CHECK COMES BEFORE THE PERMISSION CHECK, deliberately. The two answers are "you may
    not do this" and "this cannot be done", and the second is the true one here: a moderator is not
    being denied a privilege, the button no longer exists for anybody. Ordering it the other way
    would 403 a moderator and hand an author the friendly message, for identical requests.

    It is fetched with `archived_ok=True` and refused in the body rather than 404ing, because this
    is the one gesture people will race the clock on: the delete button (or the swipe) is on screen,
    the message archives underneath it, and the tap arrives a second late. A 404 on a control they
    are looking at reads as a bug; the message says what actually happened.
    """
    post = _post_or_404(request, pk, archived_ok=True)
    if post.is_archived:
        messages.error(request, "Beskeden er arkiveret, og arkiverede beskeder kan ikke slettes.")
        return redirect(_channel_of(post))
    # Before the permission check for the same reason as the archived case above.
    if post.is_deleted:
        messages.error(request, "Beskeden er allerede slettet.")
        return redirect(_channel_of(post))
    if post.author_id != current_resident(request).pk and not can_moderate(request):
        raise PermissionDenied
    back = _channel_of(post)
    if post.within_delete_grace:
        post.delete()
        messages.success(request, "Opslaget er slettet.")
    elif post.soft_delete():
        # Spelled out because it is the half people do not expect: the words are gone, the bubble
        # is not.
        messages.success(request, "Beskeden er slettet. «Besked slettet» bliver stående i samtalen.")
    else:
        # Lost the claim to a concurrent delete — same answer as the is_deleted check above, which
        # a request arriving a moment earlier would have taken instead.
        messages.error(request, "Beskeden er allerede slettet.")
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
