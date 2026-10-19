"""One-emoji-per-person reactions, shared by Den Hurtige and opslagstavlen.

The invariant both features rely on is "one person, one emoji per item": picking a different emoji
*moves* your reaction, picking the one you already used *clears* it. It is enforced in the database
by a UniqueConstraint on (item, author) in each app, which is what makes the toggle safe under a
double tap — and it is encoded once here so the two cannot drift into different semantics.

Deliberately generic over the owning field rather than over the model: each app keeps its own
concrete reaction table with a real foreign key. A single generic-FK table was considered and
rejected — a GenericForeignKey has no database-level FK, so a bulk delete (opslagstavlen's
retention purge) would leave orphaned reaction rows behind unless every model also carried a
GenericRelation. A purge that silently leaks rows for the lifetime of the board is exactly the
failure mode that feature must not have.
"""

from collections.abc import Iterable
from typing import Any, Protocol


class _Author(Protocol):
    @property
    def full_name(self) -> str: ...


class _Reaction(Protocol):
    """What this module needs of a reaction row: which emoji, and whose it is.

    Declared as read-only properties rather than plain attributes on purpose: a Protocol attribute is
    *invariant*, so `emoji: str` would refuse a Django model whose field descriptor is not exactly
    `str`. This function only ever reads them, so read-only is both truer and more permissive.

    `author` (the object) is needed as well as `author_id` because the reader panel names people.
    That makes the loaded author a requirement of every caller, which is deliberate: it is enforced
    by the Prefetch(..., select_related("author")) both apps pass, and folding the join into the
    reactions query rather than nesting a second prefetch keeps the query count flat whether an item
    has reactions or none — which is exactly what the "no extra query per reaction" tests assert.
    """

    @property
    def emoji(self) -> str: ...

    @property
    def author_id(self) -> int: ...

    @property
    def author(self) -> _Author: ...


def reaction_rows(reactions: Iterable[_Reaction], user_id: int) -> list[dict[str, object]]:
    """[{emoji, count, mine, people}] for one item, most-used first.

    Takes an iterable of reaction rows rather than the item itself, so callers pass a *prefetched*
    collection and this stays a pure function (unit-testable without a database). That is not just
    tidiness: a list page renders every item, so resolving counts with a per-item aggregate would be
    an N+1 — on Den Hurtige's 20-second poll it was six queries every twenty seconds per open tab.

    `people` is who used that emoji, in the order they reacted, for the reader panel that answers
    "who liked this?". Built here rather than in a second function so the panel can never disagree
    with the pills about ordering or grouping — they are the same list, rendered twice.

    Ties break on first use, which keeps the row from reshuffling under a thumb.
    """
    order: list[str] = []
    counts: dict[str, int] = {}
    people: dict[str, list[str]] = {}
    mine: set[str] = set()
    for reaction in reactions:
        if reaction.emoji not in counts:
            order.append(reaction.emoji)
            counts[reaction.emoji] = 0
            people[reaction.emoji] = []
        counts[reaction.emoji] += 1
        people[reaction.emoji].append(reaction.author.full_name)
        if reaction.author_id == user_id:
            mine.add(reaction.emoji)
    # First-use position captured *before* sorting: `order` is what we are sorting, so looking an
    # index up inside the key function would read a half-reordered list.
    first_seen = {e: i for i, e in enumerate(order)}
    order.sort(key=lambda e: (-counts[e], first_seen[e]))
    return [{"emoji": e, "count": counts[e], "mine": e in mine, "people": people[e]} for e in order]


CREATED = "created"
CHANGED = "changed"
DELETED = "deleted"


def apply_toggle(manager: Any, *, author: Any, emoji: str, **owner: Any) -> str:  # noqa: ANN401
    """Set, change or clear this person's one reaction on one item.

    `manager` is the reaction model's default manager and `owner` is the field identifying the item
    — `apply_toggle(QuickReaction.objects, author=me, emoji="👍", post=post)` or `notice=notice`.

    Three branches, and the unique constraint is what makes them safe: no row yet → create; same
    emoji → delete (re-tapping clears); different emoji → move it, rather than stacking a second.

    Returns which branch ran, so the caller notifies the item's author on CREATED only: swapping 👍
    for ❤️ is not news, and un-reacting must not buzz anyone's phone.
    """
    existing = manager.filter(author=author, **owner).first()
    if existing is None:
        manager.create(author=author, emoji=emoji, **owner)
        return CREATED
    if existing.emoji == emoji:
        existing.delete()
        return DELETED
    existing.emoji = emoji
    existing.save(update_fields=["emoji"])
    return CHANGED
