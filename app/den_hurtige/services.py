"""Who gets notified about what on Den Hurtige — the policy half of push.

The transport (pywebpush, the background thread, dead-endpoint cleanup, the VAPID keys) lives in
core.push, shared with opslagstavlen. What stays here is the part that is genuinely this feature's:
its audience, its URL, and the wording on the lock screen.

Two audience rules layer on top of the shared topic opt-in, and both are this feature's alone:

  * **Channel mutes.** A ChannelMute row's *presence* means "do not notify me here", so every
    channel notifies everyone until someone opts out — the opposite of a subscribe model, and
    deliberately so: a new channel that notifies nobody until people find it is a new channel nobody
    posts in.
  * **A direct reply ignores the mute.** Someone answering your message is not broadcasting at you,
    and a mute is about a channel's chatter rather than about replies to your own post. It still
    respects the *topic* opt-in, though: a resident who turned Den Hurtige notifications off
    entirely hears nothing.
"""

from typing import TYPE_CHECKING

from django.db.models import QuerySet

from core import push
from core.models import PushSubscription

from . import channels

if TYPE_CHECKING:
    from residents.models import Resident

    from .models import QuickComment, QuickPost

# The topic name this feature subscribes and notifies under (core.push.TOPIC_FIELDS).
TOPIC = "den_hurtige"

FEED_URL = "/intern/den-hurtige/"


def is_configured() -> bool:
    """Re-exported so the feed view and its tests keep one import. See core.push."""
    return push.is_configured()


def vapid_public_key() -> str:
    return push.vapid_public_key()


def is_subscribed(resident: "Resident") -> bool:
    """Whether any of this resident's devices is opted in to Den Hurtige — the initial state of the
    subscribe toggle. Per *resident*, not per device: the page is rendered before JS has read this
    browser's endpoint, and "you have this on somewhere" is the honest thing to show at that point.
    The button corrects itself once push.ts compares the actual endpoint.
    """
    return push.subscribers(TOPIC).filter(user=resident).exists()


def _channel_url(slug: str) -> str:
    """Deep link for a channel's push notification, so tapping it opens the feed the message is
    actually in rather than the default one. A post filed under a channel that has since been
    retired from the registry links to the default feed rather than a 404."""
    channel = channels.lookup(slug) or channels.DEFAULT
    return channel.url


def _audience(channel: str, exclude_user_id: int | None = None) -> QuerySet[PushSubscription]:
    """Devices that should hear about something in `channel`.

    Two filters stacked, in this order for a reason: the topic opt-in is consent to be notified by
    this feature at all (core.push), and the mute is a preference about one feed within it. Losing
    the first would notify people who turned the feature off; losing the second would notify people
    who asked this channel to be quiet.
    """
    return push.subscribers(TOPIC, exclude_user_id=exclude_user_id).exclude(
        user__channel_mutes__channel=channel
    )


def notify_new_post(post: "QuickPost") -> None:
    """Announce a new post to every subscriber except its author, minus anyone who muted the channel
    it was posted in.

    The title is the sender's name, not the feature's: every platform already labels the
    notification with the app it came from, so "Ny besked på Den Hurtige" said it twice and pushed
    the part that matters — who, and what they wrote — down into the body. Titling with the person
    is what every chat app does, and what makes a lock screen readable at a glance.
    """
    push.send(
        _audience(post.channel, exclude_user_id=post.author_id),
        head=post.author.full_name,
        body=push.preview(post.content),
        url=_channel_url(post.channel),
    )


def notify_new_comment(comment: "QuickComment") -> None:
    """Announce a comment. `notify_everyone` decides the audience; the commenter never gets their
    own comment back, and a reply to your own post notifies nobody.

    A direct reply reaches the original poster even if they muted the channel — see the module
    docstring — which is why that branch goes through `push.subscribers` rather than `_audience`.
    """
    channel = comment.post.channel
    head = f"{comment.author.full_name} svarede"
    body = push.preview(comment.content) or "📷 Billede"
    # Straight to the thread, not just the channel. Tapping "Anders svarede" used to land at the
    # bottom of the feed, leaving you to find the message the notification was about -- which, in a
    # channel where everything expires, may already have scrolled past. ?traad= opens the panel on
    # the channel page, so the conversation and its surroundings both arrive. The service worker
    # navigates an existing window rather than only focusing it (app/templates/sw.js), so this
    # works whether or not the app is already open.
    url = f"{_channel_url(channel)}?traad={comment.post_id}"

    if comment.notify_everyone:
        push.send(_audience(channel, exclude_user_id=comment.author_id), head=head, body=body, url=url)
        return
    if comment.author_id == comment.post.author_id:
        return  # commenting on your own post: the only recipient would be yourself
    recipients = push.subscribers(TOPIC).filter(user_id=comment.post.author_id)
    push.send(recipients, head=head, body=body, url=url)
