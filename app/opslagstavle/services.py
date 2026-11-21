"""Who gets notified about what on opslagstavlen — the policy half of its notifications.

The machinery (rows, preferences, push delivery, grouping) is the notifications app. What is here is
this feature's audience and wording, and they differ from a chat's in one important way:

  * a **new post** goes to everyone who can read the board (`posts.new`) — that is the point of
    leaving Facebook;
  * a **comment** goes to the post's author only (`posts.replies`), grouped per post: "Anders og
    Bente svarede". A board thread with twenty replies must not be twenty dorm-wide pushes, and
    there is deliberately no "underret alle" checkbox: that exists on Den Hurtige because a chat has
    a single shared audience, and a noticeboard does not;
  * a **reaction** goes to the post's author only (`posts.likes`), grouped the same way and at low
    importance — never to the board, where every 👍 buzzing a hundred phones is the noise this
    replaces.
  * a **reply to a comment** also tells that comment's author (`posts.replies`, grouped per
    thread), and a **reaction to a comment** tells its author only (`posts.likes`, grouped per
    comment). The post's author still hears about every comment on their post.

The notification links to the individual post, not the board: `?page=4` is not a stable address for
a thing, and a tap should land on what it is about.
"""

from typing import TYPE_CHECKING

from django.urls import reverse

from core import push
from core.markdown import plain_text
from core.mentions import notify_mentions, to_plain
from notifications import categories, services

from . import access

if TYPE_CHECKING:
    from collections.abc import Collection

    from residents.models import Resident

    from .models import Notice, NoticeComment


def notice_url(notice: "Notice") -> str:
    return reverse("opslagstavle:detail", args=[notice.pk])


def comment_url(comment: "NoticeComment") -> str:
    """The post, scrolled to the comment: in a long thread "Bente svarede" should land on the answer."""
    return f"{notice_url(comment.notice)}#kommentar-{comment.pk}"


def _can_open(resident: "Resident") -> bool:
    """Whether this resident can still open the board (rollout gate, or moved out). The kitchen
    account is not in everyone(), but can always open its own posts and the threads under them."""
    if resident.is_kitchen:
        return resident.is_active
    return access.allowed_residents(services.everyone()).filter(pk=resident.pk).exists()


def _author_can_open(notice: "Notice") -> bool:
    return _can_open(notice.author)


def notify_mentions_in_notice(notice: "Notice", old_body: str | None = None) -> set[int]:
    """Tell residents newly @-mentioned in a post. `old_body` on an edit, so a fix-up of a typo does
    not re-notify everyone the post already named."""
    return notify_mentions(
        notice.body,
        actor=notice.author,
        where="i et opslag",
        url=notice_url(notice),
        audience=access.allowed_residents(services.everyone()),
        old_text=old_body,
    )


def notify_new_notice(notice: "Notice", skip: "Collection[int]" = ()) -> None:
    """Announce a new post to everyone who can open the board, except its author.

    The body is `plain_text`, not the raw Markdown: a lock screen showing `**Vigtigt**` and
    `[link](https://…)` is the difference between a notification people read and one they dismiss.
    The author is the title, matching Den Hurtige and the card itself. `skip` is who was just told
    they are mentioned in it.
    """
    # Narrowed to people who can open the board: while the rollout gate is on, notifying a resident
    # who would get a 403 on tapping is worse than not notifying them.
    services.notify(
        access.allowed_residents(services.everyone()).exclude(pk__in=skip),
        category=categories.POSTS_NEW,
        title=notice.author.full_name,
        body=push.preview(plain_text(notice.body)),
        url=notice_url(notice),
        exclude=notice.author_id,
    )


def notify_new_comment(comment: "NoticeComment") -> None:
    """Tell the post's author someone replied, and anyone the comment @-mentions."""
    notice = comment.notice
    mentioned = notify_mentions(
        comment.body,
        actor=comment.author,
        where="i en kommentar",
        url=notice_url(notice),
        audience=access.allowed_residents(services.everyone()),
    )
    # `or "📷 Billede"`: a photo-only comment has no text, and push.preview("") is "" — which on a
    # lock screen reads as a notification that failed to load.
    body = push.preview(to_plain(comment.body)) or "📷 Billede"
    told = set(mentioned)
    parent = comment.parent
    if parent is not None and parent.author_id not in told and _can_open(parent.author):
        # Grouped per THREAD rather than per post: "Anders og Bente svarede på din kommentar" is one
        # conversation, and a second thread of yours on the same post is a different one.
        services.notify_grouped(
            parent.author_id,
            category=categories.POSTS_REPLIES,
            group_key=f"posts.replies:comment:{parent.pk}",
            actor=comment.author,
            verb="svarede på din kommentar",
            body=body,
            url=comment_url(comment),
        )
        told.add(parent.author_id)
    if notice.author_id in told:
        return  # already told, more specifically
    if not _author_can_open(notice):
        return  # the author can no longer open the board (rollout gate, or moved out)
    services.notify_grouped(
        notice.author_id,
        category=categories.POSTS_REPLIES,
        group_key=f"posts.replies:{notice.pk}",
        actor=comment.author,
        verb="svarede",
        body=body,
        url=comment_url(comment),
    )


def notify_reaction(notice: "Notice", reactor: "Resident", emoji: str) -> None:
    """Somebody reacted to `notice`. Called on a NEW reaction only (core.reactions.apply_toggle)."""
    if not _author_can_open(notice):
        return
    services.notify_grouped(
        notice.author_id,
        category=categories.POSTS_LIKES,
        group_key=f"posts.likes:{notice.pk}",
        actor=reactor,
        verb="reagerede på dit opslag",
        body=f"{emoji} {push.preview(plain_text(notice.body))}".strip(),
        url=notice_url(notice),
    )


def notify_comment_reaction(comment: "NoticeComment", reactor: "Resident", emoji: str) -> None:
    """Somebody reacted to `comment`: its author only, grouped per comment. NEW reactions only."""
    if not _can_open(comment.author):
        return
    services.notify_grouped(
        comment.author_id,
        category=categories.POSTS_LIKES,
        group_key=f"posts.likes:comment:{comment.pk}",
        actor=reactor,
        verb="reagerede på din kommentar",
        body=f"{emoji} {push.preview(to_plain(comment.body)) or '📷 Billede'}",
        url=comment_url(comment),
    )
