"""The thin layer over imaplib — connect, list UIDs, fetch headers/flags/whole messages, set flags,
append, move. Everything that talks to one.com goes through `connect`, which tests replace with a fake
(tests/test_postkasser.py), so nothing above this module knows imaplib exists.

Nothing is selected on connect: the caller picks a folder with `Connection.select`, because a
mailbox is several folders (Indbakke, Sendt, and whatever the group has made) and every UID — what
fetch, flags and STORE address — only means something inside one of them.
"""

import imaplib
import re
import ssl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from django.core.exceptions import ImproperlyConfigured

from . import crypto, utf7
from .models import Mailbox

INBOX = "INBOX"
TIMEOUT_SECONDS = 30

_UID_RE = re.compile(rb"UID (\d+)")
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "\s*([^"]+)"')
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
_COPYUID_RE = re.compile(rb"COPYUID (\d+) ([\d:,]+) ([\d:,]+)")
# A COPYUID set longer than this is a misbehaving server, not a mapping worth expanding.
_MAX_UID_SET = 100_000


UNDECRYPTABLE = "Adgangskoden kan ikke dekrypteres — indtast den igen i admin."


class MailboxError(Exception):
    """Anything one.com said no to — bad login, a timeout, a refused command. Views show it as a
    message instead of a 500; the sync stores it on Mailbox.last_sync_error."""


@dataclass
class FetchedItem:
    uid: int
    flags: set[str] = field(default_factory=set)
    size: int = 0
    payload: bytes = b""
    # INTERNALDATE: when the server received the mail (timezone-aware; None if it did not say).
    internaldate: datetime | None = None


@dataclass
class MoveResult:
    """What a move reported (RFC 4315 COPYUID): the target folder's UIDVALIDITY and where each moved UID
    went. Both are empty/None when the server did not say (no UIDPLUS, or it left the code out)."""

    uidvalidity: int | None = None
    mapping: dict[int, int] = field(default_factory=dict)


@dataclass
class FolderInfo:
    """One line of a LIST answer. `name` is as the server wrote it (modified UTF-7, see utf7.py);
    `attributes` are lower-cased (`\\sent`, `\\noselect` ...)."""

    name: str
    delimiter: str
    attributes: frozenset[str]


def expand_uid_set(text: bytes) -> list[int]:
    """`4:6,9` -> [4, 5, 6, 9], in the order the server gave them (a range may run backwards)."""
    uids: list[int] = []
    for part in text.split(b","):
        first, _, last = part.partition(b":")
        a, b = int(first), int(last or first)
        step = 1 if b >= a else -1
        if abs(b - a) + len(uids) >= _MAX_UID_SET:
            raise ValueError("UID set too large")
        uids.extend(range(a, b + step, step))
    return uids


def parse_copyuid(texts: list[bytes]) -> MoveResult:
    """The first well-formed `[COPYUID <uidvalidity> <source set> <target set>]` in the server's answer
    (tagged text and/or untagged response codes). Anything malformed means "the server did not say"."""
    for text in texts:
        match = _COPYUID_RE.search(text or b"")
        if match is None:
            continue
        try:
            source, target = expand_uid_set(match.group(2)), expand_uid_set(match.group(3))
        except ValueError:
            continue
        if len(source) == len(target):
            return MoveResult(uidvalidity=int(match.group(1)), mapping=dict(zip(source, target, strict=True)))
    return MoveResult()


_LIST_RE = re.compile(r'^\((?P<attrs>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$', re.DOTALL)
_QUOTED_RE = re.compile(r"\\(.)")


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return _QUOTED_RE.sub(r"\1", text[1:-1])
    return text


def parse_list(data: list) -> list[FolderInfo]:
    """imaplib's LIST answer: bare bytes for `(\\HasNoChildren) "." "INBOX"`, and a (line, literal)
    tuple when the server sent the name as a `{n}` literal instead of a quoted string or an atom —
    which it must for a name that is not representable as either. Anything unparseable is skipped:
    one odd line must not hide the other folders."""
    folders: list[FolderInfo] = []
    for part in data:
        literal: bytes | None = None
        if isinstance(part, tuple):
            line, literal = part[0], part[1]
        elif isinstance(part, bytes):
            line = part
        else:
            continue
        match = _LIST_RE.match(line.decode("utf-8", errors="replace").strip())
        if match is None:
            continue
        name = (
            literal.decode("utf-8", errors="replace")
            if literal is not None
            else _unquote(match.group("name"))
        )
        delimiter = "" if match.group("delim") == "NIL" else _unquote(match.group("delim"))
        attributes = frozenset(a.lower() for a in match.group("attrs").split())
        if name:
            folders.append(FolderInfo(name=name, delimiter=delimiter, attributes=attributes))
    return folders


