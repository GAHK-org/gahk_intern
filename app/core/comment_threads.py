"""Comment threads one level deep, shared by opslagstavlen and begivenheder.

A comment is either top-level or a reply to a top-level comment (`parent`). Answering a reply joins
the same thread rather than nesting deeper: on a phone a tree of indents narrows into a column, and
"where did my answer go" becomes a real question. The reply box starts with an @-mention of the
person answered instead, which is also what notifies them.

Both apps keep their own comment and reaction tables (see core.reactions for why nothing here is a
generic foreign key); what is shared is the shape, so the two threads cannot drift apart.
"""

from collections.abc import Iterable
from typing import Any

from django.contrib import messages
from django.db.models import Model, Prefetch, QuerySet
from django.http import HttpRequest

from .reactions import reaction_rows


def comments_with_reactions(comments: "QuerySet[Any]", reaction_model: type[Model]) -> "QuerySet[Any]":
    """The thread's comments with authors and reactions loaded: two queries however long it is.

    The reactions' authors are joined inside the Prefetch (not a nested prefetch) because the reader
    panel names people; see opslagstavle.views.REACTIONS for why that keeps the count flat.
    """
    return comments.select_related("author").prefetch_related(
        Prefetch("reactions", queryset=reaction_model._default_manager.select_related("author"))
    )


def threads(comments: Iterable[Any], user_id: int) -> list[Any]:
    """The top-level comments in order, each with `thread_replies`, and every comment with its
    `reaction_rows` for this reader.

    A reply whose parent is itself a reply (only the admin can make one) is shown in the thread of
    its top-level ancestor; one whose ancestor is missing from `comments` is dropped rather than
    shown out of context.
    """
    rows = list(comments)
    by_pk = {c.pk: c for c in rows}
    top: list[Any] = []
    for comment in rows:
        comment.reaction_rows = reaction_rows(comment.reactions.all(), user_id)
        comment.thread_replies = []
    for comment in rows:
        if comment.parent_id is None:
            top.append(comment)
            continue
        root = by_pk.get(comment.parent_id)
        seen = {comment.pk}
        while root is not None and root.parent_id is not None and root.pk not in seen:
            seen.add(root.pk)
            root = by_pk.get(root.parent_id)
        if root is not None and root.parent_id is None:
            root.thread_replies.append(comment)
    return top


def thread_root(parent: Any) -> Any:  # noqa: ANN401 - either app's comment model
    """The comment a reply to `parent` is filed under: `parent` itself, or its top-level comment.
    One step is enough, because every reply is filed this way."""
    return parent.parent if parent.parent_id is not None else parent


def reply_parent(request: HttpRequest, comments: "QuerySet[Any]") -> Any:  # noqa: ANN401
    """The comment a reply in this POST is filed under, or None for a new top-level comment.

    `comments` is the thread's own comments, so a `parent` from another post or event is not a
    reply here. A reply to a comment deleted while it was being written is kept as a top-level
    comment, with a word about why: losing what somebody typed is worse than an answer out of its
    thread.
    """
    raw = request.POST.get("parent", "")
    if not raw:
        return None
    parent = comments.select_related("parent").filter(pk=int(raw) if raw.isdigit() else 0).first()
    if parent is None:
        messages.warning(
            request, "Kommentaren, du svarede på, er slettet, så dit svar står som en ny kommentar."
        )
        return None
    return thread_root(parent)
