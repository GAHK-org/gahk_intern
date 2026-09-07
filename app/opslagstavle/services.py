"""Who gets notified about what on opslagstavlen — the policy half of push.

The transport (pywebpush, the background thread, dead-endpoint cleanup, the VAPID keys) is
core.push, shared with Den Hurtige. What is here is this feature's audience and wording, and they
differ from a chat's in one important way:

  * a **new post** goes to everyone who opted in — that is the point of leaving Facebook;
  * a **comment** goes to the post's author only. A board thread with twenty replies must not be
    twenty dorm-wide pushes, and there is deliberately no "underret alle" checkbox: that exists on
    Den Hurtige because a chat has a single shared audience, and a noticeboard does not;
  * a **reaction** notifies nobody, for the reason NoticeReaction's sibling docstring gives — a feed
    where every 👍 buzzes a hundred phones is the noise this replaces, and it stops people reacting
    at all.

The notification links to the individual post, not the board: `?page=4` is not a stable address for
a thing, and a tap should land on what it is about.
"""

from typing import TYPE_CHECKING

from core import push
from core.markdown import plain_text

from . import access

if TYPE_CHECKING:
    from residents.models import Resident

    from .models import Notice, NoticeComment

# The topic name this feature subscribes and notifies under (core.models.TOPIC_FIELDS).
TOPIC = "opslagstavle"

BOARD_URL = "/intern/opslagstavle/"


def notice_url(notice: "Notice") -> str:
    return f"{BOARD_URL}{notice.pk}"


def is_subscribed(resident: "Resident") -> bool:
    """Whether any of this resident's devices wants board notifications — the initial state of the
    subscribe toggle. Per *resident*, because the page is rendered before JS has read this browser's
    endpoint; push.ts corrects the button once it can compare the actual endpoint."""
    return push.subscribers(TOPIC).filter(user=resident).exists()


def notify_new_notice(notice: "Notice") -> None:
    """Announce a new post to every board subscriber except its author.

    The body is `plain_text`, not the raw Markdown: a lock screen showing `**Vigtigt**` and
    `[link](https://…)` is the difference between a notification people read and one they dismiss.
    """
    # Narrowed to people who can open the board: while the rollout gate is on, notifying a resident
    # who would get a 403 on tapping is worse than not notifying them.
    push.send(
        access.allowed_subscribers(push.subscribers(TOPIC, exclude_user_id=notice.author_id)),
        # The author is the head, matching Den Hurtige and the card itself. It used to be the title,
        # with the author crammed into the front of the body; with no title left, repeating the name
        # in both lines would waste the one line a lock screen actually gives you.
        head=notice.author.full_name,
        body=push.preview(plain_text(notice.body)),
        url=notice_url(notice),
    )


def notify_new_comment(comment: "NoticeComment") -> None:
    """Tell the post's author someone replied — and nobody else."""
    if comment.author_id == comment.notice.author_id:
        return  # commenting on your own post: the only recipient would be yourself
    recipients = access.allowed_subscribers(push.subscribers(TOPIC).filter(user_id=comment.notice.author_id))
    push.send(
        recipients,
        head=f"{comment.author.full_name} svarede",
        body=push.preview(comment.body),
        url=notice_url(comment.notice),
    )