class Connection:
    """An authenticated IMAP session. No folder is selected until `select` is called."""

    def __init__(self, client: imaplib.IMAP4) -> None:
        self.client = client
        self._capabilities: frozenset[str] | None = None

    def _ok(self, typ: str, data: list, what: str) -> list:
        if typ != "OK":
            raise MailboxError(f"{what}: {data!r}")
        return data

    def _call(self, what: str, command: Callable[..., tuple[str, list]], *args: object) -> list:
        """One imaplib call whose answer must be OK. imaplib raises `IMAP4.error` for a BAD answer (and
        `abort` when the connection is gone) instead of returning it; that is a refusal like a NO, and
        must reach the caller as a MailboxError it can handle per folder, not tear the connection down."""
        try:
            return self._ok(*command(*args), what)
        except imaplib.IMAP4.error as exc:
            raise MailboxError(f"{what}: {exc}") from exc

    def select(self, folder: str) -> int | None:
        """Make `folder` (a server-side name, i.e. modified UTF-7) the one the other calls act on.
        Returns its UIDVALIDITY."""
        try:
            self._call("SELECT", self.client.select, _quote(folder))
        except UnicodeError as exc:  # imaplib sends ASCII; see append()
            raise MailboxError(f"Mappenavnet «{folder}» skal være ASCII.") from exc
        _typ, data = self.client.response("UIDVALIDITY")
        return int(data[0]) if data and data[0] else None

    def all_uids(self) -> list[int]:
        data = self._call("SEARCH", self.client.uid, "SEARCH", "ALL")
        return [int(u) for u in (data[0] or b"").split()]

    def fetch(self, uids: list[int], spec: str) -> list[FetchedItem]:
        if not uids:
            return []
        data = self._call("FETCH", self.client.uid, "FETCH", ",".join(map(str, uids)), spec)
        return parse_fetch(data)

    def fetch_message(self, uid: int) -> bytes:
        """The whole RFC 822 message. BODY.PEEK so opening it here does not mark it read on its own —
        services.open_message sets \\Seen explicitly, after it is sure the open succeeded."""
        items = [i for i in self.fetch([uid], "(UID BODY.PEEK[])") if i.uid == uid]
        if not items or not items[0].payload:
            raise MailboxError("Mailen findes ikke længere på serveren.")
        return items[0].payload

    def fetch_flags(self, min_uid: int) -> list[FetchedItem]:
        """Flags for every message from `min_uid` up — one short range, however many are cached. The
        caller filters to the UIDs it cares about (`N:*` always includes the last message)."""
        data = self._call("FETCH", self.client.uid, "FETCH", f"{min_uid}:*", "(UID FLAGS)")
        return parse_fetch(data)

    def add_flags(self, uid: int, *flags: str) -> None:
        self._call("STORE", self.client.uid, "STORE", str(uid), "+FLAGS", f"({' '.join(flags)})")

    def remove_flags(self, uid: int, *flags: str) -> None:
        self._call("STORE", self.client.uid, "STORE", str(uid), "-FLAGS", f"({' '.join(flags)})")

    def append(self, folder: str, message: bytes) -> None:
        when = imaplib.Time2Internaldate(time.time())
        try:
            self._call("APPEND", self.client.append, _quote(folder), r"(\Seen)", when, message)
        except UnicodeError as exc:
            # imaplib sends the folder name as ASCII; a non-ASCII name would need modified UTF-7.
            # MailboxForm.clean_sent_folder refuses one, so this only guards rows saved some other
            # way — and it must not escape as a 500 AFTER the mail has already gone out.
            raise MailboxError(f"Mappenavnet «{folder}» skal være ASCII.") from exc

    def capabilities(self) -> frozenset[str]:
        """The server's CAPABILITY list, asked for once per connection (after login: what a server offers
        can differ from what it said in its greeting)."""
        if self._capabilities is None:
            data = self._call("CAPABILITY", self.client.capability)
            self._capabilities = frozenset(
                word.upper() for chunk in data if isinstance(chunk, bytes) for word in chunk.decode().split()
            )
        return self._capabilities

    def _uid_command(self, command: str, *args: str) -> tuple[list[bytes], MoveResult]:
        """`UID <command> <args>` through imaplib's own command machinery, because `Connection.uid` throws
        away the tagged answer, and that is where COPYUID is. Returns every text that may hold the code."""
        client = self.client
        try:
            typ, data = client._simple_command("UID", command, *args)
        except imaplib.IMAP4.error as exc:  # a BAD answer: refused, but a refusal like any other
            raise MailboxError(f"{command}: {exc}") from exc
        texts = [d for d in data if isinstance(d, bytes)]
        # imaplib files a bracketed response code under its name, without the name: `[COPYUID 1 4 9]`
        # arrives as untagged_responses["COPYUID"] == [b"1 4 9"].
        texts += [
            b"COPYUID " + d for d in client.untagged_responses.pop("COPYUID", []) if isinstance(d, bytes)
        ]
        if typ != "OK":
            raise MailboxError(f"{command}: {data!r}")
        return texts, parse_copyuid(texts)

    def move(self, uids: list[int], target: str) -> MoveResult:
        """Move `uids` of the SELECTED folder to `target` (a server-side name). Only ever acts on exactly
        those UIDs:
          * MOVE (RFC 6851) if the server has it;
          * else UIDPLUS: COPY, flag \\Deleted, and `UID EXPUNGE` of those UIDs alone;
          * else refuse. A plain EXPUNGE would also remove whatever somebody else has flagged \\Deleted
            in that folder, so there is no fallback to it.
        """
        if not uids:
            return MoveResult()
        capabilities = self.capabilities()
        if "MOVE" not in capabilities and "UIDPLUS" not in capabilities:
            raise MailboxError("Serveren understøtter ikke sikker flytning af mails.")
        wanted = ",".join(str(uid) for uid in sorted(set(uids)))
        try:
            quoted = _quote(target)
            quoted.encode("ascii")
            if "MOVE" in capabilities:
                return self._uid_command("MOVE", wanted, quoted)[1]
            result = self._uid_command("COPY", wanted, quoted)[1]
            try:
                self._uid_command("STORE", wanted, "+FLAGS.SILENT", r"(\Deleted)")
                self._uid_command("EXPUNGE", wanted)
            except MailboxError as exc:
                raise MailboxError(
                    f"Mailen er kopieret til «{utf7.decode(target)}», men kunne ikke fjernes fra den gamle mappe: {exc}"
                ) from exc
            return result
        except UnicodeError as exc:
            raise MailboxError(f"Mappenavnet «{target}» skal være ASCII.") from exc

    def list_folders(self) -> list[FolderInfo]:
        return parse_list(self._call("LIST", self.client.list, '""', '"*"'))


