"""Who gets notified about what on Den Hurtige — the policy half of its notifications.

The machinery (rows, preferences, push delivery, grouping) lives in the notifications app. What stays
here is the part that is genuinely this feature's: its audience, its URLs, and the wording.

Three kinds of notification, each its own category on the settings page:

  * **A new message in a room** (`chat.room.<slug>`, push only). One category per room, so muting a
    noisy room is a toggle on the settings page — it used to be a ChannelMute row, and those were
    carried over as preferences (notifications migration 0002). Every room notifies everyone until
    someone turns it off: a new room that notifies nobody until people find it is a room nobody
    posts in.
  * **A reply to your message** (`chat.replies`), grouped per message: "Anders og Bente svarede".
    A reply is addressed to you, not to the room, so a muted room does not silence it.
  * **A reaction to your message** (`chat.likes`), grouped the same way.

Every chat notification expires with the message it is about: a push for a message that has already
vanished from the feed is a tap into nothing.
"""

from typing import TYPE_CHECKING

from django.db.models import QuerySet

from core import push
from core.mentions import notify_mentions, to_plain
from core.rollout import Gate
from notifications import categories, services

from . import access, channels

if TYPE_CHECKING:
    from residents.models import Resident

    from .models import QuickComment, QuickPost

FEED_URL = "/intern/den-hurtige/"


def _channel(slug: str) -> channels.Channel:
    """A post filed under a channel that has since been retired from the registry links to the
    default feed rather than a 404."""
    return channels.lookup(slug) or channels.DEFAULT


def _thread_url(post: "QuickPost") -> str:
    """Straight to the thread, not just the channel: ?traad= opens the panel on the channel page, so
    the conversation and its surroundings both arrive — in a channel where everything expires, the
    message may already have scrolled past. The service worker navigates an existing window rather
    than only focusing it (app/templates/sw.js), so this works whether or not the app is open."""
    return f"{_channel(post.channel).url}?traad={post.pk}"


def _audience(slug: str) -> "QuerySet[Resident]":
    """Everyone who can open this room: the feature's rollout gate, then the room's own roles. The
    resident's own toggle for the room is the notifications app's business, not this one's."""
    channel = _channel(slug)
    audience = Gate(lambda: channel.roles).allowed_residents(access.allowed_residents(services.everyone()))
    if channel.slug == channels.KITCHEN_CHANNEL:
        audience = audience | services.kitchen()
    return audience


def notify_new_post(post: "QuickPost") -> None:
    """Announce a new post to everyone who can read the room, except its author.

    The title is the sender's name, not the feature's: every platform already labels the
    notification with the app it came from, so titling with the person is what makes a lock screen
    readable at a glance. Anyone the post @-mentions is told that instead.
    """
    mentioned = notify_mentions(
        post.content,
        actor=post.author,
        where="i Den Hurtige",
        url=_thread_url(post),
        audience=_audience(post.channel),
        expires_at=post.expires_at,
    )
    services.notify(
        _audience(post.channel).exclude(pk__in=mentioned),
        category=categories.chat_room_key(_channel(post.channel).slug),
        title=post.author.full_name,
        body=push.preview(to_plain(post.content)) or "📷 Billede",
        url=_channel(post.channel).url,
        expires_at=post.expires_at,
        exclude=post.author_id,
    )


def notify_new_comment(comment: "QuickComment") -> None:
    """A reply tells the post's author (grouped, under "Svar på dine beskeder"). `notify_everyone`
    additionally announces it to the room, as if it were a new post — minus the commenter and the
    post's author, who has just been told. Anyone the reply @-mentions is told that instead."""
    post = comment.post
    body = push.preview(to_plain(comment.content)) or "📷 Billede"
    mentioned = notify_mentions(
        comment.content,
        actor=comment.author,
        where="i Den Hurtige",
        url=_thread_url(post),
        audience=_audience(post.channel),
        expires_at=post.expires_at,
    )
    if comment.author_id != post.author_id and post.author_id not in mentioned:
        services.notify_grouped(
            post.author_id,
            category=categories.CHAT_REPLIES,
            group_key=f"chat.replies:{post.pk}",
            actor=comment.author,
            verb="svarede",
            body=body,
            url=_thread_url(post),
            expires_at=post.expires_at,
        )
    if comment.notify_everyone:
        services.notify(
            _audience(post.channel).exclude(pk=post.author_id).exclude(pk__in=mentioned),
            category=categories.chat_room_key(_channel(post.channel).slug),
            title=f"{comment.author.full_name} svarede",
            body=body,
            url=_thread_url(post),
            expires_at=post.expires_at,
            exclude=comment.author_id,
        )


def notify_reaction(post: "QuickPost", reactor: "Resident", emoji: str) -> None:
    """Somebody reacted to `post`. Grouped per post, so ten reactions are one notification that
    says "10 personer reagerede" rather than ten buzzes. Called on a NEW reaction only (see
    core.reactions.apply_toggle)."""
    services.notify_grouped(
        post.author_id,
        category=categories.CHAT_LIKES,
        group_key=f"chat.likes:{post.pk}",
        actor=reactor,
        verb="reagerede på din besked",
        body=f"{emoji} {push.preview(to_plain(post.content))}".strip(),
        url=_thread_url(post),
        expires_at=post.expires_at,
    )
