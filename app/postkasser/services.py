"""What the views and the sync task do to a mailbox: sync folders and thread the mail into sager, open a
message, send a mail, move mail between one.com's folders, and move or hand over a sag on the board."""

import logging
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import formataddr, formatdate, make_msgid
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from residents.models import Resident

from . import columns, imap, notify, render, utf7
from .models import (
    BoardColumn,
    ColumnRole,
    Mailbox,
    MailEvent,
    MailFolder,
    MailMessage,
    MailRule,
    MailThread,
)

if TYPE_CHECKING:
    from . import rules

logger = logging.getLogger(__name__)

# First sync of a folder with years of mail imports only the newest this many; older mail is still
# in one.com's webmail. Keeps the first run (and the header cache) bounded. The inbox is where the
# work is, so it gets the deeper history.
INITIAL_SYNC_LIMIT = 500
OTHER_FOLDER_SYNC_LIMIT = 200
# Folders that are never shown and never feed the board.
SKIPPED_USES = {
    MailFolder.SpecialUse.DRAFTS,
    MailFolder.SpecialUse.TRASH,
    MailFolder.SpecialUse.JUNK,
}
# A sag started by a mail sent from the intranet has no message until the sent copy is synced. It is
# kept that long before the empty-sag cleanup may take it (the copy may never come: a failed APPEND).
PENDING_GRACE = timedelta(days=2)
# A sag found empty while some folder could not be read is only hidden (MailThread.emptied_at); it is
# deleted at the first clean run, or when it has been hidden this long.
EMPTY_THREAD_TTL = timedelta(days=7)
MAX_EMPTIED_IDS = 20_000  # characters of Message-IDs kept on a hidden sag
# A sync or a move holds Mailbox.sync_started_at while it runs; a claim older than this belongs to a run
# that died without releasing it (a killed worker) and is taken over. A run that is alive renews its
# claim (Claim.renew) after every folder / move group / fetch batch, so the TTL only has to cover the
# longest single step (one IMAP round trip is bounded by imap.TIMEOUT_SECONDS), never a whole sync.
# Short on purpose: after a crash nobody can sync, move or refresh until the TTL has passed, so every
# minute of it is a minute of lockout. Long enough that a slow step is not mistaken for a dead run and
# taken over — which would let two runs interleave (a stale run then notices on its next renew and
# aborts with SyncBusy).
CLAIM_TTL = timedelta(minutes=3)
SYNC_BUSY = "Postkassen synkroniseres allerede — prøv igen om lidt."
SYNC_LOST = "Postkassen blev overtaget af en anden synkronisering — afbrudt, prøv igen om lidt."
STALE_FOLDER = "Mappen «{}» er ændret siden sidste synkronisering — hent post og prøv igen"
# Folders mail can be moved to, and the ones that are deleted from the board instead of re-filed.
UNMOVABLE_TARGETS = {MailFolder.SpecialUse.SENT, MailFolder.SpecialUse.DRAFTS}
REMOVING_TARGETS = {MailFolder.SpecialUse.TRASH, MailFolder.SpecialUse.JUNK}
FETCH_BATCH = 50
# The follow-up sync of the Sent folder after a send refreshes the flags of only this many of its newest
# cached mails (the regular sync refreshes all of them).
SENT_FLAG_WINDOW = 200
# What counts as NEW mail (for the rules and the notifications) in an already-synced folder:
#   * a sync that brings in at most LIVE_BATCH new UIDs: all of them. That is what live mail looks like —
#     and a mail rescued from Spam keeps its original INTERNALDATE on most servers (COPY/MOVE preserve it),
#     so dates cannot tell it from history;
#   * a bigger batch (somebody dragged a pile of old mail in, or the server was unreachable for days): only
#     the mail whose INTERNALDATE (when the server received it — NOT the sender's Date header, which can say
#     anything) is not older than the folder's previous sync minus RECENT_SLACK. Date is the fallback when
#     the server gave no INTERNALDATE.
# A folder's first sync is never "new" (history import).
RECENT_SLACK = timedelta(days=1)
LIVE_BATCH = 20
HEADER_SPEC = "(UID FLAGS RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER])"


_ATTRIBUTE_USES = {
    r"\sent": MailFolder.SpecialUse.SENT,
    r"\drafts": MailFolder.SpecialUse.DRAFTS,
    r"\trash": MailFolder.SpecialUse.TRASH,
    r"\junk": MailFolder.SpecialUse.JUNK,
    r"\archive": MailFolder.SpecialUse.ARCHIVE,
}
# For servers that do not send SPECIAL-USE attributes: the last path segment, lower-cased.
_NAME_USES = {
    **dict.fromkeys(["sent", "sendt", "sent items", "sent messages", "sendte"], MailFolder.SpecialUse.SENT),
    **dict.fromkeys(["trash", "papirkurv", "deleted items", "slettet"], MailFolder.SpecialUse.TRASH),
    **dict.fromkeys(["junk", "spam", "uønsket"], MailFolder.SpecialUse.JUNK),
    **dict.fromkeys(["drafts", "kladder"], MailFolder.SpecialUse.DRAFTS),
}


class SyncBusy(Exception):
    """Another sync or move holds the mailbox (see `claim`), or took it over from this run."""


class StaleFolder(imap.MailboxError):
    """A folder's UIDVALIDITY is not the one the cached UIDs were read under: they may now point at
    other mails, so nothing is done to them. Danish message, for the user."""


class Claim:
    """The token `claim` yields: truthy while the mailbox is held. The claim's value is the timestamp
    in `Mailbox.sync_started_at`; `renew` moves it forward and notices if somebody else has taken over."""

    def __init__(self, mailbox: Mailbox, at: datetime | None) -> None:
        self.mailbox = mailbox
        self.at = at

    def __bool__(self) -> bool:
        return self.at is not None

    def renew(self) -> None:
        """Heartbeat: a compare-and-swap on the timestamp this run last wrote. If it is no longer there
        (a takeover after the TTL, or the claim was cleared) the claim is lost — raise SyncBusy so the run
        aborts cleanly rather than carrying on beside its successor."""
        if self.at is None:
            raise SyncBusy(SYNC_LOST)
        now = timezone.now()
        if not Mailbox.objects.filter(pk=self.mailbox.pk, sync_started_at=self.at).update(
            sync_started_at=now
        ):
            self.at = None
            raise SyncBusy(SYNC_LOST)
        self.at = now


@contextmanager
def claim(mailbox: Mailbox) -> Iterator[Claim]:
    """Take the mailbox for one sync or move; yields a `Claim`, falsy if somebody else holds it.

    A compare-and-swap UPDATE in the database, because the contenders are a Celery worker and web
    requests in other processes: a process-local lock or the (locmem) cache would not see each other.
    Taken only if nobody holds it or the holder's claim is older than CLAIM_TTL; kept alive with
    `Claim.renew`; released in `finally` with the LATEST timestamp, and only if the claim is still ours
    (a run that outlived the TTL must not free its successor's)."""
    now = timezone.now()
    taken = (
        Mailbox.objects.filter(pk=mailbox.pk)
        .filter(Q(sync_started_at__isnull=True) | Q(sync_started_at__lt=now - CLAIM_TTL))
        .update(sync_started_at=now)
    )
    token = Claim(mailbox, now if taken else None)
    try:
        yield token
    finally:
        if token.at is not None:
            Mailbox.objects.filter(pk=mailbox.pk, sync_started_at=token.at).update(sync_started_at=None)


