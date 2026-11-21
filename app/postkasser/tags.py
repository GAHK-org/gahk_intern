"""A mailbox's tags (categories on sager): the edits the "Tags" page makes, and tagging a sag.

Like columns.py, the rules live HERE, in one place the page, the admin inline and the tests all go
through. Every edit runs in a transaction that first locks the mailbox row and then re-reads what it is
about to change, so two people editing at once (or a stale page) get a clean message, never a 500 or a
duplicate. Tags are attached to a sag (MailThread.tags), never to a single mail.
"""

from collections.abc import Iterable

from django.db import IntegrityError, transaction
from django.utils import timezone

from residents.models import Resident

from . import services
from .models import Mailbox, MailEvent, MailTag, MailThread, TagColor

MAX_NAME = 40


class TagError(Exception):
    """A refused edit; the message is Danish and meant to be shown to the person who tried it."""


def clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise TagError("Tagget skal have et navn.")
    if len(cleaned) > MAX_NAME:
        raise TagError(f"Navnet må højst være {MAX_NAME} tegn.")
    return cleaned


def clean_color(color: str) -> str:
    if color not in TagColor.values:
        raise TagError("Ukendt farve.")
    return color


def validate_names(names: Iterable[str]) -> None:
    """For the admin inline: every name valid and no two the same, ignoring case."""
    seen: set[str] = set()
    for name in names:
        cleaned = clean_name(name)
        if cleaned.casefold() in seen:
            raise TagError(f"Der er to tags der hedder «{cleaned}».")
        seen.add(cleaned.casefold())


def _lock(mailbox: Mailbox) -> None:
    """Serialise tag edits of one mailbox (a no-op on SQLite, which serialises writers anyway)."""
    Mailbox.objects.select_for_update().filter(pk=mailbox.pk).first()


def _fresh(tag: MailTag) -> MailTag:
    found = MailTag.objects.filter(pk=tag.pk, mailbox_id=tag.mailbox_id).first()
    if found is None:
        raise TagError("Tagget findes ikke længere.")
    return found


def _taken(mailbox: Mailbox, name: str, *, except_pk: int | None = None) -> bool:
    wanted = name.casefold()
    return any(
        existing.casefold() == wanted
        for pk, existing in mailbox.tags.values_list("pk", "name")
        if pk != except_pk
    )


def _next_position(mailbox: Mailbox) -> int:
    last = mailbox.tags.order_by("-position").values_list("position", flat=True).first()
    return 0 if last is None else last + 1


def add(mailbox: Mailbox, name: str, color: str = TagColor.GRAA) -> MailTag:
    cleaned, color = clean_name(name), clean_color(color)
    try:
        with transaction.atomic():
            _lock(mailbox)
            if _taken(mailbox, cleaned):
                raise TagError(f"Der er allerede et tag der hedder «{cleaned}».")
            return MailTag.objects.create(
                mailbox=mailbox, name=cleaned, color=color, position=_next_position(mailbox)
            )
    except IntegrityError as exc:  # the exact-name constraint, if two adds slipped past the lock
        raise TagError(f"Der er allerede et tag der hedder «{cleaned}».") from exc


def rename(tag: MailTag, name: str) -> None:
    cleaned = clean_name(name)
    with transaction.atomic():
        _lock(tag.mailbox)
        fresh = _fresh(tag)
        if _taken(fresh.mailbox, cleaned, except_pk=fresh.pk):
            raise TagError(f"Der er allerede et tag der hedder «{cleaned}».")
        fresh.name = cleaned
        fresh.save(update_fields=["name"])


def recolor(tag: MailTag, color: str) -> None:
    color = clean_color(color)
    with transaction.atomic():
        _lock(tag.mailbox)
        fresh = _fresh(tag)
        fresh.color = color
        fresh.save(update_fields=["color"])


def move(tag: MailTag, direction: int) -> None:
    """-1 = up the list, +1 = down. At an end it is a no-op. Positions are renumbered 0..n-1 first, so
    a swap is always between neighbours."""
    with transaction.atomic():
        _lock(tag.mailbox)
        fresh = _fresh(tag)
        ordered = list(fresh.mailbox.tags.all())
        for position, other in enumerate(ordered):
            if other.position != position:
                other.position = position
                other.save(update_fields=["position"])
        index = next(i for i, other in enumerate(ordered) if other.pk == fresh.pk)
        neighbour = index + direction
        if not 0 <= neighbour < len(ordered):
            return
        a, b = ordered[index], ordered[neighbour]
        a.position, b.position = b.position, a.position
        a.save(update_fields=["position"])
        b.save(update_fields=["position"])


def delete(tag: MailTag) -> int:
    """Delete a tag; the sager that carried it stay, without it. Returns how many carried it."""
    with transaction.atomic():
        _lock(tag.mailbox)
        fresh = _fresh(tag)
        carried = fresh.threads.count()
        fresh.delete()
    return carried


def set_for_thread(thread: MailThread, chosen: Iterable[int], resident: Resident) -> bool:
    """Give `thread` exactly the tags `chosen` (pks), and log it. Pks that are not this mailbox's tags
    are ignored — a tag of another group's mailbox is never applied. Returns whether anything changed."""
    mailbox = thread.mailbox
    with transaction.atomic():
        _lock(mailbox)  # like every other tag edit: a tag deleted a moment ago must not be re-applied
        valid = set(mailbox.tags.filter(pk__in=list(chosen)).values_list("pk", flat=True))
        current = set(thread.tags.values_list("pk", flat=True))
        if valid == current:
            return False
        thread.tags.set(valid)
        MailThread.objects.filter(pk=thread.pk).update(last_activity_at=timezone.now())
        names = ", ".join(mailbox.tags.filter(pk__in=valid).values_list("name", flat=True))
        services.log(
            mailbox,
            resident,
            MailEvent.Action.TAG,
            message=None,
            subject=thread.subject,
            thread=thread,
            detail=names or "ingen",
        )
    return True