def parse_fetch(data: list) -> list[FetchedItem]:
    """imaplib's FETCH response is a list mixing (meta, literal) tuples — for anything that carries a
    body or header literal — with bare bytes for flags-only lines. What follows a literal on the same
    response line (` FLAGS (\\Seen))`, ` UID 8 FLAGS (...))`, or just `)`) arrives as its own bytes
    item starting with a space or `)`; it belongs to the tuple before it. Pull UID/FLAGS/SIZE out of
    the joined text."""
    items: list[FetchedItem] = []
    index = 0
    while index < len(data):
        part = data[index]
        index += 1
        if isinstance(part, tuple):
            meta, payload = part[0], part[1]
            if index < len(data) and _is_continuation(data[index]):
                meta += data[index]
                index += 1
        elif isinstance(part, bytes) and not _is_continuation(part):
            meta, payload = part, b""
        else:
            continue
        uid = _UID_RE.search(meta)
        if uid is None:
            continue
        flags = _FLAGS_RE.search(meta)
        size = _SIZE_RE.search(meta)
        items.append(
            FetchedItem(
                uid=int(uid.group(1)),
                flags=set(flags.group(1).decode().split()) if flags else set(),
                size=int(size.group(1)) if size else 0,
                payload=payload,
                internaldate=parse_internaldate(meta),
            )
        )
    return items


def parse_internaldate(meta: bytes) -> datetime | None:
    """`INTERNALDATE "17-Nov-2026 10:00:00 +0100"` out of a FETCH line, as an aware datetime; None if the
    line has none or it is malformed."""
    match = _INTERNALDATE_RE.search(meta)
    if match is None:
        return None
    # imaplib.Internaldate2tuple rather than strptime("%b"): month names in INTERNALDATE are always
    # English, and strptime would read them in whatever LC_TIME the process happens to run under.
    # Fed the raw `INTERNALDATE "…"` token: its pattern expects the space-padded day (" 7-Nov-…").
    parsed = imaplib.Internaldate2tuple(match.group(0))
    if parsed is None:
        return None
    return datetime.fromtimestamp(time.mktime(parsed), tz=UTC)


def _is_continuation(part: object) -> bool:
    return isinstance(part, bytes) and part[:1] in {b" ", b")"}


def decrypted_password(mailbox: Mailbox) -> str:
    try:
        return crypto.decrypt(mailbox.password_encrypted)
    except (ImproperlyConfigured, ValueError) as exc:
        raise MailboxError(UNDECRYPTABLE) from exc


@contextmanager
def connect(mailbox: Mailbox) -> Iterator[Connection]:
    password = decrypted_password(mailbox)
    try:
        client = imaplib.IMAP4_SSL(
            mailbox.imap_host,
            mailbox.imap_port,
            ssl_context=ssl.create_default_context(),
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, imaplib.IMAP4.error) as exc:
        raise MailboxError(f"Kunne ikke forbinde til {mailbox.imap_host}: {exc}") from exc
    try:
        try:
            client.login(mailbox.login, password)
        except imaplib.IMAP4.error as exc:
            raise MailboxError("one.com afviste login — er adgangskoden ændret?") from exc
        yield Connection(client)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise MailboxError(str(exc)) from exc
    finally:
        try:
            client.logout()
        except (OSError, imaplib.IMAP4.error):
            pass