def special_use(info: imap.FolderInfo, display_name: str, *, guess_from_name: bool = True) -> str:
    """What a folder is for: INBOX by name (RFC 3501 makes it case-insensitive and always present), then
    the server's own attributes, then — unless `guess_from_name` is off — a guess from the name.
    The guess is only made for top-level folders and those directly under INBOX: a folder somebody made
    called "Spam" three levels down is theirs, not the server's junk folder."""
    if info.name.upper() == imap.INBOX:
        return MailFolder.SpecialUse.INBOX
    for attribute in info.attributes:
        if attribute in _ATTRIBUTE_USES:
            return _ATTRIBUTE_USES[attribute]
    if not guess_from_name:
        return MailFolder.SpecialUse.NONE
    return _NAME_USES.get(display_name.strip().lower(), MailFolder.SpecialUse.NONE)


def _apply_flags(message: MailMessage, flags: set[str]) -> bool:
    seen, answered = r"\Seen" in flags, r"\Answered" in flags
    changed = (message.seen, message.answered) != (seen, answered)
    message.seen, message.answered = seen, answered
    return changed


_SUBJECT_PREFIX = re.compile(r"^\s*((re|sv|aw|fwd?|vs)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)
_MESSAGE_ID = re.compile(r"<[^<>\s]+>")


def thread_subject(subject: str) -> str:
    """The subject without Re:/Sv:/Fwd:/VS: in front, so every mail of a sag shares one title."""
    return (_SUBJECT_PREFIX.sub("", subject).strip() or subject.strip())[:998]


def _ids(text: str) -> list[str]:
    """Message-IDs out of an In-Reply-To / References value (`<a@x> <b@y>`), in order."""
    return _MESSAGE_ID.findall(text)


@dataclass
class _Run:
    """What one mailbox sync carries from folder to folder."""

    mailbox: Mailbox
    created: int = 0
    # Message-ID -> sag, for mail that was in the cache a moment ago and is gone now (moved to
    # another folder, folder renamed, UIDVALIDITY reset). The copy that shows up in its new place
    # joins the same sag, whichever of the two folders is synced first.
    moved: dict[str, int] = field(default_factory=dict)
    # The same for mail with no Message-ID: its fingerprint (see _fingerprint) -> sag.
    moved_prints: dict[tuple, int] = field(default_factory=dict)
    # The claim this run holds, renewed as it goes (None: a caller that holds none, e.g. a test).
    claim: Claim | None = None
    # Rules and notifications (see _automate): the rules of this run, loaded on first use, and what the
    # sync tells people at its end — the sager born in this run and the first mail that started each, the
    # ones that got a new mail, and the ones a rule handed to someone (sag pk -> resident pk).
    ruleset: "rules.RuleSet | None" = None
    born: dict[int, MailMessage] = field(default_factory=dict)
    replies: dict[int, MailMessage] = field(default_factory=dict)
    assigned: dict[int, int] = field(default_factory=dict)
    # Sager born during a folder's first sync — see _settle_first_syncs.
    fresh: set[int] = field(default_factory=set)
    _ny: BoardColumn | None = None

    def ny(self) -> BoardColumn:
        if self._ny is None:
            self._ny = columns.ny_column(self.mailbox)
        return self._ny

    def remember(self, messages: "QuerySet[MailMessage]") -> None:
        filed = messages.exclude(thread=None)
        self.moved.update(filed.exclude(message_id="").values_list("message_id", "thread_id"))
        for message in filed.filter(message_id="", date__isnull=False):
            self.moved_prints[_fingerprint(message)] = message.thread_id  # type: ignore[assignment]

    def heartbeat(self) -> None:
        if self.claim is not None:
            self.claim.renew()

    def checkpoint(self) -> tuple:
        return self.created, dict(self.born), dict(self.replies), dict(self.assigned), set(self.fresh)

    def rollback_to(self, checkpoint: tuple) -> None:
        """Forget what a folder's rolled-back transaction recorded: its sager do not exist (and their
        primary keys may be handed out again)."""
        self.created, born, replies, assigned, fresh = checkpoint
        self.born.clear()
        self.born.update(born)
        self.replies.clear()
        self.replies.update(replies)
        self.assigned.clear()
        self.assigned.update(assigned)
        self.fresh.clear()
        self.fresh.update(fresh)

    def pending(self) -> notify.Pending:
        return notify.Pending(self.mailbox, dict(self.born), dict(self.replies), dict(self.assigned))


def _fingerprint(message: MailMessage) -> tuple:
    """What stands in for a Message-ID on a mail that has none: the same mail moved to another folder
    keeps sender, date, subject and size (RFC822.SIZE)."""
    return (message.from_address.lower(), message.date, message.subject, message.size)


def _refresh_folders(run: _Run, infos: list[imap.FolderInfo]) -> list[MailFolder]:
    """Upsert a MailFolder per listed folder (saving only those that changed — this runs every two
    minutes), delete the ones the server no longer has, and empty the ones that are never shown.
    Returns the live folders, inbox first, then by path."""
    mailbox = run.mailbox
    existing = {f.name: f for f in mailbox.folders.all()}
    live: dict[str, MailFolder] = {}
    with transaction.atomic():
        for info in infos:
            if info.name in live:
                continue
            decoded = utf7.decode(info.name)
            parts = decoded.split(info.delimiter) if info.delimiter else [decoded]
            top_level = len(parts) == 1 or (len(parts) == 2 and parts[0].upper() == imap.INBOX)
            use = special_use(info, parts[-1], guess_from_name=top_level)
            fields = {
                "display_name": "Indbakke" if use == MailFolder.SpecialUse.INBOX else parts[-1],
                "path": "/".join(parts),
                "depth": len(parts) - 1,
                "delimiter": info.delimiter,
                "special_use": use,
                "selectable": r"\noselect" not in info.attributes and r"\nonexistent" not in info.attributes,
            }
            folder = existing.get(info.name)
            if folder is None:
                folder = MailFolder.objects.create(mailbox=mailbox, name=info.name, **fields)
            else:
                changed = [name for name, value in fields.items() if getattr(folder, name) != value]
                for name in changed:
                    setattr(folder, name, fields[name])
                if changed:
                    folder.save(update_fields=changed)
            live[info.name] = folder
        for name, folder in existing.items():
            if name not in live:
                run.remember(folder.messages.all())
                # Switched off BEFORE the folder goes: the link is then cleared (SET_NULL), and a rule that
                # said "mail in Faktura" must not quietly become "all mail".
                MailRule.objects.filter(folder=folder).update(
                    is_active=False,
                    disabled_reason=f"Slået fra: mappen «{folder.display_name}» findes ikke længere",
                )
                folder.delete()
        for folder in live.values():
            if folder.special_use in SKIPPED_USES:
                folder.messages.all().delete()
    return sorted(live.values(), key=lambda f: (f.special_use != MailFolder.SpecialUse.INBOX, f.path.lower()))


def sync(mailbox: Mailbox, *, background: bool = True) -> int:
    """Sync under the mailbox's claim (see `claim`): raises SyncBusy if another sync or a move holds it,
    so two overlapping runs — a beat tick and the refresh button, say — never interleave. The work
    itself is `_sync`. The notifications are sent AFTER the claim is released, in the background
    unless `background` is False — which only the sync command / Celery task pass."""
    run = _Run(mailbox)
    try:
        with claim(mailbox) as claimed:
            if not claimed:
                raise SyncBusy(SYNC_BUSY)
            run.claim = claimed
            _sync(run)
    finally:
        # Also when the run aborted half way (a lost claim, an unexpected error): the folders that had
        # committed have told the database about their mail, and nobody would ever be told otherwise —
        # the next sync no longer sees that mail as new.
        if run.born or run.replies or run.assigned:
            run.pending().send(background=background)
    return run.created


def _sync(run: _Run) -> None:
    """Bring the header cache and the sager in line with the server. How many messages came in, and what
    to tell people about them, is left on `run` (the caller sends it once the claim is released — also if
    this raises, for the folders that had committed).

    The folders are listed first and mirrored as MailFolder rows; then every selectable folder except
    drafts, trash and junk is synced on its own (_sync_folder). One folder failing is recorded and
    skipped, not allowed to stop the others — a group's inbox must not stop updating because some
    other folder will not open. Only a failure of the connection or the login as a whole raises.

    Sager with no mail left are deleted here, at the very END: a mail moved between folders is pruned
    from one and re-filed in the other, and the sag (with the status the group gave it) must still be
    there when it arrives. If a folder failed, the mail may be sitting in it, so the sager that lost a
    mail are only hidden (MailThread.emptied_at) until a run where every folder answers.

    `run.claim` is the claim the caller holds; it is renewed after every folder (and fetch batch), and a
    claim lost to a takeover raises SyncBusy, aborting before anything is cleaned up. Any other failure
    inside a folder is that folder's error, like a MailboxError.
    """
    mailbox = run.mailbox
    errors: list[str] = []
    attempted = 0
    try:
        with imap.connect(mailbox) as conn:
            for folder in _refresh_folders(run, conn.list_folders()):
                if not folder.selectable or folder.special_use in SKIPPED_USES:
                    continue
                attempted += 1
                try:
                    _sync_folder(run, conn, folder)
                except SyncBusy:
                    raise
                except OSError:
                    # A socket timeout/reset mid-response leaves unread FETCH lines on the connection;
                    # the next folder's SELECT/FETCH would read them as its own and cache another
                    # folder's headers under its UIDs. Abort the whole run — connect() turns it into a
                    # MailboxError, and sync()'s finally still announces what earlier folders committed.
                    raise
                except imap.MailboxError as exc:
                    logger.warning("postkasser: %s / %s: %s", mailbox.address, folder.name, exc)
                    errors.append(f"{folder.display_name}: {exc}")
                except Exception as exc:
                    logger.exception("postkasser: %s / %s failed", mailbox.address, folder.name)
                    errors.append(f"{folder.display_name}: {exc}")
                run.heartbeat()
    except imap.MailboxError as exc:
        mailbox.last_sync_error = str(exc)
        mailbox.save(update_fields=["last_sync_error"])
        raise

    run.heartbeat()
    with transaction.atomic():
        _settle_first_syncs(run)
        _settle_empty_threads(run, clean=not errors)
        if len(errors) < attempted or not attempted:
            mailbox.last_synced_at = timezone.now()
        mailbox.last_sync_error = "; ".join(errors)
        mailbox.save(update_fields=["last_synced_at", "last_sync_error"])


def _sync_folder(
    run: _Run, conn: imap.Connection, folder: MailFolder, *, flag_window: int | None = None
) -> None:
    """One folder, three idempotent steps (syncs of one mailbox never overlap, see `claim`):
      1. UIDVALIDITY changed → the cached UIDs point at nothing; drop them all.
      2. Delete cached rows whose UID is gone from the server (deleted/moved in one.com's webmail).
      3. Fetch headers for UIDs not yet cached, and refresh flags (read/answered) for the rest —
         someone may have read a mail in one.com, or on their phone.
    Everything is read from the server first and written in one transaction after, so a folder that
    fails half way changes nothing. The caller holds the mailbox's claim.
    `flag_window`: refresh the flags of only that many of the newest cached UIDs (the follow-up sync of
    the Sent folder after a send; the regular sync refreshes them all)."""
    mailbox = run.mailbox
    uidvalidity = conn.select(folder.name)
    reset = folder.uidvalidity != uidvalidity
    # A UIDVALIDITY reset re-imports the whole folder, exactly like the first sync does.
    first = folder.last_synced_at is None or reset
    limit = (
        INITIAL_SYNC_LIMIT if folder.special_use == MailFolder.SpecialUse.INBOX else OTHER_FOLDER_SYNC_LIMIT
    )

    server_uids = sorted(conn.all_uids())
    cached = {} if reset else {m.uid: m for m in folder.messages.all()}
    gone = cached.keys() - set(server_uids)
    cached = {uid: m for uid, m in cached.items() if uid not in gone}
    if cached:
        # IMAP UIDs only ever grow, so anything below the oldest cached UID was deliberately
        # left out by the initial limit (or deleted from the cache) — it is not "new".
        oldest = min(cached)
        new_uids = [uid for uid in server_uids if uid > oldest and uid not in cached]
    else:
        new_uids = server_uids[-limit:]

    # What counts as new mail here: see LIVE_BATCH.
    live = not first and len(new_uids) <= LIVE_BATCH
    cutoff = folder.last_synced_at - RECENT_SLACK if folder.last_synced_at is not None else None
    created: list[tuple[MailMessage, datetime | None, bool]] = []
    for start in range(0, len(new_uids), FETCH_BATCH):
        run.heartbeat()
        batch = new_uids[start : start + FETCH_BATCH]
        for item in conn.fetch(batch, HEADER_SPEC):
            if item.uid not in batch:  # an unsolicited FETCH for some other message
                continue
            message = MailMessage(mailbox=mailbox, folder=folder, uid=item.uid, size=item.size)
            try:
                fields = render.header_fields(item.payload)
            except Exception:
                logger.warning("postkasser: unreadable headers for %s/%s", mailbox.address, item.uid)
                fields = {}
            for name, value in fields.items():
                setattr(message, name, value)
            _apply_flags(message, item.flags)
            arrived = item.internaldate or message.date
            old = not first and not live and cutoff is not None and arrived is not None and arrived < cutoff
            created.append((message, item.internaldate, old))

    changed = []
    if cached:
        window = sorted(cached)[-flag_window:] if flag_window else sorted(cached)
        for item in conn.fetch_flags(window[0]):
            known = cached.get(item.uid)
            if known is not None and _apply_flags(known, item.flags):
                changed.append(known)

    run.heartbeat()
    checkpoint = run.checkpoint()
    try:
        with transaction.atomic():
            if reset:
                run.remember(folder.messages.all())
                folder.messages.all().delete()
            elif gone:
                run.remember(folder.messages.filter(uid__in=gone))
                folder.messages.filter(uid__in=gone).delete()
            for message, internaldate, old in created:
                if _file_message(run, folder, message, first=first, old=old, internaldate=internaldate):
                    run.created += 1
            MailMessage.objects.bulk_update(changed, ["seen", "answered"])
            folder.uidvalidity = uidvalidity
            folder.last_synced_at = timezone.now()
            folder.save(update_fields=["uidvalidity", "last_synced_at"])
    except BaseException:
        run.rollback_to(checkpoint)  # the sager noted for the notifications went with the rollback
        raise


def _lookup_thread(run: _Run, ids: list[str]) -> MailThread | None:
    """The sag that already holds a mail with one of these Message-IDs (earlier in `ids` wins): a
    cached message, one that was cached a moment ago (run.moved), one a hidden sag lost in an earlier run
    (emptied_ids), or a mail just sent from the intranet whose copy has not been synced yet
    (pending_message_id)."""
    if not ids:
        return None
    mailbox = run.mailbox
    found: dict[str, int] = {i: run.moved[i] for i in ids if i in run.moved}
    for message_id, thread_id in mailbox.messages.filter(
        message_id__in=ids, thread__isnull=False
    ).values_list("message_id", "thread_id"):
        found.setdefault(message_id, thread_id)
    for thread_id, pending in mailbox.threads.filter(pending_message_id__in=ids).values_list(
        "pk", "pending_message_id"
    ):
        found.setdefault(pending, thread_id)
    unresolved = [i for i in ids if i not in found]
    if unresolved:
        # A sag hidden by an earlier run (emptied_at) while a folder was unreadable: the mail it lost
        # is the one coming back now.
        for thread_id, lost in mailbox.threads.filter(emptied_at__isnull=False).values_list(
            "pk", "emptied_ids"
        ):
            for message_id in lost.split():
                if message_id in unresolved:
                    found.setdefault(message_id, thread_id)
    for message_id in ids:
        if message_id in found:
            thread = (
                MailThread.objects.select_related("column")
                .filter(pk=found[message_id], mailbox=mailbox)
                .first()
            )
            if thread is not None:
                return thread
    return None


def _lookup_by_fingerprint(run: _Run, folder: MailFolder, message: MailMessage) -> MailThread | None:
    """The sag of the same mail in another folder, for a mail with no Message-ID — only used where a
    Message-ID would have been (a move: the copy in the new place). Needs a Date, or the fingerprint
    is too weak; within this mailbox only."""
    if message.date is None:
        return None
    thread_id = run.moved_prints.get(_fingerprint(message))
    if thread_id is None:
        thread_id = (
            run.mailbox.messages.filter(
                message_id="",
                from_address__iexact=message.from_address,
                date=message.date,
                subject=message.subject,
                size=message.size,
                thread__isnull=False,
            )
            .exclude(folder=folder)
            .values_list("thread_id", flat=True)
            .first()
        )
    if thread_id is None:
        return None
    return MailThread.objects.select_related("column").filter(pk=thread_id, mailbox=run.mailbox).first()


def _file_message(
    run: _Run,
    folder: MailFolder,
    message: MailMessage,
    *,
    first: bool,
    old: bool = False,
    internaldate: datetime | None = None,
) -> bool:
    """Store one new header row and put it in a sag. Returns False if the row already existed (an
    overlapping sync got there first). `old`: history somebody dragged into the folder (see LIVE_BATCH,
    decided by _sync_folder); `internaldate`: when the server received the mail.

    The row is saved FIRST, without a sag; only when that worked is a sag created, reopened or joined.
    Done the other way round, a lost race left a brand-new empty sag behind (an empty card on the board)."""
    mailbox = run.mailbox
    in_sent = folder.special_use == MailFolder.SpecialUse.SENT
    incoming = not in_sent and message.from_address.lower() != mailbox.address.lower()
    # The mail's own Message-ID first: that is the same mail again, moved to another folder, and it
    # must go back to ITS sag. Only then what it replies to, most recent ancestor first. Never the
    # subject alone — "Hej" and "Spørgsmål" would merge strangers' mail.
    own = [message.message_id] if message.message_id else []
    thread = _lookup_thread(run, own) if own else _lookup_by_fingerprint(run, folder, message)
    same_mail = thread is not None
    if thread is None:
        replied_to = [*_ids(message.in_reply_to), *reversed(_ids(message.references))]
        thread = _lookup_thread(run, [i for i in dict.fromkeys(replied_to) if i not in own])
    if thread is None and in_sent:
        # Our own mail with nothing to attach to. Mail composed in the intranet already has its
        # sag (start_thread); one written in one.com's webmail is not a case for the board.
        return _save(message)
    message.thread = None
    if not _save(message):
        return False
    born = thread is None
    if thread is None:
        thread = MailThread.objects.create(
            mailbox=mailbox, subject=thread_subject(message.subject), column=run.ny()
        )
        if first or (old and message.seen):  # history: see _settle_first_syncs
            run.fresh.add(thread.pk)
    else:
        if same_mail and thread.pending_message_id == message.message_id:
            thread.pending_message_id = ""
            thread.save(update_fields=["pending_message_id"])
        # A genuinely new reply from outside reopens a sag we had answered or closed. Not the same
        # mail turning up again in another folder, and not a sag born in this very sync.
        if (
            incoming
            and not same_mail
            and thread.pk not in run.fresh
            and thread.column.role
            in (
                ColumnRole.AFVENTER,
                ColumnRole.FAERDIG,
            )
        ):
            _set_column(thread, run.ny(), None)
    message.thread = thread
    message.save(update_fields=["thread"])
    if thread.emptied_at is not None:  # a mail has come back to a sag that was hidden as empty
        thread.emptied_at, thread.emptied_ids = None, ""
        thread.save(update_fields=["emptied_at", "emptied_ids"])
    stamp = message.date or internaldate or thread.last_message_at or timezone.now()
    changed = []
    if thread.last_message_at is None or stamp > thread.last_message_at:
        thread.last_message_at = stamp
        changed.append("last_message_at")
    # A mail that is only the same one in another folder is not activity. Activity is when the mail ARRIVED
    # (what the time rules go by), not the Date it claims — last_message_at keeps that.
    if not same_mail:
        thread.last_activity_at = timezone.now()
        changed.append("last_activity_at")
    if changed:
        thread.save(update_fields=changed)
    # Rules are for NEW incoming mail: not a folder's first sync (a historic import), not a mail that
    # merely turned up again after a move, not our own, not old mail that was dragged into the folder. A
    # mail somebody has already read (webmail, phone) before the sync still gets the rules; it only does not
    # notify anybody — there is nothing to tell people about what has been read.
    if incoming and not first and not same_mail and not old:
        _automate(run, thread, message, born=born, announce=not message.seen)
    return True


def _automate(run: _Run, thread: MailThread, message: MailMessage, *, born: bool, announce: bool) -> None:
    """Run the mailbox's rules over a new incoming mail and remember it for the notifications at the end
    of the sync. Never raises: the mail is filed, whatever a rule or a notification does."""
    from . import rules  # here, not at the top: rules imports this module

    if announce:
        (run.born if born else run.replies)[thread.pk] = message
    try:
        with transaction.atomic():  # a savepoint: a failing query must not poison the folder's transaction
            if run.ruleset is None:
                run.ruleset = rules.RuleSet(run.mailbox, rules.load(run.mailbox))
            outcome = rules.apply_to_new_mail(run.ruleset, thread, message)
    except Exception:
        logger.exception("postkasser: rules for %s/%s failed", run.mailbox.address, message.uid)
        return
    if outcome.assigned is not None:
        run.assigned[thread.pk] = outcome.assigned.pk


def _save(message: MailMessage) -> bool:
    try:
        with transaction.atomic():
            message.save()
    except IntegrityError:
        return False
    return True


def _settle_first_syncs(run: _Run) -> None:
    """A folder's first sync imports history. Making every old conversation a fresh "Ny" card would
    bury the board on day one, so a sag born then starts as Færdig — unless something in it is still
    unread, which is exactly what someone should look at. The same goes for old, already read mail that
    turns up later in a folder that was synced before (dragged in in webmail)."""
    if not run.fresh:
        return
    done = columns.faerdig_column(run.mailbox)
    for thread in MailThread.objects.filter(pk__in=run.fresh):
        if not thread.messages.filter(seen=False).exists():
            thread.column = done
            thread.save(update_fields=["column"])


def _settle_empty_threads(run: _Run, *, clean: bool) -> None:
    """The end of a sync: what to do with sager that have no mail left (except those still waiting for
    the copy of a mail sent from the intranet, for PENDING_GRACE).

    A run where every folder answered knows the mail is gone: they are deleted. A run where a folder
    failed does not — the mail may be sitting in it — so they are only stamped `emptied_at` (hidden by
    the board; the Message-IDs they lost are kept so the mail can rejoin them) and deleted by the first
    clean run, or after EMPTY_THREAD_TTL whatever happens. A sag that has mail again loses its stamp."""
    now = timezone.now()
    threads = MailThread.objects.filter(mailbox=run.mailbox)
    threads.filter(emptied_at__isnull=False, messages__isnull=False).update(emptied_at=None, emptied_ids="")
    empty = threads.filter(messages__isnull=True).exclude(
        pending_message_id__gt="", created_at__gte=now - PENDING_GRACE
    )
    if clean:
        empty.delete()
        return
    for thread in list(empty):
        lost = set(thread.emptied_ids.split()) | {i for i, pk in run.moved.items() if pk == thread.pk}
        ids = " ".join(sorted(lost))[:MAX_EMPTIED_IDS]
        if thread.emptied_at is None or ids != thread.emptied_ids:
            thread.emptied_at = thread.emptied_at or now
            thread.emptied_ids = ids
            thread.save(update_fields=["emptied_at", "emptied_ids"])
    empty.filter(emptied_at__lt=now - EMPTY_THREAD_TTL).delete()


def select_checked(conn: imap.Connection, folder: MailFolder) -> None:
    """SELECT `folder` for acting on cached UIDs: only if its UIDVALIDITY is the one the cache was read
    under. Anything else (a folder re-created, or never synced) and a cached UID may now name a
    DIFFERENT mail — moving, flagging or opening that would hit the wrong one. Raises StaleFolder."""
    validity = conn.select(folder.name)
    if folder.uidvalidity is None or validity != folder.uidvalidity:
        raise StaleFolder(STALE_FOLDER.format(folder.display_name))


def fetch_raw(mailbox: Mailbox, message: MailMessage, *, mark_seen: bool = False) -> bytes:
    marked = True
    with imap.connect(mailbox) as conn:
        select_checked(conn, message.folder)
        raw = conn.fetch_message(message.uid)
        if mark_seen:
            # Whatever the cache says: someone may have marked it unread at one.com since the last sync.
            # STORE +FLAGS is idempotent and the connection is open and selected anyway.
            try:
                conn.add_flags(message.uid, r"\Seen")
            except imap.MailboxError as exc:  # read-only mailbox, say: the mail can still be shown
                logger.warning("postkasser: could not mark %s/%s read: %s", mailbox.address, message.uid, exc)
                marked = False
    if marked and mark_seen and not message.seen:
        message.seen = True
        message.save(update_fields=["seen"])
    return raw


def sent_folder_name(mailbox: Mailbox) -> str:
    """Where a sent mail is filed: the folder named on the mailbox if the server really has it; otherwise
    (nothing named — "" means automatic — or a name the server no longer has) the folder the server marks
    as Sent; else what was typed (and let APPEND say no)."""
    typed = mailbox.sent_folder
    folders = mailbox.folders.all()
    if typed and _usable_sent(folders.filter(name=typed)).exists():
        return typed
    # Several folders can be marked Sent; the top-level one is the mailbox's own, not a subfolder's.
    server_side = (
        _usable_sent(folders).filter(special_use=MailFolder.SpecialUse.SENT).order_by("depth", "path").first()
    )
    if server_side is not None:
        return server_side.name
    # A name the server has but that cannot take sent mail (drafts, trash...) is never used; one it does
    # not have is tried anyway (and APPEND may say no) — the folders may simply not be synced yet.
    return "" if sent_folder_exists_unsuitably(mailbox) else typed


def _usable_sent(folders: "QuerySet[MailFolder]") -> "QuerySet[MailFolder]":
    """The folders a sent copy can be filed in (the ones MailboxForm offers): selectable, and not drafts,
    trash or junk."""
    return folders.filter(selectable=True).exclude(special_use__in=SKIPPED_USES)


def sent_folder_is_stale(mailbox: Mailbox) -> bool:
    """A name is set on the mailbox, the folders are known, and the server has no such folder (renamed or
    mistyped) or it is no place for sent mail (drafts, trash, junk, unselectable): `sent_folder_name` then
    falls back to the Sent folder the server marks, if any."""
    typed = mailbox.sent_folder
    return (
        bool(typed)
        and mailbox.folders.exists()
        and not _usable_sent(mailbox.folders.filter(name=typed)).exists()
    )


def sent_folder_exists_unsuitably(mailbox: Mailbox) -> bool:
    """The stored name is a folder the server has, but one that cannot take sent mail."""
    typed = mailbox.sent_folder
    return bool(typed) and mailbox.folders.filter(name=typed).exists() and sent_folder_is_stale(mailbox)


def sync_sent_folder(mailbox: Mailbox) -> None:
    """Right after a mail was sent and filed: sync the SENT folder (only that one), so the copy joins its
    sag now and not at the next scheduled sync. Under the claim; if somebody holds it, or the server will
    not answer, nothing happens — the next sync picks the copy up. Never raises."""
    name = sent_folder_name(mailbox)
    folder = (
        mailbox.folders.filter(name=name, selectable=True).exclude(special_use__in=SKIPPED_USES).first()
        if name
        else None
    )
    if folder is None:
        return
    try:
        with claim(mailbox) as claimed:
            if not claimed:
                return
            run = _Run(mailbox, claim=claimed)
            with imap.connect(mailbox) as conn:
                _sync_folder(run, conn, folder, flag_window=SENT_FLAG_WINDOW)
            pending = run.pending()
    except (SyncBusy, imap.MailboxError) as exc:
        logger.info("postkasser: sync of the sent folder of %s skipped: %s", mailbox.address, exc)
        return
    except Exception:
        logger.exception("postkasser: sync of the sent folder of %s failed", mailbox.address)
        return
    pending.send(background=True)


def log(
    mailbox: Mailbox,
    resident: Resident | None,
    action: str,
    *,
    message: MailMessage | None,
    subject: str,
    thread: MailThread | None = None,
    detail: str = "",
) -> None:
    """`resident` is None for what the system did by itself (a customer's reply reopening a sag)."""
    MailEvent.objects.create(
        mailbox=mailbox,
        message=message,
        thread=thread if thread is not None else (message.thread if message is not None else None),
        resident=resident,
        action=action,
        subject=subject[:998],
        detail=detail[:255],
    )


def _set_column(thread: MailThread, column: BoardColumn, resident: Resident | None) -> bool:
    if thread.column_id == column.pk:
        return False
    thread.column = column
    thread.last_activity_at = timezone.now()
    thread.save(update_fields=["column", "updated_at", "last_activity_at"])
    log(
        thread.mailbox,
        resident,
        MailEvent.Action.STATUS,
        message=None,
        subject=thread.subject,
        thread=thread,
        detail=column.name,
    )
    return True


def move_thread(thread: MailThread, column: BoardColumn, resident: Resident) -> bool:
    """Move a sag to `column` (the caller has checked it belongs to the same mailbox). Returns whether
    anything changed; the event's detail is the column's name at this moment."""
    return _set_column(thread, column, resident)


def assign_thread(thread: MailThread, assignee: Resident | None, resident: Resident) -> bool:
    """Hand a sag to `assignee` (the caller has checked they may open the mailbox), or to nobody."""
    if thread.assignee_id == (assignee.pk if assignee else None):
        return False
    thread.assignee = assignee
    thread.last_activity_at = timezone.now()
    thread.save(update_fields=["assignee", "updated_at", "last_activity_at"])
    if assignee is not None:
        try:
            notify.assigned(thread, assignee, by=resident)
        except Exception:
            logger.exception("postkasser: notifying the new assignee of sag %s failed", thread.pk)
    log(
        thread.mailbox,
        resident,
        MailEvent.Action.ASSIGN,
        message=None,
        subject=thread.subject,
        thread=thread,
        detail=assignee.full_name if assignee else "ingen",
    )
    return True


def start_thread(mailbox: Mailbox, sender: Resident, subject: str, message_id: str) -> MailThread:
    """The sag for a mail written in the intranet. It goes straight to the "waiting" column (or Ny if
    the group has none), assigned to whoever wrote — they are the one waiting for the answer. The
    mail's own copy joins it when the sent folder is synced (`pending_message_id`)."""
    column = columns.for_role(mailbox, ColumnRole.AFVENTER) or columns.ny_column(mailbox)
    thread = MailThread.objects.create(
        mailbox=mailbox,
        subject=thread_subject(subject),
        column=column,
        assignee=sender,
        last_message_at=timezone.now(),
        last_activity_at=timezone.now(),
        pending_message_id=message_id,
    )
    log(
        mailbox,
        sender,
        MailEvent.Action.SEND,
        message=None,
        subject=subject,
        thread=thread,
        detail=column.name,
    )
    return thread


def record_reply(
    mailbox: Mailbox, message: MailMessage, replier: Resident, subject: str, message_id: str
) -> MailThread:
    """After a reply went out: the sag moves to the "waiting" column if the group has one (otherwise it
    stays where it is) and goes to the replier if it had nobody. A mail with no sag yet (one of our own
    from the sent folder) gets one now, and takes the mail with it."""
    log(mailbox, replier, MailEvent.Action.REPLY, message=message, subject=message.subject)
    thread = message.thread
    if thread is None:
        thread = start_thread(mailbox, replier, subject or message.subject, message_id)
        message.thread = thread
        message.save(update_fields=["thread"])
        return thread
    waiting = columns.for_role(mailbox, ColumnRole.AFVENTER)
    if waiting is not None:
        _set_column(thread, waiting, replier)
    if thread.assignee_id is None:
        assign_thread(thread, replier, replier)
    return thread


@dataclass
class Outgoing:
    to: list[str]
    cc: list[str]
    subject: str
    body: str
    in_reply_to: str = ""
    references: str = ""


_RE_PREFIX = re.compile(r"^\s*(re|sv|aw)\s*:", re.IGNORECASE)


def reply_subject(subject: str) -> str:
    return subject if _RE_PREFIX.match(subject) else f"Re: {subject}"


def quote(text: str, sender: str, date: str) -> str:
    quoted = "\n".join(f"> {line}" for line in text.strip().splitlines())
    return f"\n\n{date} skrev {sender}:\n{quoted}\n"


def initial_body(mailbox: Mailbox, sent_by: Resident, quoted: str = "") -> str:
    """The compose box's starting text: room to write, then the sign-off (whose hand is on it — a
    shared inbox has many senders behind one address, and the reader, often a former member, wants
    to know who to follow up with), then the signature, then the quoted mail being answered.
    Editable, and sent as it stands.

    The signature gets the `-- ` delimiter only when nothing follows it: clients treat everything
    after that line as signature, and would grey out or drop the quoted thread with it."""
    body = f"\n\nVenlig hilsen\n{sent_by.full_name}, {mailbox.name}"
    if signature := mailbox.signature.strip():
        body += f"\n\n{signature}" if quoted else f"\n\n-- \n{signature}"
    return body + quoted


@dataclass
class Sent:
    message_id: str
    warnings: list[str]
    filed: bool = False  # the copy was appended to the sent folder


def send(mailbox: Mailbox, outgoing: Outgoing, *, sent_by: Resident) -> Sent:
    """Send through the MAILBOX'S OWN one.com login and From address — one.com refuses a From the
    login does not own (see settings.DEFAULT_FROM_EMAIL), so the site's shared SMTP account cannot
    send as ak@. Then file a copy in the mailbox's sent folder, as a real mail client would.

    The backend is settings.POSTKASSER_EMAIL_BACKEND (EMAIL_BACKEND unless overridden), so dev prints to
    the console and tests use locmem; the host/port/login below only matter to the SMTP backend.

    Returns the new mail's Message-ID (the sag it belongs to waits for its copy by that id) and warnings
    for things that went wrong AFTER the mail left (the sent-folder copy) — the send itself raising is
    the caller's error to show.
    """
    body = outgoing.body.rstrip()
    headers = {
        "Message-ID": make_msgid(domain=mailbox.address.rsplit("@", 1)[-1]),
        # Set here so the copy filed in the sent folder carries the same Date as the mail that left.
        "Date": formatdate(localtime=True),
    }
    if outgoing.in_reply_to:
        headers["In-Reply-To"] = outgoing.in_reply_to
        headers["References"] = f"{outgoing.references} {outgoing.in_reply_to}".strip()

    password = imap.decrypted_password(mailbox)
    connection = get_connection(
        settings.POSTKASSER_EMAIL_BACKEND or settings.EMAIL_BACKEND,
        host=mailbox.smtp_host,
        port=mailbox.smtp_port,
        username=mailbox.login,
        password=password,
        use_ssl=mailbox.smtp_port == 465,
        use_tls=mailbox.smtp_port != 465,
        timeout=imap.TIMEOUT_SECONDS,
    )
    mail = EmailMessage(
        subject=outgoing.subject,
        body=body,
        from_email=formataddr((mailbox.name, mailbox.address)),
        to=outgoing.to,
        cc=outgoing.cc,
        headers=headers,
        connection=connection,
    )
    mail.send()

    warnings = []
    filed = False
    if not (folder := sent_folder_name(mailbox)):
        if mailbox.folders.exists():
            warning = (
                "Mailen er sendt, men ingen af postkassens mapper kan bruges som Sendt-mappe — "
                "vælg Sendt-mappen i admin."
            )
            if not outgoing.in_reply_to:
                warning += f" Sagen beholdes i {PENDING_GRACE.days} dage, medmindre kopien dukker op eller nogen svarer."
            warnings.append(warning)
        else:
            warnings.append(
                "Mailen er sendt, men postkassen kender ikke en Sendt-mappe endnu — hent post først."
            )
    else:
        try:
            with imap.connect(mailbox) as conn:
                conn.append(folder, mail.message().as_bytes())
            filed = True
        except imap.MailboxError as exc:
            logger.warning("postkasser: could not file sent mail for %s: %s", mailbox.address, exc)
            warning = f"Mailen er sendt, men kunne ikke gemmes i «{utf7.decode(folder)}»."
            if not outgoing.in_reply_to:  # a new sag was (or will be) started for it
                warning += f" Sagen beholdes i {PENDING_GRACE.days} dage, medmindre kopien dukker op eller nogen svarer."
            warnings.append(warning)
    return Sent(message_id=headers["Message-ID"], warnings=warnings, filed=filed)


def mark_answered(mailbox: Mailbox, message: MailMessage) -> None:
    """Set \\Answered on the server too, so one.com's own webmail shows the reply arrow. Raises StaleFolder
    (and touches nothing) if the folder has changed since the cache was read; other failures are logged."""
    try:
        with imap.connect(mailbox) as conn:
            select_checked(conn, message.folder)
            conn.add_flags(message.uid, r"\Answered")
    except StaleFolder:
        raise
    except imap.MailboxError as exc:
        logger.warning("postkasser: could not flag %s/%s answered: %s", mailbox.address, message.uid, exc)
    message.answered = True
    message.save(update_fields=["answered"])


def mark_unread(mailbox: Mailbox, messages: Iterable[MailMessage], *, by: Resident) -> int:
    """Take \\Seen off `messages` at one.com, then in the cache (only for what the server accepted), and
    log it. Returns how many were marked.

    Deliberately NOT under the claim: one STORE per mail is idempotent and harmless next to a sync (whose
    flag refresh reads the server's state anyway), and taking the claim would fail the button with
    "synkroniseres allerede" during every scheduled sync. A folder whose UIDVALIDITY changed is refused
    (StaleFolder) — a cached UID may now be another mail. Raises MailboxError for anything the server
    refuses — after every folder has been tried; the mails marked stay marked."""
    rows = list(
        MailMessage.objects.select_related("folder", "thread").filter(
            pk__in=[m.pk for m in messages], mailbox=mailbox
        )
    )
    by_folder: dict[int, list[MailMessage]] = {}
    for message in rows:
        by_folder.setdefault(message.folder_id, []).append(message)
    marked = 0
    errors: list[str] = []
    with imap.connect(mailbox) as conn:
        for group in by_folder.values():
            # One folder refusing (a BAD, a stale UIDVALIDITY) leaves the other folders' mails to be marked.
            try:
                select_checked(conn, group[0].folder)
                for message in group:
                    conn.remove_flags(message.uid, r"\Seen")
                    message.seen = False
                    message.save(update_fields=["seen"])
                    log(mailbox, by, MailEvent.Action.UNREAD, message=message, subject=message.subject)
                    marked += 1
            except imap.MailboxError as exc:
                errors.append(str(exc))
    if errors:
        raise imap.MailboxError("; ".join(errors))
    return marked


# --- moving mail between one.com's folders ------------------------------------------------------------


class MoveRefused(Exception):
    """The move cannot be made at all (a bad target, nothing movable); the message is Danish, for the user."""


@dataclass
class MoveOutcome:
    moved: int = 0
    skipped: int = 0  # already in the target, or in the sent folder (never moved)
    errors: list[str] = field(default_factory=list)  # one per source folder that failed
    thread_removed: bool = False  # moved to Papirkurv/Spam and the sag went with it
    lost_claim: bool = False  # another run took the mailbox over mid-move; the rest was not moved
    # What the target sync found worth telling people: the caller sends it (`notices.send`) once the claim
    # is released — move_messages itself does not.
    notices: notify.Pending | None = None


def move_label(folder: MailFolder) -> str:
    """Papirkurv/Spam by what the folder is for, whatever the server calls it; else its own name."""
    if folder.special_use in REMOVING_TARGETS:
        return str(MailFolder.SpecialUse(folder.special_use).label)
    return folder.display_name


def is_movable(message: MailMessage) -> bool:
    """Mail in the sent folder stays there: it is our own copy, and moving it would break the history."""
    return message.folder.special_use != MailFolder.SpecialUse.SENT


def move_targets(mailbox: Mailbox, messages: Iterable[MailMessage]) -> list[MailFolder]:
    """The folders `messages` could be moved to: every selectable folder but sent and drafts, including
    Papirkurv/Spam, minus the one all of them are already in. Each carries `move_label`, `indent`
    (non-breaking spaces by depth) and `removes` (it takes the sag off the board)."""
    movable = [m for m in messages if is_movable(m)]
    if not movable:
        return []
    already = {m.folder_id for m in movable}
    found: list[MailFolder] = [
        f
        for f in mailbox.folders.filter(selectable=True).exclude(special_use__in=UNMOVABLE_TARGETS)
        if len(already) != 1 or f.pk not in already
    ]
    found.sort(key=lambda f: (f.special_use != MailFolder.SpecialUse.INBOX, f.path.lower()))
    for folder in found:
        folder.move_label = move_label(folder)  # type: ignore[attr-defined]
        folder.indent = "  " * folder.depth  # type: ignore[attr-defined]
        folder.removes = folder.special_use in REMOVING_TARGETS  # type: ignore[attr-defined]
    return found


def move_messages(
    mailbox: Mailbox, messages: Iterable[MailMessage], target: MailFolder, *, moved_by: Resident
) -> MoveOutcome:
    """Move `messages` to the folder `target` at one.com and bring the cache along.

    One IMAP move per source folder (a UID only means something inside its folder), all on one
    connection. A group that fails is reported and does not stop the others; the groups that went
    through are already in the cache by then. Raises SyncBusy if a sync or another move holds the
    mailbox, MoveRefused for a target mail cannot go to or nothing to move, MailboxError if the server
    cannot be reached at all. The notifications the target sync found are in `outcome.notices`; the
    caller sends them (in the background) now that the claim is released.

    CONCURRENCY: the whole move runs under the same claim a sync takes (`claim`). A sync in another
    process therefore cannot run between "the mail left the old folder" and "the cache knows where it
    went" and delete the sag as empty — it is refused with SyncBusy instead (and the beat job simply
    tries again in two minutes).

    The cache follows in one of two ways: with a COPYUID answer for THIS state of the target folder
    (same UIDVALIDITY), the rows are updated in place (folder, uid) and keep their sag; otherwise the
    target folder is synced — which re-joins the moved mail to its sag by Message-ID, the source row still
    being there to say which — and the old rows are deleted after. To Papirkurv/Spam, which are never
    synced, the rows are simply deleted, and a sag with nothing but sent mail left goes with them.
    """
    target = MailFolder.objects.get(pk=target.pk, mailbox=mailbox)
    if not target.selectable or target.special_use in UNMOVABLE_TARGETS:
        raise MoveRefused(f"Mails kan ikke flyttes til «{move_label(target)}».")
    with claim(mailbox) as claimed:
        if not claimed:
            raise SyncBusy(SYNC_BUSY)
        # Read under the claim: a sync that finished a moment ago may have pruned some of them.
        rows = list(
            MailMessage.objects.select_related("folder", "thread").filter(
                pk__in=[m.pk for m in messages], mailbox=mailbox
            )
        )
        movable = [m for m in rows if is_movable(m) and m.folder_id != target.pk]
        outcome = MoveOutcome(skipped=len(rows) - len(movable))
        if not movable:
            raise MoveRefused(
                f"Der er ingen mails at flytte: de ligger allerede i «{move_label(target)}», "
                "eller i Sendt, hvorfra mails ikke flyttes."
            )
        groups: dict[int, list[MailMessage]] = {}
        for message in movable:
            groups.setdefault(message.folder_id, []).append(message)

        done: list[tuple[MailMessage, str]] = []  # (message, what became of its row: a _Refiled)
        run = _Run(mailbox, claim=claimed)
        try:
            with imap.connect(mailbox) as conn:
                for group in groups.values():
                    source = group[0].folder
                    try:
                        claimed.renew()
                    except SyncBusy:
                        outcome.lost_claim = True
                        outcome.errors.append(SYNC_LOST + " Resten af mailene blev ikke flyttet.")
                        break
                    try:
                        # The UIDs were cached under some UIDVALIDITY; if the folder has been re-created
                        # since, they may now be other mails — move nothing from it.
                        select_checked(conn, source)
                        result = conn.move([m.uid for m in group], target.name)
                    except StaleFolder as exc:
                        outcome.errors.append(str(exc))
                        continue
                    except imap.MailboxError as exc:
                        logger.warning("postkasser: move %s/%s: %s", mailbox.address, source.name, exc)
                        outcome.errors.append(f"Kunne ikke flytte mails fra «{source.display_name}»: {exc}")
                        continue
                    # With a COPYUID answer, only the UIDs the server says it moved were moved (the rest
                    # had been deleted at one.com meanwhile); without one it cannot be told.
                    actual = [m for m in group if m.uid in result.mapping] if result.mapping else group
                    outcome.moved += len(actual)
                    if not actual:
                        continue
                    try:
                        refiled, warning = _refile(run, conn, actual, target, result)
                    except SyncBusy:
                        # Lost the claim inside the target sync: the mail IS moved, its rows are not
                        # brought along; the sync of whoever took over sorts it out.
                        done.extend((m, _Refiled.PENDING) for m in actual)
                        outcome.lost_claim = True
                        outcome.errors.append(SYNC_LOST + " Resten af mailene blev ikke flyttet.")
                        break
                    done.extend((m, refiled) for m in actual)
                    if warning:
                        outcome.errors.append(warning)
        except imap.MailboxError as exc:
            if not outcome.moved:
                raise
            outcome.errors.append(str(exc))
        outcome.notices = run.pending()
        if done:
            _log_moves(mailbox, moved_by, done, target)
            if target.special_use in REMOVING_TARGETS:
                outcome.thread_removed = _drop_moved_threads(
                    mailbox, {m.thread_id for m, _ in done if m.thread_id}
                )
    return outcome


class _Refiled:
    """What became of a moved mail's cache row."""

    IN_PLACE = "in_place"  # the row now points at the mail's new folder and UID
    RESYNCED = "resynced"  # the target folder was synced: the mail has a NEW row, the old one is gone
    PENDING = "pending"  # the old row is stale and stays until the next sync prunes it
    REMOVED = "removed"  # moved to Papirkurv/Spam: no row


def _refile(
    run: _Run, conn: imap.Connection, group: list[MailMessage], target: MailFolder, result: imap.MoveResult
) -> tuple[str, str]:
    """Bring the cache in line with a move that happened at the server. Returns (a _Refiled, a warning
    for the user or "")."""
    ids = [m.pk for m in group]
    if target.special_use in REMOVING_TARGETS:
        MailMessage.objects.filter(pk__in=ids).delete()
        return _Refiled.REMOVED, ""
    known = target.uidvalidity is not None and result.uidvalidity == target.uidvalidity
    if known and all(m.uid in result.mapping for m in group):
        try:
            with transaction.atomic():
                for message in group:
                    message.folder = target
                    message.uid = result.mapping[message.uid]
                    message.save(update_fields=["folder", "uid"])
        except IntegrityError:  # a stale row already holds one of the new UIDs: let a sync sort it out
            for message in group:  # the in-memory copies were changed before the rollback
                message.refresh_from_db()
        else:
            return _Refiled.IN_PLACE, ""
    try:
        _sync_folder(run, conn, target)
    except imap.MailboxError as exc:
        # The mail is moved at the server. The old rows stay until the next sync prunes them (keeping
        # their sag, see _Run.moved) and finds the mail in its new folder.
        logger.warning("postkasser: sync of %s after a move failed: %s", target.name, exc)
        return (
            _Refiled.PENDING,
            f"Flyttet, men «{target.display_name}» kunne ikke hentes endnu — det sker ved næste synkronisering.",
        )
    with transaction.atomic():
        MailMessage.objects.filter(pk__in=ids).delete()
        _settle_first_syncs(run)
    return _Refiled.RESYNCED, ""


def _moved_row(
    mailbox: Mailbox, message: MailMessage, refiled: str, target: MailFolder
) -> MailMessage | None:
    """The cache row of a moved mail as it is NOW: the same row when it was updated in place, its new
    row in the target folder after a resync (found by Message-ID), None when the old row is stale or gone."""
    if refiled == _Refiled.IN_PLACE:
        return message
    if refiled == _Refiled.RESYNCED and message.message_id:
        return MailMessage.objects.filter(
            mailbox=mailbox, folder=target, message_id=message.message_id
        ).first()
    return None


def _log_moves(
    mailbox: Mailbox, resident: Resident, done: list[tuple[MailMessage, str]], target: MailFolder
) -> None:
    """One MOVE event per sag (per mail that has none), detail = the target's name; `message` is set
    only for a single mail, and then to its row as it is now (see _moved_row)."""
    by_thread: dict[int | None, list[tuple[MailMessage, str]]] = {}
    for message, refiled in done:
        key = message.thread_id or -message.pk  # no sag: one event per mail
        by_thread.setdefault(key, []).append((message, refiled))
    for entries in by_thread.values():
        first, refiled = entries[0]
        single = len(entries) == 1 and len(done) == 1
        log(
            mailbox,
            resident,
            MailEvent.Action.MOVE,
            message=_moved_row(mailbox, first, refiled, target) if single else None,
            subject=first.thread.subject if first.thread is not None else first.subject,
            thread=first.thread,
            detail=move_label(target),
        )


def _drop_moved_threads(mailbox: Mailbox, thread_ids: set[int]) -> bool:
    """After a move to Papirkurv/Spam: the sag goes if nothing but our own sent mail is left in it (that
    stays in Sendt, and a sag of only sent copies is not one the board has any use for). Returns whether
    one went."""
    empty = MailThread.objects.filter(pk__in=thread_ids, mailbox=mailbox).exclude(
        pk__in=MailMessage.objects.exclude(folder__special_use=MailFolder.SpecialUse.SENT)
        .filter(thread__in=thread_ids)
        .values("thread_id")
    )
    count, _ = empty.delete()
    return count > 0
