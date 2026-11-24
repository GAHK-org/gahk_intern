"""Who is told what about the postkasser — the policy half of its notifications (the notifications app is
the machinery), in the way reparationer/services.py does it. Three categories, all under "Postkasser":

  * a NEW sag arrives -> everybody who can open the mailbox now (`access.members`), except whoever a rule
    has just handed it to (they get the "assigned" one instead);
  * a new mail arrives on a sag that has an assignee -> the assignee, if they can still open the mailbox;
  * a sag is handed to someone by somebody else, by hand or by a rule -> that someone. Never when you
    hand it to yourself.

Never the body of a mail: the subject and who wrote it, like a card on the board. Called by the sync once
per sag per run (one notification, however many mails came in), never for a folder's first sync or for
mail that merely moved folder — services decides that; this module only sends.

Never inside a claim and never inline in a web request: the sync and the moves only COLLECT what to tell
(`Pending`); their callers `send` it after the claim is released, in the background unless they are the
sync command / Celery task.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial

from django.urls import reverse

from notifications import categories, services
from residents.models import Resident

from . import access
from .models import ColumnRole, Mailbox, MailMessage, MailThread

logger = logging.getLogger(__name__)

# A sync that finds more new sager than this (a folder somebody dragged a pile of mail into) tells the
# group about this many and no more.
MAX_NEW_SAG_NOTICES = 20


def thread_url(thread: MailThread) -> str:
    return reverse("postkasser:thread", args=[thread.mailbox.slug, thread.pk])


def _body(thread: MailThread, message: MailMessage | None) -> str:
    sender = message.sender if message is not None else ""
    return (
        f"{thread.subject or '(intet emne)'} — fra {sender}" if sender else thread.subject or "(intet emne)"
    )


def new_sag(
    thread: MailThread,
    message: MailMessage,
    *,
    members: Iterable[int],
    skip: int | None = None,
    background: bool = True,
) -> None:
    mailbox = thread.mailbox
    services.notify(
        members,
        category=categories.MAIL_NEW_SAG,
        title=f"Ny sag i {mailbox.name}",
        body=_body(thread, message),
        url=thread_url(thread),
        exclude=skip,
        background=background,
    )


def assigned_reply(
    thread: MailThread, message: MailMessage, *, member_ids: set[int], background: bool = True
) -> None:
    """Nothing if there is no assignee, or they can no longer open the mailbox (they left the group)."""
    if thread.assignee_id is None or thread.assignee_id not in member_ids:
        return
    services.notify(
        [thread.assignee_id],
        category=categories.MAIL_ASSIGNED_REPLY,
        title="Nyt svar på en sag, du er ansvarlig for",
        body=_body(thread, message),
        url=thread_url(thread),
        background=background,
    )


def assigned(thread: MailThread, assignee: Resident, *, by: Resident | None, background: bool = True) -> None:
    """`by` is who handed it over (None: a rule). Nothing if that is the assignee themself."""
    if by is not None and by.pk == assignee.pk:
        return
    services.notify(
        [assignee.pk],
        category=categories.MAIL_ASSIGNED,
        title="Du er blevet ansvarlig for en sag",
        body=_body(thread, None),
        url=thread_url(thread),
        background=background,
    )


@dataclass
class Pending:
    """What a sync (or a move that synced) found worth telling people, collected while it held the claim:
    the sager born and the first mail that started each, the ones that got a new mail, and the ones a rule
    handed to someone (sag pk -> resident pk)."""

    mailbox: Mailbox
    created: dict[int, MailMessage] = field(default_factory=dict)
    replies: dict[int, MailMessage] = field(default_factory=dict)
    assigned_by_rule: dict[int, int] = field(default_factory=dict)

    def send(self, *, background: bool) -> None:
        after_sync(
            self.mailbox,
            created=self.created,
            replies=self.replies,
            assigned_by_rule=self.assigned_by_rule,
            background=background,
        )


def _safely(what: str, mailbox: Mailbox, send: Callable[[], None]) -> None:
    """One notification; a failure is logged and never stops the next one (nor the caller)."""
    try:
        send()
    except Exception:
        logger.exception("postkasser: %s notification for %s failed", what, mailbox.address)


def after_sync(
    mailbox: Mailbox,
    *,
    created: dict[int, MailMessage],
    replies: dict[int, MailMessage],
    assigned_by_rule: dict[int, int],
    background: bool = True,
) -> None:
    """One notification per sag for a whole sync. Reads each sag fresh (a rule may have changed it, or a
    later step deleted it). Never raises: a failed notification must not fail the sync, nor stop the next.
    `background` is False only for the sync command / Celery task (core.push.send_many)."""
    try:
        threads = {
            t.pk: t
            for t in MailThread.objects.select_related("mailbox", "column").filter(
                mailbox=mailbox, pk__in={*created, *replies, *assigned_by_rule}
            )
        }
        member_ids = set(access.members(mailbox).values_list("pk", flat=True))
    except Exception:
        logger.exception("postkasser: notifications for %s failed", mailbox.address)
        return
    eligible = sent = 0
    for pk, message in created.items():
        thread = threads.get(pk)
        # A sag that ended the sync in Færdig (history, a newsletter rule) is not news.
        if thread is None or thread.column.role == ColumnRole.FAERDIG:
            continue
        eligible += 1
        if sent >= MAX_NEW_SAG_NOTICES:
            continue
        sent += 1
        _safely(
            "new sag",
            mailbox,
            partial(
                new_sag,
                thread,
                message,
                members=member_ids,
                skip=assigned_by_rule.get(pk),
                background=background,
            ),
        )
    if eligible > sent:
        logger.warning(
            "postkasser: %s: %s new sager in one sync, only the first %s announced",
            mailbox.address,
            eligible,
            MAX_NEW_SAG_NOTICES,
        )
    for pk, message in replies.items():
        thread = threads.get(pk)
        # (a rule that handed it over on this very mail tells the assignee once, below)
        if pk not in created and thread is not None and assigned_by_rule.get(pk) != thread.assignee_id:
            _safely(
                "reply",
                mailbox,
                partial(assigned_reply, thread, message, member_ids=member_ids, background=background),
            )
    for pk, resident_id in assigned_by_rule.items():
        thread = threads.get(pk)
        if thread is None or thread.assignee_id != resident_id:
            continue
        _safely(
            "assignment",
            mailbox,
            partial(_tell_assignee, thread, resident_id, background=background),
        )


def _tell_assignee(thread: MailThread, resident_id: int, *, background: bool) -> None:
    assignee = Resident.objects.filter(pk=resident_id, is_active=True).first()
    if assignee is not None:
        assigned(thread, assignee, by=None, background=background)
