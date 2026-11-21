"""Postkasser — the groups' shared one.com mailboxes, read and answered inside the intranet, as a board of
sager (mail threads) with columns each group names itself.

Nothing here talks to one.com: `postkasser.imap.connect` is replaced by a fake that serves in-memory
folders (LIST, SELECT, UIDs, flags), and sending goes through Django's locmem backend. What is tested is
the feature: who may open which mailbox, how folders and the header cache follow the server, how mail
is threaded into sager and moved between columns, that a hostile HTML mail is neutralised, that opening
a mail marks it read and logs it, and that a reply is threaded and filed.
"""

import email.policy
import imaplib
import itertools
import re
import socketserver
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime

import pytest
from django.conf import LazySettings
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.db.models import RestrictedError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import Room, Workgroup
from notifications.models import Notification
from postkasser import columns, crypto, imap, notify, render, rules, services, tags, utf7
from postkasser.forms import AUTOMATIC_SENT, MailboxForm, RuleForm, TimeRuleForm
from postkasser.models import (
    BoardColumn,
    ColumnRole,
    Mailbox,
    MailEvent,
    MailFolder,
    MailMessage,
    MailRule,
    MailTag,
    MailThread,
    MailTimeRule,
)
from residents.models import Residency, Resident, Role, RoleAssignment, active_period

pytestmark = pytest.mark.django_db

MakeResident = Callable[..., Resident]

_ids = itertools.count(1)
_RECENT_DATE = format_datetime(timezone.now())


def raw_mail(
    subject: str = "Hej",
    sender: str = "Mette <mette@example.com>",
    body: str = "Hvornår åbner kælderen?",
    html: str | None = None,
    attachment: tuple[str, bytes] | None = None,
    message_id: str | None = None,
    in_reply_to: str = "",
    references: str = "",
    date: str | None = None,
    cc: str = "",
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "ak@gahk.dk"
    if cc:
        msg["Cc"] = cc
    # Recent by default (fixed per run, so mails sharing it stay identical): old mail is not "new" to the
    # rules and the notifications (services.RECENT_SLACK).
    msg["Date"] = date or _RECENT_DATE
    # Unique by default: sager are built from Message-IDs, so two test mails sharing one would be one sag.
    msg["Message-ID"] = message_id or f"<m{next(_ids)}@example.com>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    if attachment is not None:
        msg.add_attachment(attachment[1], maintype="text", subtype="html", filename=attachment[0])
    return msg.as_bytes()


def days_ago(days: int) -> str:
    """An RFC 5322 Date header `days` before now."""
    return format_datetime(timezone.now() - timedelta(days=days))


@dataclass
class FolderData:
    uidvalidity: int = 1
    messages: dict[int, bytes] = field(default_factory=dict)
    flags: dict[int, set[str]] = field(default_factory=dict)
    attributes: tuple[str, ...] = ()
    delimiter: str = "."


class FakeServer:
    """A mailbox as the fake connection sees it: folders (named as the server writes them, i.e. in
    modified UTF-7), each with UIDs, messages and flags. INBOX and a Sent folder exist by default;
    `messages`, `flags` and `uidvalidity` are the INBOX's, for the many tests that need only that."""

    def __init__(self) -> None:
        self.folders: dict[str, FolderData] = {
            "INBOX": FolderData(),
            "Sent": FolderData(attributes=(r"\Sent",)),
        }
        self.added: list[tuple[str, int, tuple[str, ...]]] = []  # (folder, uid, flags) of every STORE
        self.removed: list[tuple[str, int, tuple[str, ...]]] = []  # (folder, uid, flags) of every -FLAGS
        self.flag_error: imap.MailboxError | None = None
        self.appended: list[tuple[str, bytes]] = []
        self.selected: list[str] = []  # every SELECT, in order
        self.error: imap.MailboxError | None = None
        self.append_error: imap.MailboxError | None = None
        self.broken: set[str] = set()  # folders whose SELECT is refused
        self.list_data: list | None = None  # raw imaplib LIST data to serve instead of the folders
        self.flag_ranges: list[int] = []
        # INTERNALDATE per (folder, uid), when a test sets one; else the mail's own Date header (a server
        # that received it when it was sent), and "none" (a server that gives no INTERNALDATE) with
        # `no_internaldate`.
        self.internaldates: dict[tuple[str, int], datetime] = {}
        self.no_internaldate = False
        # Moving (FakeConnection.move): does the server answer with COPYUID, which source folders
        # refuse, what was moved, and something to run in the middle of a move (concurrency tests).
        self.copyuid = True
        self.move_error: imap.MailboxError | None = None
        self.refuse_moves_from: set[str] = set()
        self.moves: list[tuple[str, list[int], str]] = []
        self.during_move: Callable[[], None] | None = None

    @property
    def uidvalidity(self) -> int:
        return self.folders["INBOX"].uidvalidity

    @uidvalidity.setter
    def uidvalidity(self, value: int) -> None:
        self.folders["INBOX"].uidvalidity = value

    @property
    def messages(self) -> dict[int, bytes]:
        return self.folders["INBOX"].messages

    @messages.setter
    def messages(self, value: dict[int, bytes]) -> None:
        self.folders["INBOX"].messages = value

    @property
    def flags(self) -> dict[int, set[str]]:
        return self.folders["INBOX"].flags

    @flags.setter
    def flags(self, value: dict[int, set[str]]) -> None:
        self.folders["INBOX"].flags = value

    def add_folder(self, name: str, *attributes: str, delimiter: str = ".") -> FolderData:
        self.folders[name] = FolderData(attributes=attributes, delimiter=delimiter)
        return self.folders[name]

    def put(self, uid: int, raw: bytes, flags: set[str] | None = None, folder: str = "INBOX") -> None:
        self.folders[folder].messages[uid] = raw
        self.folders[folder].flags[uid] = flags or set()

    def take(self, uid: int, folder: str = "INBOX") -> tuple[bytes, set[str]]:
        data = self.folders[folder]
        return data.messages.pop(uid), data.flags.pop(uid)

    def list_lines(self) -> list[bytes]:
        if self.list_data is not None:
            return self.list_data
        return [
            f'({" ".join((r"\HasNoChildren", *f.attributes))}) "{f.delimiter}" "{name}"'.encode()
            for name, f in self.folders.items()
        ]


class FakeConnection:
    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.folder: str | None = None

    @property
    def data(self) -> FolderData:
        assert self.folder is not None, "no folder SELECTed before a UID command"
        return self.server.folders[self.folder]

    def select(self, folder: str) -> int | None:
        if folder in self.server.broken:
            raise imap.MailboxError(f"SELECT: NO {folder}")
        if folder not in self.server.folders:
            raise imap.MailboxError(f"SELECT: NO [NONEXISTENT] {folder}")
        self.folder = folder
        self.server.selected.append(folder)
        return self.data.uidvalidity

    def list_folders(self) -> list[imap.FolderInfo]:
        return imap.parse_list(self.server.list_lines())

    def all_uids(self) -> list[int]:
        return sorted(self.data.messages)

    def fetch(self, uids: list[int], spec: str) -> list[imap.FetchedItem]:
        headers = "HEADER" in spec
        items = []
        for uid in uids:
            raw = self.data.messages[uid]
            payload = raw.split(b"\n\n", 1)[0] + b"\n\n" if headers else b""
            items.append(
                imap.FetchedItem(
                    uid=uid,
                    flags=set(self.data.flags[uid]),
                    size=len(raw),
                    payload=payload,
                    internaldate=None if self.server.no_internaldate else self.internaldate(uid, raw),
                )
            )
        return items

    def internaldate(self, uid: int, raw: bytes) -> datetime | None:
        assert self.folder is not None
        if (self.folder, uid) in self.server.internaldates:
            return self.server.internaldates[self.folder, uid]
        date = BytesParser(policy=email.policy.default).parsebytes(raw, headersonly=True)["Date"]
        return date.datetime if date is not None else None

    def fetch_flags(self, min_uid: int) -> list[imap.FetchedItem]:
        self.server.flag_ranges.append(min_uid)
        return [
            imap.FetchedItem(uid=uid, flags=set(flags))
            for uid, flags in self.data.flags.items()
            if uid >= min_uid
        ]

    def fetch_message(self, uid: int) -> bytes:
        if uid not in self.data.messages:
            raise imap.MailboxError("Mailen findes ikke længere på serveren.")
        return self.data.messages[uid]

    def add_flags(self, uid: int, *flags: str) -> None:
        assert self.folder is not None
        self.server.added.append((self.folder, uid, flags))
        self.data.flags[uid] |= set(flags)

    def remove_flags(self, uid: int, *flags: str) -> None:
        assert self.folder is not None
        if self.server.flag_error is not None:
            raise self.server.flag_error
        self.server.removed.append((self.folder, uid, flags))
        self.data.flags[uid] -= set(flags)

    def move(self, uids: list[int], target: str) -> imap.MoveResult:
        server = self.server
        if server.move_error is not None:
            raise server.move_error
        assert self.folder is not None
        if self.folder in server.refuse_moves_from:
            raise imap.MailboxError(f"UID MOVE: NO cannot move from {self.folder}")
        if server.during_move is not None:
            server.during_move()
        destination = server.folders[target]
        mapping = {}
        for uid in sorted(uids):
            if uid not in self.data.messages:  # deleted at the server meanwhile: a real MOVE skips it
                continue
            raw, flags = self.server.take(uid, self.folder)
            new = max(destination.messages, default=0) + 1
            destination.messages[new], destination.flags[new] = raw, flags
            mapping[uid] = new
        server.moves.append((self.folder, sorted(m for m in uids if m in mapping), target))
        if server.copyuid:
            return imap.MoveResult(uidvalidity=destination.uidvalidity, mapping=mapping)
        return imap.MoveResult()

    def append(self, folder: str, message: bytes) -> None:
        if self.server.append_error is not None:
            raise self.server.append_error
        self.server.appended.append((folder, message))
        if folder in self.server.folders:  # a real server files it, and the next sync sees it
            data = self.server.folders[folder]
            uid = max(data.messages, default=0) + 1
            data.messages[uid] = message
            data.flags[uid] = {r"\Seen"}


@pytest.fixture(autouse=True)
def encryption_key(settings: LazySettings) -> None:
    """DEBUG is off under pytest, so crypto needs a real key (production behaviour, on purpose)."""
    settings.MAILBOX_ENCRYPTION_KEY = "u3Uc-qAi9iiCv3fkfWRRjd6_lsDpJhE6mQiH-_iDGYg="


@pytest.fixture(autouse=True)
def no_real_imap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that forgets the `server` fixture must fail, not log in to the real imap.one.com with a
    test password."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("test tried to open a real IMAP connection — use the `server` fixture")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", refuse)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer()

    @contextmanager
    def connect(mailbox: Mailbox) -> Iterator[FakeConnection]:
        if fake.error is not None:
            raise fake.error
        yield FakeConnection(fake)

    monkeypatch.setattr(imap, "connect", connect)
    return fake


def make_mailbox(
    slug: str = "ak",
    roles: list[str] | None = None,
    *,
    name: str = "AK-gruppen",
    signature: str = "",
    is_active: bool = True,
    sent_folder: str = "Sent",
) -> Mailbox:
    # The default board columns come with the mailbox (models: post_save).
    return Mailbox.objects.create(
        name=name,
        slug=slug,
        address=f"{slug}@gahk.dk",
        password_encrypted=crypto.encrypt("hemmeligt"),
        roles=roles or [],
        signature=signature,
        is_active=is_active,
        sent_folder=sent_folder,
    )


def make_message(mailbox: Mailbox, uid: int = 1, *, folder: str = "INBOX", **fields: object) -> MailMessage:
    """A cached header row without going through a sync."""
    mail_folder, _ = MailFolder.objects.get_or_create(
        mailbox=mailbox,
        name=folder,
        defaults={
            "display_name": folder,
            "path": folder,
            "special_use": MailFolder.SpecialUse.INBOX if folder == "INBOX" else "",
        },
    )
    return MailMessage.objects.create(mailbox=mailbox, folder=mail_folder, uid=uid, **fields)


def col(mailbox: Mailbox, role: str) -> BoardColumn:
    column = columns.for_role(mailbox, role)
    assert column is not None
    return column


def join_workgroup(resident: Resident, workgroup: Workgroup) -> None:
    year, month = active_period()
    room = Room.objects.create(
        legacy_index=700 + resident.pk, number=700 + resident.pk, floor="1", side="mod gaden"
    )
    Residency.objects.create(resident=resident, room=room, workgroup=workgroup, year=year, month=month)


@pytest.fixture
def ak_member(make_resident: MakeResident, client: Client) -> Resident:
    member = make_resident("ak@intern.dk", roles=(Role.AK,))
    client.force_login(member)
    return member


def synced(server: FakeServer, mailbox: Mailbox, *raws: bytes) -> list[MailMessage]:
    for uid, raw in enumerate(raws, start=1):
        server.put(uid, raw)
    services.sync(mailbox)
    return list(mailbox.messages.order_by("uid"))


def board(client: Client, slug: str = "ak", **params: str) -> str:
    response = client.get(reverse("postkasser:board", args=[slug]), params)
    assert response.status_code == 200
    return response.content.decode()


def subjects_in(client: Client, column: BoardColumn, mailbox: Mailbox, **params: str) -> list[str]:
    """The sager the board shows in `column` (read from the view's context, not scraped from HTML)."""
    response = client.get(reverse("postkasser:board", args=[mailbox.slug]), params)
    for shown, cards in response.context["columns"]:
        if shown.pk == column.pk:
            return [c.subject for c in cards]
    raise AssertionError("column not on the board")


# --- access ---------------------------------------------------------------------------------------


def test_a_role_opens_the_mailbox(client: Client, ak_member: Resident) -> None:
    make_mailbox(roles=["ak"])
    assert client.get(reverse("postkasser:board", args=["ak"])).status_code == 200


def test_a_workgroup_member_opens_the_mailbox(client: Client, make_resident: MakeResident) -> None:
    group = Workgroup.objects.create(name="Festgruppen")
    mailbox = make_mailbox("fest")
    mailbox.workgroups.add(group)
    member = make_resident("fest@intern.dk")
    join_workgroup(member, group)
    client.force_login(member)
    assert client.get(reverse("postkasser:board", args=["fest"])).status_code == 200


def test_someone_outside_the_group_gets_404(client: Client, make_resident: MakeResident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    other = Workgroup.objects.create(name="Andre")
    mailbox.workgroups.add(Workgroup.objects.create(name="Festgruppen"))
    outsider = make_resident("ude@intern.dk")
    join_workgroup(outsider, other)
    client.force_login(outsider)
    message = make_message(mailbox)
    assert client.get(reverse("postkasser:board", args=["ak"])).status_code == 404
    assert client.get(reverse("postkasser:message", args=["ak", message.pk])).status_code == 404


def test_an_inactive_mailbox_is_closed(client: Client, ak_member: Resident) -> None:
    make_mailbox(roles=["ak"], is_active=False)
    assert client.get(reverse("postkasser:board", args=["ak"])).status_code == 404


def test_a_message_of_another_mailbox_is_404(client: Client, ak_member: Resident) -> None:
    make_mailbox(roles=["ak"])
    secret = make_mailbox("fest", name="Festgruppen")
    message = make_message(secret)
    assert client.get(reverse("postkasser:message", args=["ak", message.pk])).status_code == 404


def test_index_redirects_when_there_is_exactly_one_mailbox(client: Client, ak_member: Resident) -> None:
    make_mailbox(roles=["ak"])
    response = client.get(reverse("postkasser:index"))
    assert response.status_code == 302
    assert response["Location"] == reverse("postkasser:board", args=["ak"])


def test_index_lists_several_mailboxes_with_unread_counts(client: Client, ak_member: Resident) -> None:
    make_mailbox(roles=["ak"])
    other = make_mailbox("fest", name="Festgruppen", roles=["ak"])
    make_message(other, seen=False)
    response = client.get(reverse("postkasser:index"))
    assert response.status_code == 200
    assert "Festgruppen" in response.content.decode()
    assert "1 ulæst" in response.content.decode()


def test_the_sidebar_entry_shows_only_with_access(client: Client, make_resident: MakeResident) -> None:
    make_mailbox(roles=["ak"])
    plain = make_resident("plain@intern.dk")
    client.force_login(plain)
    assert reverse("postkasser:index") not in client.get(reverse("dashboard")).content.decode()
    member = make_resident("ak@intern.dk", roles=(Role.AK,))
    client.force_login(member)
    assert reverse("postkasser:index") in client.get(reverse("dashboard")).content.decode()


# --- sync -----------------------------------------------------------------------------------------


def test_sync_caches_new_headers(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.put(1, raw_mail("Første", attachment=("a.html", b"<b>x</b>")), {r"\Seen"})
    server.put(2, raw_mail("Anden", sender="ole@example.com"))
    assert services.sync(mailbox) == 2
    first, second = mailbox.messages.order_by("uid")
    assert (first.subject, first.from_name, first.from_address) == ("Første", "Mette", "mette@example.com")
    assert first.seen and first.has_attachments and first.date is not None
    assert not second.seen and not second.has_attachments and second.sender == "ole@example.com"
    mailbox.refresh_from_db()
    assert mailbox.last_synced_at is not None
    assert set(mailbox.folders.values_list("name", "uidvalidity")) == {("INBOX", 1), ("Sent", 1)}


def test_sync_removes_mail_deleted_on_the_server(server: FakeServer) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox, raw_mail("Bliver"), raw_mail("Forsvinder"))
    del server.messages[2]
    services.sync(mailbox)
    assert list(mailbox.messages.values_list("subject", flat=True)) == ["Bliver"]


def test_sync_refreshes_flags(server: FakeServer) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox, raw_mail())
    server.flags[1] = {r"\Seen", r"\Answered"}
    assert services.sync(mailbox) == 0
    message = mailbox.messages.get()
    assert message.seen and message.answered


def test_a_changed_uidvalidity_wipes_the_cache(server: FakeServer) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox, raw_mail("Gammel"))
    old_pk = mailbox.messages.get().pk
    server.uidvalidity = 2
    server.messages = {1: raw_mail("Ny, samme UID")}
    server.flags = {1: set()}
    services.sync(mailbox)
    message = mailbox.messages.get()
    assert message.subject == "Ny, samme UID" and message.pk != old_pk
    assert mailbox.folders.get(name="INBOX").uidvalidity == 2


def test_a_mailbox_error_is_stored(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.error = imap.MailboxError("one.com afviste login")
    with pytest.raises(imap.MailboxError):
        services.sync(mailbox)
    mailbox.refresh_from_db()
    assert "afviste login" in mailbox.last_sync_error
    server.error = None
    services.sync(mailbox)
    mailbox.refresh_from_db()
    assert mailbox.last_sync_error == ""


def test_the_refresh_button_syncs_and_reports_errors(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    make_mailbox(roles=["ak"])
    server.put(1, raw_mail())
    response = client.post(reverse("postkasser:refresh", args=["ak"]), follow=True)
    assert "1 ny mail" in response.content.decode()
    server.error = imap.MailboxError("nede")
    response = client.post(reverse("postkasser:refresh", args=["ak"]), follow=True)
    assert "Kunne ikke hente post" in response.content.decode()
    assert client.get(reverse("postkasser:refresh", args=["ak"])).status_code == 405


def test_the_board_lists_sager_and_shows_sync_state(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    synced(server, mailbox, raw_mail("Tilmelding"), raw_mail("Regning", sender="bogholder@example.com"))
    page = board(client)
    assert "Tilmelding" in page and "Regning" in page and "Sidst hentet" in page
    assert "Ulæst" in page
    mailbox.last_sync_error = "Login afvist"
    mailbox.save(update_fields=["last_sync_error"])
    assert "Login afvist" in board(client)


# --- rendering ------------------------------------------------------------------------------------


def test_scripts_and_handlers_are_stripped() -> None:
    html = render.clean_html(
        '<p onclick="x()">Hi<script>alert(1)</script><img src="x" onerror="alert(1)"></p>'
    )
    assert "<script" not in html and "alert" not in html and "onerror" not in html and "onclick" not in html
    assert "Hi" in html


def test_cid_images_are_dropped_and_remote_ones_kept() -> None:
    html = render.clean_html('<img src="cid:logo@x"><img src="https://t.example/p.gif">')
    assert "cid:" not in html and "https://t.example/p.gif" in html


def test_javascript_links_are_dropped() -> None:
    assert "javascript" not in render.clean_html('<a href="javascript:alert(1)">x</a>')


def test_the_frame_blocks_remote_images_until_asked() -> None:
    blocked = render.frame_document("<p>x</p>", allow_images=False)
    allowed = render.frame_document("<p>x</p>", allow_images=True)
    assert "img-src data:" in blocked and "https:" not in blocked
    assert "default-src 'none'" in blocked and "default-src 'none'" in allowed
    assert "img-src data: https: http:" in allowed


def test_a_text_mail_is_escaped() -> None:
    assert "&lt;script&gt;" in render.text_as_html("<script>alert(1)</script>")
    rendered = render.render(raw_mail(body="<b>fed</b>"))
    assert rendered.html is None and rendered.text.strip() == "<b>fed</b>"


def test_the_message_page_escapes_the_frame_into_srcdoc(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    html = '<p>Hej</p><img src="https://t.example/p.gif"><script>alert(1)</script>'
    (message,) = synced(server, mailbox, raw_mail(html=html))
    url = reverse("postkasser:message", args=["ak", message.pk])
    page = client.get(url).content.decode()
    assert 'srcdoc="' in page and "<script>alert" not in page
    assert "Vis billeder" in page and "img-src data:" in page and "https: http:" not in page
    shown = client.get(url, {"billeder": "1"}).content.decode()
    assert "Vis billeder" not in shown and "img-src data: https: http:" in shown


# --- reading --------------------------------------------------------------------------------------


def test_opening_a_message_marks_it_seen_and_logs_it(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    (message,) = synced(server, mailbox, raw_mail("Spørgsmål"))
    response = client.get(reverse("postkasser:message", args=["ak", message.pk]))
    assert response.status_code == 200 and "Spørgsmål" in response.content.decode()
    assert server.added == [("INBOX", 1, (r"\Seen",))]
    message.refresh_from_db()
    assert message.seen
    event = MailEvent.objects.get()
    assert (event.action, event.resident, event.message) == (MailEvent.Action.READ, ak_member, message)


def test_opening_stores_seen_even_when_the_cache_is_stale(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    """Cache says seen, the server says unseen (marked unread in one.com since the last sync): opening it
    here must still set \\Seen at the server."""
    mailbox = make_mailbox(roles=["ak"])
    (message,) = synced(
        server,
        mailbox,
        raw_mail("Spørgsmål"),
    )
    MailMessage.objects.filter(pk=message.pk).update(seen=True)
    assert r"\Seen" not in server.flags[1]
    client.get(reverse("postkasser:message", args=["ak", message.pk]))
    assert server.added == [("INBOX", 1, (r"\Seen",))]
    assert r"\Seen" in server.flags[1]
    assert MailMessage.objects.get(pk=message.pk).seen


def test_a_server_failure_on_open_is_a_message_not_a_500(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    (message,) = synced(server, mailbox, raw_mail())
    del server.messages[1]
    response = client.get(reverse("postkasser:message", args=["ak", message.pk]), follow=True)
    assert "Kunne ikke hente mailen" in response.content.decode()


def test_attachments_are_always_downloads(client: Client, ak_member: Resident, server: FakeServer) -> None:
    mailbox = make_mailbox(roles=["ak"])
    (message,) = synced(server, mailbox, raw_mail(attachment=("evil.html", b"<script>alert(1)</script>")))
    page = client.get(reverse("postkasser:message", args=["ak", message.pk])).content.decode()
    assert "evil.html" in page
    response = client.get(reverse("postkasser:attachment", args=["ak", message.pk, 0]))
    assert response["Content-Type"] == "application/octet-stream"
    assert response["Content-Disposition"].startswith("attachment")
    assert b"alert(1)" in response.content
    assert client.get(reverse("postkasser:attachment", args=["ak", message.pk, 5])).status_code == 404


# --- sending --------------------------------------------------------------------------------------


def test_compose_sends_from_the_mailbox_and_files_a_copy(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    make_mailbox(roles=["ak"], signature="AK-gruppen")
    response = client.post(
        reverse("postkasser:compose", args=["ak"]),
        {"to": "a@example.com, b@example.com", "cc": "", "subject": "Nyt", "body": "Hej alle"},
    )
    assert response.status_code == 302
    (sent,) = mail.outbox
    assert sent.from_email == "AK-gruppen <ak@gahk.dk>"
    assert sent.to == ["a@example.com", "b@example.com"]
    assert sent.body == "Hej alle"  # sent as written: greeting and signature live in the form
    (folder, raw) = server.appended[0]
    assert folder == "Sent" and b"Subject: Nyt" in raw
    assert MailEvent.objects.get().action == MailEvent.Action.SEND


def test_compose_validates_addresses(client: Client, ak_member: Resident, server: FakeServer) -> None:
    make_mailbox(roles=["ak"])
    response = client.post(
        reverse("postkasser:compose", args=["ak"]), {"to": "ikke-en-adresse", "subject": "x", "body": "y"}
    )
    assert response.status_code == 200 and "ikke en gyldig mailadresse" in response.content.decode()
    assert not mail.outbox


def test_a_reply_is_threaded_and_marks_answered(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    (message,) = synced(server, mailbox, raw_mail("Spørgsmål", message_id="<orig@example.com>"))
    url = reverse("postkasser:reply", args=["ak", message.pk])
    form = client.get(url).content.decode()
    assert "Re: Spørgsmål" in form and "mette@example.com" in form and "&gt; Hvornår åbner kælderen?" in form
    response = client.post(
        url, {"to": "mette@example.com", "cc": "", "subject": "Re: Spørgsmål", "body": "Kl. 20"}
    )
    assert response.status_code == 302
    (sent,) = mail.outbox
    assert sent.extra_headers["In-Reply-To"] == "<orig@example.com>"
    assert sent.extra_headers["References"] == "<orig@example.com>"
    assert ("INBOX", message.uid, (r"\Answered",)) in server.added
    message.refresh_from_db()
    assert message.answered
    assert MailEvent.objects.get(action=MailEvent.Action.REPLY).message == message
    page = client.get(reverse("postkasser:message", args=["ak", message.pk])).content.decode()
    assert "Besvaret af" in page and ak_member.full_name in page


def test_reply_all_keeps_the_others_but_not_the_mailbox_itself(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    raw = EmailMessage()
    raw["From"], raw["To"], raw["Cc"] = "mette@example.com", "ak@gahk.dk, ole@example.com", "per@example.com"
    raw["Subject"] = "Gruppemail"
    raw.set_content("x")
    (message,) = synced(server, mailbox, raw.as_bytes())
    url = reverse("postkasser:reply", args=["ak", message.pk])
    page = client.get(url, {"alle": "1"}).content.decode()
    assert (
        "ole@example.com, per@example.com" in page
        and "ak@gahk.dk" not in page.split('name="cc"')[1].split(">")[0]
    )
    assert 'name="alle" value="1"' in page


def test_a_failed_sent_copy_is_a_warning_but_still_a_success(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    make_mailbox(roles=["ak"])
    server.append_error = imap.MailboxError("quota")
    response = client.post(
        reverse("postkasser:compose", args=["ak"]),
        {"to": "a@example.com", "subject": "Nyt", "body": "Hej"},
        follow=True,
    )
    page = response.content.decode()
    assert len(mail.outbox) == 1
    assert "Mailen er sendt" in page and "kunne ikke gemmes" in page


def test_a_smtp_failure_is_shown_on_the_form(
    client: Client, ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_mailbox(roles=["ak"])

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(services.EmailMessage, "send", boom)
    response = client.post(
        reverse("postkasser:compose", args=["ak"]), {"to": "a@example.com", "subject": "Nyt", "body": "Hej"}
    )
    assert response.status_code == 200 and "Mailen blev ikke sendt" in response.content.decode()
    assert not MailEvent.objects.exists()


# --- crypto and admin -----------------------------------------------------------------------------


def test_crypto_round_trip() -> None:
    token = crypto.encrypt("hemmeligt")
    assert token != "hemmeligt" and crypto.decrypt(token) == "hemmeligt"


def test_a_token_under_another_key_is_a_loud_error(settings: LazySettings) -> None:
    token = crypto.encrypt("hemmeligt")
    settings.MAILBOX_ENCRYPTION_KEY = "Qm9ndXNLZXlCb2d1c0tleUJvZ3VzS2V5Qm9ndXNLZXk="
    with pytest.raises(ImproperlyConfigured):
        crypto.decrypt(token)


def test_a_missing_key_is_an_error_outside_debug(settings: LazySettings) -> None:
    settings.MAILBOX_ENCRYPTION_KEY = ""
    with pytest.raises(ImproperlyConfigured):
        crypto.encrypt("x")


def test_the_admin_form_keeps_the_old_password_when_left_blank() -> None:
    mailbox = make_mailbox()
    old = mailbox.password_encrypted
    data = {
        "name": "AK-gruppen", "slug": "ak", "address": "ak@gahk.dk", "username": "", "password": "",
        "imap_host": "imap.one.com", "imap_port": 993, "smtp_host": "send.one.com", "smtp_port": 465,
        "sent_folder": "Sent", "signature": "", "roles": "[]", "is_active": "on",
    }  # fmt: skip
    form = MailboxForm(data, instance=mailbox)
    assert form.is_valid(), form.errors
    form.save()
    mailbox.refresh_from_db()
    assert mailbox.password_encrypted == old
    form = MailboxForm({**data, "password": "nyt"}, instance=mailbox)
    assert form.is_valid(), form.errors
    form.save()
    mailbox.refresh_from_db()
    assert crypto.decrypt(mailbox.password_encrypted) == "nyt"


def test_a_new_mailbox_needs_a_password() -> None:
    form = MailboxForm(
        {
            "name": "Ny",
            "slug": "ny",
            "address": "ny@gahk.dk",
            "password": "",
            "imap_host": "imap.one.com",
            "imap_port": 993,
            "smtp_host": "send.one.com",
            "smtp_port": 465,
            "sent_folder": "",
            "roles": "[]",
            "is_active": "on",
        }
    )
    assert not form.is_valid() and "password" in form.errors


def test_the_password_is_never_rendered_in_the_admin(client: Client, make_resident: MakeResident) -> None:
    mailbox = make_mailbox()
    admin = make_resident("adm@intern.dk", roles=(Role.ADMINISTRATOR,))
    Resident.objects.filter(pk=admin.pk).update(is_superuser=True)
    client.force_login(admin)
    page = client.get(reverse("admin:postkasser_mailbox_change", args=[mailbox.pk])).content.decode()
    assert mailbox.password_encrypted not in page and "hemmeligt" not in page


def test_the_sync_command_survives_a_failing_mailbox(
    server: FakeServer, capsys: pytest.CaptureFixture[str]
) -> None:
    from django.core.management import call_command

    make_mailbox()
    server.error = imap.MailboxError("nede")
    call_command("sync_mailboxes")
    assert "FEJL nede" in capsys.readouterr().err


def test_parse_fetch_reads_imaplib_shapes() -> None:
    items = imap.parse_fetch(
        [
            (b"1 (UID 7 FLAGS (\\Seen \\Answered) RFC822.SIZE 99 BODY[HEADER] {5}", b"a: b\n"),
            b")",
            b"2 (UID 8 FLAGS ())",
        ]
    )
    assert [(i.uid, i.size) for i in items] == [(7, 99), (8, 0)]
    assert items[0].flags == {r"\Seen", r"\Answered"} and items[1].flags == set()
    assert BytesParser(policy=email.policy.default).parsebytes(items[0].payload)["a"] == "b"


# --- hostile input, limits and failures -----------------------------------------------------------

HOSTILE = (
    b"From: =?utf-8?q?Eve=0D=0A?= <e@x.dk>\nSubject: Hej\nDate: Mon, 02 Nov 2026 10:00:00 +0100\n\nbody\n"
)


def test_a_hostile_header_does_not_crash_parsing() -> None:
    fields = render.header_fields(HOSTILE)
    assert "\r" not in str(fields["from_name"]) and "\n" not in str(fields["from_name"])
    rendered = render.render(HOSTILE)
    assert "\r" not in rendered.from_header and "\n" not in rendered.from_header
    assert rendered.subject == "Hej"


def test_a_hostile_header_still_syncs_and_opens(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.put(1, HOSTILE)
    server.put(2, raw_mail("Næste"))
    assert services.sync(mailbox) == 2
    hostile = mailbox.messages.get(uid=1)
    assert client.get(reverse("postkasser:message", args=["ak", hostile.pk])).status_code == 200
    assert client.get(reverse("postkasser:reply", args=["ak", hostile.pk])).status_code == 200


def test_an_unforeseen_crash_in_header_parsing_still_caches_a_row(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = make_mailbox()

    def boom(_raw: bytes) -> dict[str, object]:
        raise RuntimeError("parser bug")

    monkeypatch.setattr(render, "header_fields", boom)
    server.put(1, raw_mail())
    assert services.sync(mailbox) == 1
    assert mailbox.messages.get().uid == 1


def test_the_sync_command_survives_an_unexpected_crash(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from django.core.management import call_command

    first, second = make_mailbox(), make_mailbox("fest", name="Fest")
    server.put(1, raw_mail())
    real_sync = services.sync

    def flaky(mailbox: Mailbox, **kwargs: object) -> int:
        if mailbox.pk == first.pk:
            raise RuntimeError("kaboom")
        return real_sync(mailbox, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(services, "sync", flaky)
    call_command("sync_mailboxes")
    first.refresh_from_db()
    assert "kaboom" in first.last_sync_error and "FEJL" in capsys.readouterr().err
    assert second.messages.count() == 1


def test_the_initial_sync_limit_holds_on_later_runs(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "INITIAL_SYNC_LIMIT", 2)
    mailbox = make_mailbox()
    for uid in range(1, 6):
        server.put(uid, raw_mail(f"Mail {uid}"))
    assert services.sync(mailbox) == 2
    assert services.sync(mailbox) == 0
    assert sorted(mailbox.messages.values_list("uid", flat=True)) == [4, 5]
    server.put(6, raw_mail("Mail 6"))
    assert services.sync(mailbox) == 1
    assert sorted(mailbox.messages.values_list("uid", flat=True)) == [4, 5, 6]


def test_flags_are_refreshed_with_one_range_not_a_uid_list(server: FakeServer) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox, raw_mail("a"), raw_mail("b"), raw_mail("c"))
    server.flags[2] = {r"\Seen"}
    services.sync(mailbox)
    assert server.flag_ranges == [1]
    assert mailbox.messages.get(uid=2).seen


def test_parse_fetch_joins_the_continuation_after_a_literal() -> None:
    items = imap.parse_fetch(
        [
            (b"1 (UID 7 BODY[HEADER] {5}", b"a: b\n"),
            b" FLAGS (\\Seen \\Answered))",
            (b"2 (BODY[HEADER] {5}", b"c: d\n"),
            b" UID 8 FLAGS (\\Seen) RFC822.SIZE 12)",
            (b"3 (UID 9 BODY[HEADER] {5}", b"e: f\n"),
            b")",
        ]
    )
    assert [i.uid for i in items] == [7, 8, 9]
    assert items[0].flags == {r"\Seen", r"\Answered"} and items[1].flags == {r"\Seen"} and items[1].size == 12
    assert items[2].flags == set() and items[2].payload == b"e: f\n"


def test_fetch_message_picks_the_item_for_its_uid() -> None:
    class Client:
        def uid(self, *_args: object) -> tuple[str, list]:
            return "OK", [(b"1 (UID 3 BODY[] {2}", b"zz"), b")", (b"2 (UID 4 BODY[] {2}", b"ok"), b")"]

    conn = imap.Connection(Client())  # type: ignore[arg-type]
    assert conn.fetch_message(4) == b"ok"


def test_a_password_that_cannot_be_decrypted_is_a_mailbox_error(
    client: Client, ak_member: Resident, settings: LazySettings
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    settings.MAILBOX_ENCRYPTION_KEY = "Qm9ndXNLZXlCb2d1c0tleUJvZ3VzS2V5Qm9ndXNLZXk="
    with pytest.raises(imap.MailboxError, match="dekrypteres"):
        with imap.connect(mailbox):
            pass
    response = client.post(
        reverse("postkasser:compose", args=["ak"]), {"to": "a@example.com", "subject": "Nyt", "body": "Hej"}
    )
    assert response.status_code == 200 and "kan ikke dekrypteres" in response.content.decode()
    assert not mail.outbox
    settings.MAILBOX_ENCRYPTION_KEY = "ikke-en-nøgle"  # malformed, not just another key
    with pytest.raises(imap.MailboxError):
        with imap.connect(mailbox):
            pass


ADMIN_DATA = {
    "name": "AK-gruppen", "slug": "ak", "address": "ak@gahk.dk", "username": "", "password": "",
    "imap_host": "imap.one.com", "imap_port": 993, "smtp_host": "send.one.com", "smtp_port": 465,
    "sent_folder": "Sent", "signature": "", "roles": "[]", "is_active": "on",
}  # fmt: skip


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("imap_host", "evil.example"),
        ("smtp_host", "evil.example"),
        ("imap_port", 143),
        ("smtp_port", 25),
        ("username", "other"),
        ("address", "other@gahk.dk"),
    ],
)
def test_changing_where_the_password_goes_requires_retyping_it(field: str, value: object) -> None:
    pk = make_mailbox().pk

    def fresh() -> Mailbox:  # a validated ModelForm writes into its instance, so never reuse one
        return Mailbox.objects.get(pk=pk)

    form = MailboxForm({**ADMIN_DATA, field: value}, instance=fresh())
    assert not form.is_valid() and "password" in form.errors
    assert MailboxForm({**ADMIN_DATA, field: value, "password": "nyt"}, instance=fresh()).is_valid()
    assert MailboxForm({**ADMIN_DATA, "signature": "Ny"}, instance=fresh()).is_valid()


def test_html_only_mail_is_quoted_with_line_breaks() -> None:
    html = "<html><head><title>Titel</title></head><body><p>Første</p><p>Anden<br>linje &amp; mere</p></body></html>"
    text = render.html_to_text(html)
    assert text == "Første\nAnden\nlinje & mere"
    assert "Titel" not in render.clean_html(html) and "Første" in render.clean_html(html)
    raw = EmailMessage()
    raw["Subject"], raw["From"] = "x", "a@example.com"
    raw.set_content(html, subtype="html")
    assert render.render(raw.as_bytes()).text == "Første\nAnden\nlinje & mere"


def test_greeting_and_signature_precede_the_quote_in_the_form(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"], signature="Mvh AK")
    page = client.get(reverse("postkasser:compose", args=["ak"])).content.decode()
    assert "Venlig hilsen" in page and ak_member.full_name in page and "Mvh AK" in page
    (message,) = synced(server, mailbox, raw_mail("Spørgsmål"))
    assert "-- \nMvh AK" in page  # nothing follows a new mail's signature, so it gets the delimiter
    body = client.get(reverse("postkasser:reply", args=["ak", message.pk])).context["form"].initial["body"]
    assert body.index("Venlig hilsen") < body.index("Mvh AK") < body.index("> Hvornår")
    # No `-- ` above a quote: clients hide everything after that line as signature, thread included.
    assert "-- \n" not in body


def test_a_forwarded_mail_attachment_downloads_with_content(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    outer = EmailMessage()
    outer["Subject"], outer["From"], outer["To"] = "Fwd", "a@example.com", "ak@gahk.dk"
    outer.set_content("se vedhæftet")
    inner = EmailMessage()
    inner["Subject"] = "Indlejret"
    inner.set_content("indhold")
    outer.add_attachment(inner, filename="indlejret.eml")
    (message,) = synced(server, mailbox, outer.as_bytes())
    (attachment,) = render.render(outer.as_bytes()).attachments
    assert attachment.size > 0
    response = client.get(reverse("postkasser:attachment", args=["ak", message.pk, 0]))
    assert b"Subject: Indlejret" in response.content


def test_newlines_in_a_subject_are_flattened_and_bad_headers_are_a_form_error(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    make_mailbox(roles=["ak"])
    client.post(
        reverse("postkasser:compose", args=["ak"]),
        {"to": "a@example.com", "subject": "Hej\r\nBcc: x@example.com", "body": "Hej"},
    )
    (sent,) = mail.outbox
    assert sent.subject == "Hej Bcc: x@example.com" and "\n" not in sent.subject


def test_message_id_and_references_from_a_mail_have_no_newlines() -> None:
    raw = b"From: a@example.com\nMessage-ID: <a@x>\nReferences: =?utf-8?q?<r@x>=0D=0ABcc:_x?=\n\nhi\n"
    rendered = render.render(raw)
    assert "\n" not in rendered.references and "\r" not in rendered.references


def test_a_header_injection_attempt_is_a_form_error(
    client: Client, ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from django.core.mail import BadHeaderError

    make_mailbox(roles=["ak"])

    def boom(*_args: object, **_kwargs: object) -> None:
        raise BadHeaderError("no")

    monkeypatch.setattr(services.EmailMessage, "send", boom)
    response = client.post(
        reverse("postkasser:compose", args=["ak"]), {"to": "a@example.com", "subject": "x", "body": "y"}
    )
    assert response.status_code == 200 and "Mailen blev ikke sendt" in response.content.decode()


@pytest.mark.parametrize(
    "html",
    [
        '<img src="//t.example/p.gif">',
        '<img src="/django-admin/x.gif">',
        '<a href="/residents/">x</a>',
        '<a href="//evil.example/">x</a>',
        '<a href="data:text/html,hi">x</a>',
        '<img src="cid:logo">',
    ],
)
def test_relative_and_protocol_relative_urls_are_dropped(html: str) -> None:
    cleaned = render.clean_html(html)
    assert "href=" not in cleaned and "src=" not in cleaned


def test_absolute_links_and_data_images_survive() -> None:
    cleaned = render.clean_html(
        '<a href="https://ok.example/">a</a><a href="mailto:a@b.dk">m</a><img src="data:image/png;base64,AAAA">'
    )
    assert 'href="https://ok.example/"' in cleaned and 'href="mailto:a@b.dk"' in cleaned
    assert 'src="data:image/png;base64,AAAA"' in cleaned


def test_remote_images_are_detected_case_insensitively() -> None:
    raw = raw_mail(html='<p>x</p><img src="HTTPS://T.example/p.gif">')
    assert render.render(raw).has_remote_images
    assert not render.render(
        raw_mail(html='<p>x</p><img src="data:image/png;base64,AAAA">')
    ).has_remote_images


def test_the_sent_folder_name_is_quoted_for_append() -> None:
    calls: list[str] = []

    class Client:
        def append(self, folder: str, *_args: object) -> tuple[str, list]:
            calls.append(folder)
            return "OK", []

    imap.Connection(Client()).append('Sent "Mail"\\x', b"m")  # type: ignore[arg-type]
    assert calls == ['"Sent \\"Mail\\"\\\\x"']


def test_the_sent_copy_carries_the_same_date(client: Client, ak_member: Resident, server: FakeServer) -> None:
    make_mailbox(roles=["ak"])
    client.post(
        reverse("postkasser:compose", args=["ak"]), {"to": "a@example.com", "subject": "x", "body": "y"}
    )
    (sent,) = mail.outbox
    (_folder, raw) = server.appended[0]
    assert sent.extra_headers["Date"].encode() in raw


def test_reply_all_dedupes_case_insensitively_and_skips_the_recipient(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    raw = EmailMessage()
    raw["From"] = "mette@example.com"
    raw["To"] = "AK@gahk.dk, Ole@example.com, ole@EXAMPLE.com, Mette@Example.com"
    raw["Cc"] = "ole@example.com, per@example.com"
    raw["Subject"] = "Gruppemail"
    raw.set_content("x")
    (message,) = synced(server, mailbox, raw.as_bytes())
    form = client.get(reverse("postkasser:reply", args=["ak", message.pk]), {"alle": "1"}).context["form"]
    assert form.initial["to"] == "mette@example.com"
    assert form.initial["cc"] == "Ole@example.com, per@example.com"


# --- access, more ---------------------------------------------------------------------------------


def test_a_workgroup_membership_only_in_a_previous_month_is_denied(
    client: Client, make_resident: MakeResident
) -> None:
    group = Workgroup.objects.create(name="Festgruppen")
    make_mailbox("fest").workgroups.add(group)
    former = make_resident("former@intern.dk")
    current = make_resident("current@intern.dk")
    join_workgroup(current, Workgroup.objects.create(name="Andre"))  # makes this month the active list
    year, month = active_period()
    previous = (year, month - 1) if month > 1 else (year - 1, 12)
    room = Room.objects.create(legacy_index=900, number=900, floor="1", side="mod gaden")
    Residency.objects.create(resident=former, room=room, workgroup=group, year=previous[0], month=previous[1])
    client.force_login(former)
    assert client.get(reverse("postkasser:board", args=["fest"])).status_code == 404


def test_an_administrator_opens_a_role_gated_mailbox(client: Client, make_resident: MakeResident) -> None:
    make_mailbox(roles=["ak"])
    admin = make_resident("adm2@intern.dk", roles=(Role.ADMINISTRATOR,))
    client.force_login(admin)
    assert client.get(reverse("postkasser:board", args=["ak"])).status_code == 200


def test_the_wrong_slug_with_the_right_pk_is_404(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    make_mailbox("fest", name="Festgruppen", roles=["ak"])
    (message,) = synced(server, mailbox, raw_mail(attachment=("a.html", b"x")))
    assert client.get(reverse("postkasser:attachment", args=["fest", message.pk, 0])).status_code == 404
    assert client.get(reverse("postkasser:reply", args=["fest", message.pk])).status_code == 404


def test_the_srcdoc_attribute_is_escaped(client: Client, ak_member: Resident, server: FakeServer) -> None:
    mailbox = make_mailbox(roles=["ak"])
    (message,) = synced(server, mailbox, raw_mail(html="<p>Hej</p>"))
    page = client.get(reverse("postkasser:message", args=["ak", message.pk])).content.decode()
    assert 'srcdoc="&lt;!doctype html&gt;' in page and "<p>Hej" not in page


def test_a_non_ascii_sent_folder_is_refused_by_the_admin_form() -> None:
    form = MailboxForm({**ADMIN_DATA, "sent_folder": "Udgående", "password": "x"})
    assert not form.is_valid() and "sent_folder" in form.errors


def test_a_non_ascii_sent_folder_is_a_mailbox_error_not_a_crash() -> None:
    class Client:
        def append(self, *args: object) -> tuple[str, list]:
            "Udgående".encode("ascii")  # what imaplib does with the folder name
            return "OK", []

    conn = imap.Connection(Client())  # type: ignore[arg-type]
    with pytest.raises(imap.MailboxError):
        conn.append("Udgående", b"x")


def test_an_unsolicited_fetch_for_another_uid_does_not_create_a_row(server: FakeServer) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.put(1, raw_mail("Ægte"))
    real_fetch = FakeConnection.fetch

    def fetch_plus_stray(self: FakeConnection, uids: list[int], spec: str) -> list[imap.FetchedItem]:
        return [*real_fetch(self, uids, spec), imap.FetchedItem(uid=999, flags=set(), payload=b"")]

    FakeConnection.fetch = fetch_plus_stray  # type: ignore[method-assign]
    try:
        services.sync(mailbox)
    finally:
        FakeConnection.fetch = real_fetch  # type: ignore[method-assign]
    assert list(mailbox.messages.values_list("subject", flat=True)) == ["Ægte"]


# === folders, sager and the board (the second version of the feature) =================================
#
# Everything above is the original inbox, kept working on top of folders. What follows covers what the
# board added: modified UTF-7 and LIST, mirroring the server's folders, threading mail into sager,
# moving a sag between columns (ours: nothing changes at one.com), per-group column layouts, and the
# permissions around all of it.


def make_thread(
    mailbox: Mailbox,
    subject: str = "Sag",
    *,
    role: str = ColumnRole.NY,
    column: BoardColumn | None = None,
    assignee: Resident | None = None,
    age_days: int = 1,
    sender: str = "mette@example.com",
    folder: str = "INBOX",
    seen: bool = True,
) -> MailThread:
    """A sag with one cached mail, without a sync."""
    when = timezone.now() - timedelta(days=age_days)
    thread = MailThread.objects.create(
        mailbox=mailbox,
        subject=subject,
        column=column or col(mailbox, role),
        assignee=assignee,
        last_message_at=when,
    )
    uid = next(_ids) + 1000
    make_message(
        mailbox,
        uid,
        folder=folder,
        thread=thread,
        subject=subject,
        from_address=sender,
        from_name=sender.split("@", maxsplit=1)[0].title(),
        date=when,
        seen=seen,
        message_id=f"<t{uid}@example.com>",
    )
    return thread


# --- modified UTF-7 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["Ølkælder", "Udgående", "Å", "A & B", "&", "a&-b", "Sent", "日本語 mix", "😀"]
)
def test_modified_utf7_round_trips(name: str) -> None:
    assert utf7.decode(utf7.encode(name)) == name


def test_modified_utf7_matches_rfc_examples() -> None:
    assert utf7.encode("Ølkælder") == "&ANg-lk&AOY-lder"
    assert utf7.encode("Udgående") == "Udg&AOU-ende"
    assert utf7.encode("A & B") == "A &- B"
    assert utf7.decode("&AOY-") == "æ" and utf7.decode("&-") == "&"
    assert utf7.encode("Ølkælder").isascii()


def test_a_mangled_utf7_run_is_left_as_it_came() -> None:
    assert utf7.decode("Mappe &!!!-") == "Mappe &!!!-"


# --- LIST parsing ---------------------------------------------------------------------------------


def test_list_parsing_reads_quoted_atom_literal_and_attributes() -> None:
    folders = imap.parse_list(
        [
            b'(\\HasNoChildren) "." "INBOX"',
            b'(\\HasNoChildren \\Sent) "." Sent',  # an atom, not a quoted string
            (b'(\\HasNoChildren) "." {12}', b"Udg&AOU-ende"),  # a literal, as imaplib hands it over
            b"",  # the empty continuation imaplib leaves after a literal
            b'(\\Noselect \\HasChildren) "/" "Delt"',
            b'(\\HasNoChildren) NIL "Flad"',
            b"not a list line",
            b'(\\HasNoChildren) "." "Med \\"citat\\""',
        ]
    )
    assert [f.name for f in folders] == ["INBOX", "Sent", "Udg&AOU-ende", "Delt", "Flad", 'Med "citat"']
    by_name = {f.name: f for f in folders}
    assert by_name["Sent"].attributes == {r"\hasnochildren", r"\sent"}
    assert r"\noselect" in by_name["Delt"].attributes and by_name["Delt"].delimiter == "/"
    assert by_name["Flad"].delimiter == ""  # NIL: a flat namespace


@pytest.mark.parametrize(
    ("name", "attributes", "expected"),
    [
        ("INBOX", (), "inbox"),
        ("inbox", (), "inbox"),
        ("Whatever", (r"\sent",), "sent"),
        ("Whatever", (r"\drafts",), "drafts"),
        ("Whatever", (r"\trash",), "trash"),
        ("Whatever", (r"\junk",), "junk"),
        ("Whatever", (r"\archive",), "archive"),
        ("Sendte", (), "sent"),
        ("Sent Items", (), "sent"),
        ("Papirkurv", (), "trash"),
        ("Deleted Items", (), "trash"),
        ("Uønsket", (), "junk"),
        ("Spam", (), "junk"),
        ("Kladder", (), "drafts"),
        ("Festudvalg", (), ""),
    ],
)
def test_special_use_from_attributes_and_name(name: str, attributes: tuple[str, ...], expected: str) -> None:
    info = imap.FolderInfo(name=name, delimiter=".", attributes=frozenset(attributes))
    assert services.special_use(info, name) == expected


# --- folders ----------------------------------------------------------------------------------------


def test_sync_mirrors_the_servers_folders(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.add_folder("INBOX.Fest")
    server.add_folder(utf7.encode("Ølkælder"), delimiter="/")
    server.add_folder("Delt", r"\Noselect")
    services.sync(mailbox)
    folders = {f.name: f for f in mailbox.folders.all()}
    assert set(folders) == {"INBOX", "Sent", "INBOX.Fest", "&ANg-lk&AOY-lder", "Delt"}
    assert (folders["INBOX"].display_name, folders["INBOX"].special_use) == ("Indbakke", "inbox")
    assert folders["Sent"].special_use == "sent"
    nested = folders["INBOX.Fest"]
    assert (nested.display_name, nested.path, nested.depth, nested.delimiter) == (
        "Fest",
        "INBOX/Fest",
        1,
        ".",
    )
    danish = folders["&ANg-lk&AOY-lder"]
    assert (danish.display_name, danish.depth) == ("Ølkælder", 0)
    assert not folders["Delt"].selectable
    assert "Delt" not in server.selected  # \Noselect is never SELECTed
    assert "&ANg-lk&AOY-lder" in server.selected  # and the server is addressed in ITS spelling


def test_a_folder_gone_from_the_server_is_removed_with_its_cache(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.add_folder("Fest")
    server.put(1, raw_mail("I mappen"), folder="Fest")
    services.sync(mailbox)
    assert mailbox.messages.filter(folder__name="Fest").count() == 1
    del server.folders["Fest"]
    services.sync(mailbox)
    assert not mailbox.folders.filter(name="Fest").exists()
    assert not mailbox.messages.exists()


def test_drafts_trash_and_junk_are_not_synced(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.add_folder("Drafts", r"\Drafts")
    server.add_folder("Slettet")  # no attribute: recognised by name
    server.add_folder("Spam")
    for name in ("Drafts", "Slettet", "Spam"):
        server.put(1, raw_mail(f"I {name}"), folder=name)
    server.put(1, raw_mail("I indbakken"))
    services.sync(mailbox)
    assert set(mailbox.folders.values_list("name", flat=True)) >= {"Drafts", "Slettet", "Spam"}
    assert not {"Drafts", "Slettet", "Spam"} & set(server.selected)
    assert list(mailbox.messages.values_list("subject", flat=True)) == ["I indbakken"]


def test_a_folder_that_turns_into_trash_loses_its_cache(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.add_folder("Gamle")
    server.put(1, raw_mail("Gammel"), folder="Gamle")
    services.sync(mailbox)
    assert mailbox.threads.count() == 1
    server.folders["Gamle"].attributes = (r"\Trash",)
    services.sync(mailbox)
    assert not mailbox.messages.exists() and not mailbox.threads.exists()


def test_one_failing_folder_does_not_stop_the_others(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.add_folder("Daarlig")
    server.add_folder("Fest")
    server.broken.add("Daarlig")
    server.put(1, raw_mail("Indbakke-mail"))
    server.put(1, raw_mail("Fest-mail"), folder="Fest")
    assert services.sync(mailbox) == 2
    assert sorted(mailbox.messages.values_list("subject", flat=True)) == ["Fest-mail", "Indbakke-mail"]
    mailbox.refresh_from_db()
    assert "Daarlig" in mailbox.last_sync_error and mailbox.last_synced_at is not None
    server.broken.clear()
    services.sync(mailbox)
    mailbox.refresh_from_db()
    assert mailbox.last_sync_error == ""


def test_the_refresh_button_warns_about_a_failed_folder(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    make_mailbox(roles=["ak"])
    server.add_folder("Daarlig")
    server.broken.add("Daarlig")
    page = client.post(reverse("postkasser:refresh", args=["ak"]), follow=True).content.decode()
    assert "Nogle mapper kunne ikke hentes" in page and "Daarlig" in page


def test_the_initial_limit_is_deeper_for_the_inbox_than_for_other_folders(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "INITIAL_SYNC_LIMIT", 3)
    monkeypatch.setattr(services, "OTHER_FOLDER_SYNC_LIMIT", 2)
    mailbox = make_mailbox()
    server.add_folder("Fest")
    for uid in range(1, 6):
        server.put(uid, raw_mail(f"I{uid}"))
        server.put(uid, raw_mail(f"F{uid}"), folder="Fest")
    services.sync(mailbox)
    assert sorted(mailbox.messages.filter(folder__name="INBOX").values_list("uid", flat=True)) == [3, 4, 5]
    assert sorted(mailbox.messages.filter(folder__name="Fest").values_list("uid", flat=True)) == [4, 5]


def test_the_same_uid_in_two_folders_are_two_messages(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.add_folder("Fest")
    server.put(1, raw_mail("I indbakken"))
    server.put(1, raw_mail("I festmappen"), folder="Fest")
    services.sync(mailbox)
    assert mailbox.messages.count() == 2


def test_uidvalidity_is_per_folder(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.add_folder("Fest")
    server.put(1, raw_mail("Indbakke"))
    server.put(1, raw_mail("Fest"), folder="Fest")
    services.sync(mailbox)
    inbox_row = mailbox.messages.get(folder__name="INBOX")
    server.folders["Fest"].uidvalidity = 9
    services.sync(mailbox)
    assert mailbox.messages.get(folder__name="INBOX").pk == inbox_row.pk  # untouched
    assert mailbox.folders.get(name="Fest").uidvalidity == 9


# --- message operations act in the message's own folder -----------------------------------------------


def test_opening_replying_and_downloading_select_the_messages_folder(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.add_folder("Fest")
    server.put(1, raw_mail("Indbakke-mail", body="fra indbakken"))
    server.put(1, raw_mail("Fest-mail", body="fra festmappen", attachment=("a.html", b"x")), folder="Fest")
    services.sync(mailbox)
    fest = mailbox.messages.get(folder__name="Fest")
    page = client.get(reverse("postkasser:message", args=["ak", fest.pk])).content.decode()
    assert "Fest-mail" in page
    assert server.added == [("Fest", 1, (r"\Seen",))]
    assert client.get(reverse("postkasser:attachment", args=["ak", fest.pk, 0])).content == b"x"
    client.post(
        reverse("postkasser:reply", args=["ak", fest.pk]),
        {"to": "mette@example.com", "subject": "Re: Fest-mail", "body": "Svar"},
    )
    assert ("Fest", 1, (r"\Answered",)) in server.added
    assert all(folder == "Fest" for folder, _uid, _flags in server.added)


# --- threading --------------------------------------------------------------------------------------


def test_thread_subject_drops_reply_and_forward_prefixes() -> None:
    assert services.thread_subject("Re: Sv: VS: Fwd: Tilmelding") == "Tilmelding"
    assert services.thread_subject("AW: FW: Re[2]: Hej") == "Hej"
    assert services.thread_subject("Re:") == "Re:"  # nothing left: keep what there was


def test_a_new_mail_starts_a_sag_in_the_ny_column(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.put(1, raw_mail("Re: Spørgsmål om kælderen"))
    services.sync(mailbox)
    sag = mailbox.threads.get()
    assert sag.subject == "Spørgsmål om kælderen" and sag.column == col(mailbox, ColumnRole.NY)
    assert sag.last_message_at is not None and sag.messages.count() == 1


def test_a_reply_joins_the_sag_by_in_reply_to(server: FakeServer) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox, raw_mail("Spørgsmål", message_id="<a@x>"))
    server.put(2, raw_mail("Re: Spørgsmål", message_id="<b@x>", in_reply_to="<a@x>"))
    services.sync(mailbox)
    sag = mailbox.threads.get()
    assert sag.messages.count() == 2


def test_a_reply_joins_the_sag_by_references(server: FakeServer) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox, raw_mail("Spørgsmål", message_id="<a@x>"))
    # The immediate parent is unknown (never delivered here); only the head of the chain is.
    server.put(
        2, raw_mail("Re: Re: Spørgsmål", message_id="<c@x>", in_reply_to="<b@x>", references="<a@x> <b@x>")
    )
    services.sync(mailbox)
    assert mailbox.threads.get().messages.count() == 2


def test_a_mail_joins_a_sag_in_another_folder(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.add_folder("Fest")
    server.put(1, raw_mail("Spørgsmål", message_id="<a@x>"))
    server.put(1, raw_mail("Re: Spørgsmål", message_id="<b@x>", in_reply_to="<a@x>"), folder="Fest")
    services.sync(mailbox)
    sag = mailbox.threads.get()
    assert set(sag.messages.values_list("folder__name", flat=True)) == {"INBOX", "Fest"}


def test_the_subject_alone_never_merges_two_sager(server: FakeServer) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox, raw_mail("Hej"), raw_mail("Hej"))
    assert mailbox.threads.count() == 2


def test_a_mail_in_the_sent_folder_joins_a_sag_but_never_starts_one(server: FakeServer) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox, raw_mail("Spørgsmål", message_id="<a@x>"))
    waiting = col(mailbox, ColumnRole.AFVENTER)
    MailThread.objects.update(column=waiting)
    server.put(
        1,
        raw_mail("Re: Spørgsmål", sender="ak@gahk.dk", message_id="<r@x>", in_reply_to="<a@x>"),
        folder="Sent",
    )
    server.put(2, raw_mail("Hilsen fra webmail", sender="ak@gahk.dk"), folder="Sent")
    services.sync(mailbox)
    sag = mailbox.threads.get()  # still one: the unrelated sent mail made none
    assert sag.messages.count() == 2 and sag.column == waiting  # and our own mail changed nothing
    orphan = mailbox.messages.get(subject="Hilsen fra webmail")
    assert orphan.thread is None


def test_a_customers_reply_reopens_an_answered_sag(server: FakeServer) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox, raw_mail("Spørgsmål", message_id="<a@x>"))
    sag = mailbox.threads.get()
    sag.column = col(mailbox, ColumnRole.AFVENTER)
    sag.assignee = None
    sag.save()
    server.put(2, raw_mail("Re: Spørgsmål", message_id="<b@x>", in_reply_to="<a@x>"))
    services.sync(mailbox)
    sag.refresh_from_db()
    assert sag.column == col(mailbox, ColumnRole.NY)
    event = MailEvent.objects.get(thread=sag, action=MailEvent.Action.STATUS)
    assert event.detail == "Ny" and event.resident is None


def test_a_reply_after_faerdig_reopens_and_keeps_the_assignee(
    server: FakeServer, make_resident: MakeResident
) -> None:
    mailbox = make_mailbox()
    owner = make_resident("owner@intern.dk")
    synced(server, mailbox, raw_mail("Spørgsmål", message_id="<a@x>"))
    MailThread.objects.update(column=col(mailbox, ColumnRole.FAERDIG), assignee=owner)
    server.put(2, raw_mail("Re: Spørgsmål", message_id="<b@x>", in_reply_to="<a@x>"))
    services.sync(mailbox)
    sag = mailbox.threads.get()
    assert sag.column == col(mailbox, ColumnRole.NY) and sag.assignee == owner


def test_our_own_sent_copy_does_not_reopen_a_sag(server: FakeServer) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox, raw_mail("Spørgsmål", message_id="<a@x>"))
    MailThread.objects.update(column=col(mailbox, ColumnRole.FAERDIG))
    server.put(
        1,
        raw_mail("Re: Spørgsmål", sender="ak@gahk.dk", message_id="<r@x>", in_reply_to="<a@x>"),
        folder="Sent",
    )
    # The same, but landing in the INBOX (a Bcc to ourselves): still our own mail, not a reply to act on.
    server.put(2, raw_mail("Re: Spørgsmål", sender="AK@gahk.dk", message_id="<r2@x>", in_reply_to="<a@x>"))
    services.sync(mailbox)
    assert mailbox.threads.get().column == col(mailbox, ColumnRole.FAERDIG)


def test_a_first_sync_files_read_history_as_faerdig_and_unread_as_ny(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.put(1, raw_mail("Gammel og læst", message_id="<old@x>"), {r"\Seen"})
    server.put(2, raw_mail("Gammel med svar", message_id="<q@x>"), {r"\Seen"})
    server.put(3, raw_mail("Re: Gammel med svar", message_id="<q2@x>", in_reply_to="<q@x>"))  # unread
    server.put(4, raw_mail("Ulæst", message_id="<new@x>"))
    services.sync(mailbox)
    by_subject = {t.subject: t.column for t in mailbox.threads.all()}
    assert by_subject == {
        "Gammel og læst": col(mailbox, ColumnRole.FAERDIG),
        "Gammel med svar": col(mailbox, ColumnRole.NY),  # something in it is still unread
        "Ulæst": col(mailbox, ColumnRole.NY),
    }
    # The reply that arrived in the same first sync did not "reopen" anything or log a move.
    assert not MailEvent.objects.filter(action=MailEvent.Action.STATUS).exists()
    # After the first sync a new, read mail is just a new sag.
    server.put(5, raw_mail("Ny og allerede læst"), {r"\Seen"})
    services.sync(mailbox)
    assert mailbox.threads.get(subject="Ny og allerede læst").column == col(mailbox, ColumnRole.NY)


def test_a_folder_added_later_has_its_own_first_sync(server: FakeServer) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox, raw_mail("Indbakke"))
    server.add_folder("Fest")
    server.put(1, raw_mail("Gammel festmail"), {r"\Seen"}, folder="Fest")
    services.sync(mailbox)
    assert mailbox.threads.get(subject="Gammel festmail").column == col(mailbox, ColumnRole.FAERDIG)
    assert mailbox.threads.get(subject="Indbakke").column == col(mailbox, ColumnRole.NY)


def test_a_mail_moved_out_of_the_inbox_keeps_its_sags_column(server: FakeServer) -> None:
    mailbox = make_mailbox()
    custom = columns.add(mailbox, "Hos brygger")
    server.add_folder("Arkiv")
    synced(server, mailbox, raw_mail("Spørgsmål", message_id="<a@x>"))
    sag = mailbox.threads.get()
    sag.column, sag.assignee = custom, None
    sag.save()
    raw, flags = server.take(1)  # moved in one.com: a new UID in the other folder
    server.put(7, raw, flags, folder="Arkiv")
    services.sync(mailbox)
    sag.refresh_from_db()
    assert mailbox.threads.count() == 1 and sag.column == custom
    assert sag.messages.get().folder.name == "Arkiv"


def test_a_mail_moved_into_the_inbox_keeps_its_sags_column(server: FakeServer) -> None:
    mailbox = make_mailbox()
    custom = columns.add(mailbox, "Hos brygger")
    server.add_folder("Arkiv")
    server.put(3, raw_mail("Spørgsmål", message_id="<a@x>"), {r"\Seen"}, folder="Arkiv")
    services.sync(mailbox)
    sag = mailbox.threads.get()
    sag.column = custom
    sag.save()
    raw, flags = server.take(3, "Arkiv")
    server.put(9, raw, flags)  # the INBOX is synced first, before the old row has been pruned
    services.sync(mailbox)
    sag.refresh_from_db()
    assert mailbox.threads.count() == 1 and sag.column == custom
    assert sag.messages.get().folder.name == "INBOX"


def test_a_moved_mail_is_not_a_reply_that_reopens(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.add_folder("Arkiv")
    synced(server, mailbox, raw_mail("Spørgsmål", message_id="<a@x>"))
    MailThread.objects.update(column=col(mailbox, ColumnRole.FAERDIG))
    raw, flags = server.take(1)
    server.put(7, raw, flags, folder="Arkiv")
    services.sync(mailbox)
    assert mailbox.threads.get().column == col(mailbox, ColumnRole.FAERDIG)


def test_a_uidvalidity_reset_keeps_the_sags(server: FakeServer) -> None:
    mailbox = make_mailbox()
    custom = columns.add(mailbox, "I gang nu")
    synced(server, mailbox, raw_mail("Spørgsmål", message_id="<a@x>"))
    MailThread.objects.update(column=custom)
    server.uidvalidity = 2
    services.sync(mailbox)
    assert mailbox.threads.get().column == custom and mailbox.messages.count() == 1


def test_a_sag_with_no_mail_left_is_deleted_at_the_end(server: FakeServer) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox, raw_mail("Forsvinder"), raw_mail("Bliver"))
    server.take(1)
    services.sync(mailbox)
    assert list(mailbox.threads.values_list("subject", flat=True)) == ["Bliver"]


def test_empty_sager_are_kept_when_a_folder_failed(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.add_folder("Fest")
    synced(server, mailbox, raw_mail("Forsvinder"))
    server.take(1)
    server.broken.add("Fest")
    services.sync(mailbox)
    assert mailbox.threads.count() == 1  # a half-seen mailbox must not cost anyone a sag's status
    server.broken.clear()
    services.sync(mailbox)
    assert mailbox.threads.count() == 0


# --- compose and reply ------------------------------------------------------------------------------


def test_compose_starts_a_waiting_sag_and_the_sent_copy_joins_it(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    response = client.post(
        reverse("postkasser:compose", args=["ak"]),
        {"to": "a@example.com", "subject": "Nyt fra AK", "body": "Hej"},
    )
    sag = mailbox.threads.get()
    assert response["Location"] == reverse("postkasser:thread", args=["ak", sag.pk])
    assert sag.column == col(mailbox, ColumnRole.AFVENTER) and sag.assignee == ak_member
    (sent,) = mail.outbox
    assert sag.pending_message_id == sent.extra_headers["Message-ID"]
    event = MailEvent.objects.get(thread=sag)
    assert (event.action, event.resident) == (MailEvent.Action.SEND, ak_member)
    # The copy arrives in the sent folder on the next sync and joins — no second sag.
    services.sync(mailbox)
    sag.refresh_from_db()
    assert mailbox.threads.count() == 1 and sag.messages.count() == 1 and sag.pending_message_id == ""
    assert sag.column == col(mailbox, ColumnRole.AFVENTER)


def test_a_reply_to_a_just_sent_mail_joins_before_its_copy_is_synced(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    client.post(
        reverse("postkasser:compose", args=["ak"]), {"to": "a@example.com", "subject": "Nyt", "body": "Hej"}
    )
    sag = mailbox.threads.get()
    (sent,) = mail.outbox
    server.folders["Sent"].messages.clear()  # the copy has not been filed (yet)
    server.put(1, raw_mail("Re: Nyt", in_reply_to=sent.extra_headers["Message-ID"]))
    services.sync(mailbox)
    sag.refresh_from_db()
    assert mailbox.threads.count() == 1 and sag.messages.count() == 1
    assert sag.column == col(mailbox, ColumnRole.NY)  # and the customer's answer reopened it


def test_an_unsynced_compose_sag_survives_cleanup_for_a_while(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.append_error = imap.MailboxError("quota")  # the copy is never filed
    client.post(
        reverse("postkasser:compose", args=["ak"]), {"to": "a@example.com", "subject": "Nyt", "body": "Hej"}
    )
    services.sync(mailbox)
    sag = mailbox.threads.get()
    page = client.get(reverse("postkasser:thread", args=["ak", sag.pk])).content.decode()
    assert "kopien i den sendte post er endnu ikke hentet" in page
    MailThread.objects.update(created_at=timezone.now() - services.PENDING_GRACE - timedelta(hours=1))
    services.sync(mailbox)
    assert not mailbox.threads.exists()


def test_a_reply_moves_the_sag_to_waiting_and_assigns_the_replier(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    (message,) = synced(server, mailbox, raw_mail("Spørgsmål"))
    sag = message.thread
    assert sag is not None and sag.assignee is None
    response = client.post(
        reverse("postkasser:reply", args=["ak", message.pk]),
        {"to": "mette@example.com", "subject": "Re: Spørgsmål", "body": "Kl. 20"},
    )
    assert response["Location"] == reverse("postkasser:thread", args=["ak", sag.pk])
    sag.refresh_from_db()
    assert sag.column == col(mailbox, ColumnRole.AFVENTER) and sag.assignee == ak_member
    actions = set(MailEvent.objects.filter(thread=sag).values_list("action", flat=True))
    assert actions >= {MailEvent.Action.REPLY, MailEvent.Action.STATUS, MailEvent.Action.ASSIGN}
    # An existing assignee is not taken over by whoever happens to answer.
    other = Resident.objects.create_user("o@intern.dk", "pw", first_name="Ole", last_name="O")
    sag.assignee, sag.column = other, col(mailbox, ColumnRole.NY)
    sag.save()
    client.post(
        reverse("postkasser:reply", args=["ak", message.pk]),
        {"to": "mette@example.com", "subject": "Re: Spørgsmål", "body": "Igen"},
    )
    sag.refresh_from_db()
    assert sag.assignee == other and sag.column == col(mailbox, ColumnRole.AFVENTER)


def test_a_reply_to_a_mail_without_a_sag_makes_one(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.put(1, raw_mail("Fra webmail", sender="ak@gahk.dk"), folder="Sent")
    services.sync(mailbox)
    message = mailbox.messages.get()
    assert message.thread is None
    client.post(
        reverse("postkasser:reply", args=["ak", message.pk]),
        {"to": "mette@example.com", "subject": "Re: Fra webmail", "body": "x"},
    )
    message.refresh_from_db()
    assert message.thread is not None and message.thread.column == col(mailbox, ColumnRole.AFVENTER)


# --- the board --------------------------------------------------------------------------------------


def test_the_board_has_the_mailboxs_columns_in_order(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    make_thread(mailbox, "Ny sag")
    make_thread(mailbox, "Sag i gang", column=mailbox.columns.get(name="I gang"))
    response = client.get(reverse("postkasser:board", args=["ak"]))
    shown = [(c.name, [t.subject for t in cards]) for c, cards in response.context["columns"]]
    assert shown == [("Ny", ["Ny sag"]), ("I gang", ["Sag i gang"]), ("Afventer svar", []), ("Færdig", [])]
    page = response.content.decode()
    for column in mailbox.columns.all():  # what the drag script reads: data-status is the column pk
        assert f'data-status="{column.pk}"' in page
    assert "data-kanban" in page and 'data-live="postkasser' not in page and "data-live-region" not in page


def test_a_card_shows_what_the_sag_is_about(client: Client, ak_member: Resident, server: FakeServer) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.add_folder("Fest")
    server.put(1, raw_mail("Tilmelding", message_id="<a@x>", attachment=("a.html", b"x")))
    server.put(
        1, raw_mail("Re: Tilmelding", sender="Ole <ole@example.com>", in_reply_to="<a@x>"), folder="Fest"
    )
    services.sync(mailbox)
    sag = mailbox.threads.get()
    sag.assignee = ak_member
    sag.save()
    response = client.get(reverse("postkasser:board", args=["ak"]))
    (card,) = [t for _c, cards in response.context["columns"] for t in cards]
    assert (card.message_count, card.unread_count, card.attachment_count) == (2, 2, 1)
    assert card.folder_chips == ["Fest"]  # the inbox is the default and gets no chip
    assert card.latest_sender in {"Ole", "Mette"}
    page = response.content.decode()
    assert "Ulæst" in page and "📎" in page and ak_member.full_name in page


def test_the_board_filters_by_folder_owner_and_search(
    client: Client, ak_member: Resident, make_resident: MakeResident
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    other = make_resident("kollega@intern.dk", roles=(Role.AK,))
    mine = make_thread(mailbox, "Min sag", assignee=ak_member, sender="bent@example.com")
    theirs = make_thread(mailbox, "Kollegaens sag", assignee=other, folder="Fest")
    free = make_thread(mailbox, "Herreløs sag", sender="ingrid@example.com")
    ny = col(mailbox, ColumnRole.NY)
    assert set(subjects_in(client, ny, mailbox)) == {mine.subject, theirs.subject, free.subject}
    assert subjects_in(client, ny, mailbox, ansvarlig="mig") == ["Min sag"]
    assert subjects_in(client, ny, mailbox, ansvarlig="ingen") == ["Herreløs sag"]
    assert subjects_in(client, ny, mailbox, ansvarlig="noget-andet").__len__() == 3  # unknown: no filter
    fest = MailFolder.objects.get(mailbox=mailbox, name="Fest")
    assert subjects_in(client, ny, mailbox, mappe=str(fest.pk)) == ["Kollegaens sag"]
    assert subjects_in(client, ny, mailbox, mappe="99999").__len__() == 3
    assert subjects_in(client, ny, mailbox, q="kollega") == ["Kollegaens sag"]  # subject
    assert subjects_in(client, ny, mailbox, q="INGRID") == ["Herreløs sag"]  # a sender's address, any case
    assert subjects_in(client, ny, mailbox, q="Bent", ansvarlig="mig") == ["Min sag"]  # combined
    assert subjects_in(client, ny, mailbox, q="findes ikke") == []


def test_the_search_finds_a_sag_by_any_mails_sender(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.put(1, raw_mail("Spørgsmål", sender="Mette <mette@example.com>", message_id="<a@x>"))
    server.put(2, raw_mail("Re: Spørgsmål", sender="Per <per@example.org>", in_reply_to="<a@x>"))
    services.sync(mailbox)
    ny = col(mailbox, ColumnRole.NY)
    assert subjects_in(client, ny, mailbox, q="example.org") == ["Spørgsmål"]  # the second mail's sender
    assert subjects_in(client, ny, mailbox, q="per") == ["Spørgsmål"]


def test_the_folder_filter_lists_synced_folders_with_unread_counts(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.add_folder("Zeta")
    server.add_folder("INBOX.Fest")
    server.add_folder(utf7.encode("Ølkælder"))
    server.add_folder("Drafts", r"\Drafts")
    server.add_folder("Papirkurv")
    server.add_folder("Delt", r"\Noselect")
    server.put(1, raw_mail("Ulæst"), folder="Zeta")
    server.put(2, raw_mail("Læst"), {r"\Seen"}, folder="Zeta")
    services.sync(mailbox)
    response = client.get(reverse("postkasser:board", args=["ak"]))
    shown = response.context["folders"]
    names = [f.display_name for f in shown]
    assert names[0] == "Indbakke"  # inbox first, then by path
    assert names[1:] == sorted(
        names[1:], key=lambda n: next(f.path for f in shown if f.display_name == n).lower()
    )
    assert {"Fest", "Ølkælder", "Zeta", "Sent"} <= set(names)
    assert not {"Drafts", "Papirkurv", "Delt"} & set(names)
    assert next(f for f in shown if f.display_name == "Zeta").unread == 1
    assert next(f for f in shown if f.display_name == "Fest").indent == "  "
    assert "Zeta (1)" in response.content.decode()


def test_faerdig_shows_thirty_days_and_the_rest_is_in_the_archive(
    client: Client, ak_member: Resident
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    done = col(mailbox, ColumnRole.FAERDIG)
    make_thread(mailbox, "Nylig færdig", column=done, age_days=10)
    make_thread(mailbox, "Gammel færdig", column=done, age_days=40, sender="gammel@example.com")
    make_thread(mailbox, "Gammel men åben", age_days=400)
    assert subjects_in(client, done, mailbox) == ["Nylig færdig"]
    assert subjects_in(client, col(mailbox, ColumnRole.NY), mailbox) == ["Gammel men åben"]
    archive = client.get(reverse("postkasser:archive", args=["ak"]))
    assert [t.subject for t in archive.context["page"]] == ["Gammel færdig"]
    found = client.get(reverse("postkasser:archive", args=["ak"]), {"q": "gammel@example"})
    assert [t.subject for t in found.context["page"]] == ["Gammel færdig"]
    assert list(client.get(reverse("postkasser:archive", args=["ak"]), {"q": "nylig"}).context["page"]) == []


def test_the_window_follows_the_faerdig_role_not_the_name(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    done = col(mailbox, ColumnRole.FAERDIG)
    columns.rename(done, "Afsluttet")
    make_thread(mailbox, "Gammel", column=done, age_days=40)
    assert subjects_in(client, done, mailbox) == []
    assert [t.subject for t in client.get(reverse("postkasser:archive", args=["ak"])).context["page"]] == [
        "Gammel"
    ]


def test_the_archive_is_closed_to_outsiders(client: Client, make_resident: MakeResident) -> None:
    make_mailbox(roles=["ak"])
    client.force_login(make_resident("plain@intern.dk"))
    assert client.get(reverse("postkasser:archive", args=["ak"])).status_code == 404
    assert client.get(reverse("postkasser:columns", args=["ak"])).status_code == 404


# --- the sag page -----------------------------------------------------------------------------------


def test_the_sag_page_shows_the_newest_mail_and_replies_to_the_newest_incoming(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.put(1, raw_mail("Spørgsmål", message_id="<a@x>", body="Første mail", date=days_ago(3)))
    server.put(
        1,
        raw_mail(
            "Re: Spørgsmål",
            sender="ak@gahk.dk",
            message_id="<b@x>",
            in_reply_to="<a@x>",
            body="Vores svar",
            date=days_ago(1),
        ),
        folder="Sent",
    )
    services.sync(mailbox)
    sag = mailbox.threads.get()
    incoming = mailbox.messages.get(message_id="<a@x>")
    response = client.get(reverse("postkasser:thread", args=["ak", sag.pk]))
    page = response.content.decode()
    assert response.status_code == 200 and "Vores svar" in page  # the newest is ours, shown in full
    assert [m.message_id for m in response.context["mails"]] == ["<a@x>", "<b@x>"]  # chronological
    assert response.context["reply_to"] == incoming
    assert reverse("postkasser:reply", args=["ak", incoming.pk]) in page
    assert 'srcdoc="&lt;!doctype html&gt;' in page and 'sandbox="allow-popups' in page
    assert reverse("postkasser:message", args=["ak", incoming.pk]) in page


def test_the_sag_page_survives_a_server_failure(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    (message,) = synced(server, mailbox, raw_mail("Spørgsmål"))
    server.take(1)
    response = client.get(reverse("postkasser:thread", args=["ak", message.thread_id]))
    assert response.status_code == 200 and "Kunne ikke hente mailen" in response.content.decode()


def test_the_sag_page_has_plain_forms_for_status_and_assignee(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    (message,) = synced(server, mailbox, raw_mail("Spørgsmål"))
    page = client.get(reverse("postkasser:thread", args=["ak", message.thread_id])).content.decode()
    assert "→ Færdig" in page and "→ Ny" not in page  # a button per OTHER column
    assert 'name="assignee"' in page and ak_member.full_name in page
    assert "csrfmiddlewaretoken" in page
    msg_page = client.get(reverse("postkasser:message", args=["ak", message.pk])).content.decode()
    assert reverse("postkasser:thread", args=["ak", message.thread_id]) in msg_page  # "‹ Til sagen"


def test_the_sag_history_lists_moves_and_handovers_but_not_reads(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    (message,) = synced(server, mailbox, raw_mail("Spørgsmål"))
    sag = message.thread
    assert sag is not None
    done = col(mailbox, ColumnRole.FAERDIG)
    client.post(reverse("postkasser:set_status", args=["ak", sag.pk]), {"status": str(done.pk)})
    client.post(reverse("postkasser:set_assignee", args=["ak", sag.pk]), {"assignee": str(ak_member.pk)})
    client.get(reverse("postkasser:thread", args=["ak", sag.pk]))  # logs a READ, which is not shown
    history = list(client.get(reverse("postkasser:thread", args=["ak", sag.pk])).context["history"])
    assert {e.action for e in history} == {MailEvent.Action.STATUS, MailEvent.Action.ASSIGN}
    page = client.get(reverse("postkasser:thread", args=["ak", sag.pk])).content.decode()
    assert f"Flyttet til Færdig af {ak_member.full_name}" in page
    assert f"Tildelt {ak_member.full_name} af {ak_member.full_name}" in page


# --- moving a sag and handing it over ---------------------------------------------------------------


def test_set_status_moves_a_sag_to_a_column(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    sag = make_thread(mailbox)
    target = mailbox.columns.get(name="I gang")
    response = client.post(reverse("postkasser:set_status", args=["ak", sag.pk]), {"status": str(target.pk)})
    assert response.status_code == 302 and response["Location"] == reverse(
        "postkasser:thread", args=["ak", sag.pk]
    )
    sag.refresh_from_db()
    assert sag.column == target
    event = MailEvent.objects.get(thread=sag)
    assert (event.action, event.detail, event.resident) == (MailEvent.Action.STATUS, "I gang", ak_member)
    # The same column again is no change and logs nothing.
    client.post(reverse("postkasser:set_status", args=["ak", sag.pk]), {"status": str(target.pk)})
    assert MailEvent.objects.filter(thread=sag).count() == 1


def test_set_status_answers_the_drag_script_in_its_own_shape(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    sag = make_thread(mailbox)
    target = col(mailbox, ColumnRole.AFVENTER)
    response = client.post(
        reverse("postkasser:set_status", args=["ak", sag.pk]),
        {"status": str(target.pk)},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    body = response.json()
    assert response.status_code == 200 and body["ok"] is True and body["error"] == ""
    assert body["status"] == str(target.pk)  # the script compares this string with what it sent
    sag.refresh_from_db()
    assert sag.column == target
    refused = client.post(
        reverse("postkasser:set_status", args=["ak", sag.pk]),
        {"status": "abc"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert refused.status_code == 200 and refused.json()["ok"] is False and refused.json()["error"]


def test_a_column_of_another_mailbox_is_refused_on_move(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    foreign = col(make_mailbox("fest", name="Festgruppen", roles=["ak"]), ColumnRole.FAERDIG)
    sag = make_thread(mailbox)
    response = client.post(reverse("postkasser:set_status", args=["ak", sag.pk]), {"status": str(foreign.pk)})
    assert response.status_code == 404
    sag.refresh_from_db()
    assert sag.column == col(mailbox, ColumnRole.NY)
    assert (
        client.post(reverse("postkasser:set_status", args=["ak", sag.pk]), {"status": "99999"}).status_code
        == 404
    )


def test_set_status_and_assignee_need_access_and_post(client: Client, make_resident: MakeResident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    sag = make_thread(mailbox)
    outsider = make_resident("ude@intern.dk")
    client.force_login(outsider)
    done = col(mailbox, ColumnRole.FAERDIG)
    assert (
        client.post(
            reverse("postkasser:set_status", args=["ak", sag.pk]), {"status": str(done.pk)}
        ).status_code
        == 404
    )
    assert (
        client.post(reverse("postkasser:set_assignee", args=["ak", sag.pk]), {"assignee": ""}).status_code
        == 404
    )
    assert client.get(reverse("postkasser:thread", args=["ak", sag.pk])).status_code == 404
    sag.refresh_from_db()
    assert sag.column == col(mailbox, ColumnRole.NY)
    client.force_login(make_resident("ak@intern.dk", roles=(Role.AK,)))
    assert client.get(reverse("postkasser:set_status", args=["ak", sag.pk])).status_code == 405
    assert client.get(reverse("postkasser:set_assignee", args=["ak", sag.pk])).status_code == 405


def test_the_sag_endpoints_require_a_csrf_token(make_resident: MakeResident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    sag = make_thread(mailbox)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(make_resident("ak@intern.dk", roles=(Role.AK,)))
    done = col(mailbox, ColumnRole.FAERDIG)
    assert (
        strict.post(
            reverse("postkasser:set_status", args=["ak", sag.pk]), {"status": str(done.pk)}
        ).status_code
        == 403
    )
    assert (
        strict.post(reverse("postkasser:set_assignee", args=["ak", sag.pk]), {"assignee": ""}).status_code
        == 403
    )
    assert (
        strict.post(reverse("postkasser:columns", args=["ak"]), {"action": "add", "name": "X"}).status_code
        == 403
    )


def test_the_wrong_slug_with_the_right_sag_pk_is_404(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    other = make_mailbox("fest", name="Festgruppen", roles=["ak"])
    sag = make_thread(mailbox)
    done = col(other, ColumnRole.FAERDIG)
    assert client.get(reverse("postkasser:thread", args=["fest", sag.pk])).status_code == 404
    assert (
        client.post(
            reverse("postkasser:set_status", args=["fest", sag.pk]), {"status": str(done.pk)}
        ).status_code
        == 404
    )
    assert (
        client.post(reverse("postkasser:set_assignee", args=["fest", sag.pk]), {"assignee": ""}).status_code
        == 404
    )


def test_set_assignee_accepts_members_and_clears(
    client: Client, ak_member: Resident, make_resident: MakeResident
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    colleague = make_resident("kollega@intern.dk", roles=(Role.AK,))
    sag = make_thread(mailbox)
    url = reverse("postkasser:set_assignee", args=["ak", sag.pk])
    assert client.post(url, {"assignee": str(colleague.pk)}).status_code == 302
    sag.refresh_from_db()
    assert sag.assignee == colleague
    event = MailEvent.objects.get(thread=sag)
    assert (event.action, event.detail, event.resident) == (
        MailEvent.Action.ASSIGN,
        colleague.full_name,
        ak_member,
    )
    client.post(url, {"assignee": ""})
    sag.refresh_from_db()
    assert sag.assignee is None
    body = client.post(
        url, {"assignee": str(colleague.pk)}, headers={"X-Requested-With": "XMLHttpRequest"}
    ).json()
    assert body["ok"] is True and body["assignee"] == colleague.pk


def test_set_assignee_rejects_someone_who_cannot_open_the_mailbox(
    client: Client, ak_member: Resident, make_resident: MakeResident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    stranger = make_resident("fremmed@intern.dk")
    admin = make_resident(
        "adm@intern.dk", roles=(Role.ADMINISTRATOR,)
    )  # can open it, but is not on the group
    sag = make_thread(mailbox)
    url = reverse("postkasser:set_assignee", args=["ak", sag.pk])
    for who in (stranger.pk, admin.pk, 99999, "abc"):
        response = client.post(url, {"assignee": str(who)}, headers={"X-Requested-With": "XMLHttpRequest"})
        assert response.status_code == 200 and response.json()["ok"] is False and response.json()["error"]
    sag.refresh_from_db()
    assert sag.assignee is None and not MailEvent.objects.filter(thread=sag).exists()
    follow = client.post(url, {"assignee": str(stranger.pk)}, follow=True)
    assert "har ikke adgang til postkassen" in follow.content.decode()


def test_members_are_the_group_now_and_not_expanded_administrators(make_resident: MakeResident) -> None:
    from postkasser import access

    group = Workgroup.objects.create(name="Festgruppen")
    mailbox = make_mailbox("fest", roles=["ak"])
    mailbox.workgroups.add(group)
    by_role = make_resident("rolle@intern.dk", roles=(Role.AK,))
    by_group = make_resident("gruppe@intern.dk")
    join_workgroup(by_group, group)
    make_resident("admin@intern.dk", roles=(Role.ADMINISTRATOR,))
    make_resident("andre@intern.dk")
    left = make_resident("gaaet@intern.dk")
    year, month = active_period()
    previous = (year, month - 1) if month > 1 else (year - 1, 12)
    room = Room.objects.create(legacy_index=950, number=950, floor="1", side="mod gaden")
    Residency.objects.create(resident=left, room=room, workgroup=group, year=previous[0], month=previous[1])
    RoleAssignment.objects.create(resident=left, role=Role.AK, year=previous[0], month=previous[1])
    assert set(access.members(mailbox)) == {by_role, by_group}


# --- columns: defaults, invariants, the Kolonner page ---------------------------------------------------


def test_a_new_mailbox_gets_the_default_columns() -> None:
    mailbox = make_mailbox()
    assert [(c.name, c.role) for c in mailbox.columns.all()] == [
        ("Ny", "ny"),
        ("I gang", ""),
        ("Afventer svar", "afventer"),
        ("Færdig", "faerdig"),
    ]


def test_the_database_allows_one_column_per_role_and_name() -> None:
    mailbox = make_mailbox()
    with pytest.raises(IntegrityError), transaction.atomic():
        BoardColumn.objects.create(mailbox=mailbox, name="Anden", role=ColumnRole.NY, position=9)
    with pytest.raises(IntegrityError), transaction.atomic():
        BoardColumn.objects.create(mailbox=mailbox, name="Ny", position=9)
    BoardColumn.objects.create(mailbox=mailbox, name="Én uden funktion", position=9)
    BoardColumn.objects.create(mailbox=mailbox, name="To uden funktion", position=10)  # no role: any number


def test_ny_and_faerdig_cannot_be_deleted_until_their_role_moves() -> None:
    mailbox = make_mailbox()
    for role in (ColumnRole.NY, ColumnRole.FAERDIG):
        with pytest.raises(columns.ColumnError, match="funktion"):
            columns.delete(col(mailbox, role))
    assert mailbox.columns.count() == 4
    fresh = columns.add(mailbox, "Nyt hjem")
    columns.set_roles(
        mailbox,
        {
            "ny": fresh.pk,
            "afventer": col(mailbox, ColumnRole.AFVENTER).pk,
            "faerdig": col(mailbox, ColumnRole.FAERDIG).pk,
        },
    )
    columns.delete(mailbox.columns.get(name="Ny"))  # no longer holds the role
    assert col(mailbox, ColumnRole.NY) == fresh


def test_deleting_a_column_moves_its_sager_to_ny() -> None:
    mailbox = make_mailbox()
    middle = mailbox.columns.get(name="I gang")
    make_thread(mailbox, "A", column=middle)
    make_thread(mailbox, "B", column=middle)
    assert columns.delete(middle) == 2
    assert not mailbox.columns.filter(name="I gang").exists()
    assert set(col(mailbox, ColumnRole.NY).threads.values_list("subject", flat=True)) == {"A", "B"}
    assert [c.position for c in mailbox.columns.all()] == [0, 1, 2]  # closed up


def test_deleting_the_waiting_column_leaves_the_group_without_one(
    client: Client, ak_member: Resident
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    waiting = col(mailbox, ColumnRole.AFVENTER)
    make_thread(mailbox, "Venter", column=waiting)
    columns.delete(waiting)
    assert columns.for_role(mailbox, ColumnRole.AFVENTER) is None
    assert mailbox.threads.get().column == col(mailbox, ColumnRole.NY)
    assert client.get(reverse("postkasser:board", args=["ak"])).status_code == 200
    assert client.get(reverse("postkasser:columns", args=["ak"])).status_code == 200


def test_a_column_that_holds_sager_cannot_be_deleted_behind_columns_py() -> None:
    mailbox = make_mailbox()
    make_thread(mailbox, "A", column=mailbox.columns.get(name="I gang"))
    with pytest.raises(RestrictedError):
        mailbox.columns.get(name="I gang").delete()


def test_deleting_a_mailbox_with_sager_still_works() -> None:
    mailbox = make_mailbox()
    make_thread(mailbox, "A")
    mailbox.delete()
    assert not MailThread.objects.exists() and not BoardColumn.objects.exists()


def test_rename_validates_and_keeps_the_role() -> None:
    mailbox = make_mailbox()
    waiting = col(mailbox, ColumnRole.AFVENTER)
    columns.rename(waiting, "  Venter   på brygger ")
    waiting.refresh_from_db()
    assert waiting.name == "Venter på brygger" and waiting.role == ColumnRole.AFVENTER
    for bad in ("", "   ", "x" * 61, "ny", "FÆRDIG"):
        with pytest.raises(columns.ColumnError):
            columns.rename(waiting, bad)
    columns.rename(waiting, "Venter på brygger")  # its own name is not a clash


def test_add_refuses_duplicates_and_gives_no_role() -> None:
    mailbox = make_mailbox()
    created = columns.add(mailbox, "Til bestyrelsen")
    assert created.role == "" and created.position == 4
    with pytest.raises(columns.ColumnError):
        columns.add(mailbox, "til bestyrelsen")
    other = make_mailbox("fest", name="Fest")
    columns.add(other, "Til bestyrelsen")  # names are per mailbox


def test_move_reorders_and_stops_at_the_ends() -> None:
    mailbox = make_mailbox()
    names = lambda: [c.name for c in mailbox.columns.all()]  # noqa: E731
    columns.move(mailbox.columns.get(name="Færdig"), -1)
    assert names() == ["Ny", "I gang", "Færdig", "Afventer svar"]
    columns.move(mailbox.columns.get(name="Ny"), -1)  # already first
    columns.move(mailbox.columns.get(name="Afventer svar"), 1)  # already last
    assert names() == ["Ny", "I gang", "Færdig", "Afventer svar"]
    columns.move(mailbox.columns.get(name="Ny"), 1)
    assert names() == ["I gang", "Ny", "Færdig", "Afventer svar"]


def test_set_roles_can_swap_and_refuses_nonsense() -> None:
    mailbox = make_mailbox()
    ny, done = col(mailbox, ColumnRole.NY), col(mailbox, ColumnRole.FAERDIG)
    columns.set_roles(mailbox, {"ny": done.pk, "afventer": None, "faerdig": ny.pk})  # a swap, and no waiting
    assert col(mailbox, ColumnRole.NY) == done and col(mailbox, ColumnRole.FAERDIG) == ny
    assert columns.for_role(mailbox, ColumnRole.AFVENTER) is None
    with pytest.raises(columns.ColumnError):
        columns.set_roles(
            mailbox, {"ny": ny.pk, "afventer": ny.pk, "faerdig": done.pk}
        )  # one column, two roles
    with pytest.raises(columns.ColumnError):
        columns.set_roles(mailbox, {"ny": None, "afventer": None, "faerdig": done.pk})  # ny is required
    with pytest.raises(columns.ColumnError):
        columns.set_roles(mailbox, {"ny": ny.pk, "afventer": None, "faerdig": None})  # and so is faerdig
    foreign = col(make_mailbox("fest", name="Fest"), ColumnRole.NY)
    with pytest.raises(columns.ColumnError):
        columns.set_roles(mailbox, {"ny": foreign.pk, "afventer": None, "faerdig": done.pk})
    assert col(mailbox, ColumnRole.NY) == done  # every refusal left things as they were


def test_validate_layout_checks_the_whole_set() -> None:
    ok = [("A", "ny"), ("B", ""), ("C", "faerdig")]
    columns.validate_layout(ok)
    for bad in (
        [("A", ""), ("C", "faerdig")],  # no ny
        [("A", "ny"), ("B", "ny"), ("C", "faerdig")],  # two ny
        [("A", "ny")],  # no faerdig
        [*ok, ("D", "afventer"), ("E", "afventer")],  # two afventer
        [*ok, ("a", "")],  # clashing names, case-insensitively
    ):
        with pytest.raises(columns.ColumnError):
            columns.validate_layout(bad)


def test_the_admin_inline_enforces_the_same_rules() -> None:
    from django.forms import inlineformset_factory

    from postkasser.admin import BoardColumnFormSet

    mailbox = make_mailbox()
    factory = inlineformset_factory(
        Mailbox, BoardColumn, formset=BoardColumnFormSet, fields=["name", "position", "role"], extra=0
    )

    def data(**overrides: dict[str, str]) -> dict[str, str]:
        rows = list(mailbox.columns.all())
        out = {
            "columns-TOTAL_FORMS": str(len(rows)),
            "columns-INITIAL_FORMS": str(len(rows)),
            "columns-MIN_NUM_FORMS": "0",
            "columns-MAX_NUM_FORMS": "1000",
        }
        for i, c in enumerate(rows):
            out |= {f"columns-{i}-id": str(c.pk), f"columns-{i}-mailbox": str(mailbox.pk),
                    f"columns-{i}-name": c.name, f"columns-{i}-position": str(c.position), f"columns-{i}-role": c.role}  # fmt: skip
            out |= {f"columns-{i}-{k}": v for k, v in overrides.get(c.name, {}).items()}
        return out

    assert factory(data(), instance=mailbox).is_valid()
    deleting_ny = factory(data(Ny={"DELETE": "on"}), instance=mailbox)
    assert not deleting_ny.is_valid() and "Indgående" in str(deleting_ny.non_form_errors())
    blanking_role = factory(data(Færdig={"role": ""}), instance=mailbox)
    assert not blanking_role.is_valid()


def test_the_mailbox_admin_shows_columns_on_change_but_not_on_add(
    client: Client, make_resident: MakeResident
) -> None:
    mailbox = make_mailbox()
    admin = make_resident("adm@intern.dk", roles=(Role.ADMINISTRATOR,))
    Resident.objects.filter(pk=admin.pk).update(is_superuser=True)
    client.force_login(admin)
    change = client.get(reverse("admin:postkasser_mailbox_change", args=[mailbox.pk]))
    assert change.status_code == 200 and "columns-TOTAL_FORMS" in change.content.decode()
    add = client.get(reverse("admin:postkasser_mailbox_add"))
    assert add.status_code == 200 and "columns-TOTAL_FORMS" not in add.content.decode()


def test_the_columns_page_edits_the_layout(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    url = reverse("postkasser:columns", args=["ak"])
    page = client.get(url).content.decode()
    assert "Kolonner" in page and "Funktioner" in page and "Indgående" in page

    def act(**data: str) -> str:
        return client.post(url, data, follow=True).content.decode()

    act(action="add", name="Til bestyrelsen")
    assert mailbox.columns.filter(name="Til bestyrelsen").exists()
    assert "allerede en kolonne" in act(action="add", name="til bestyrelsen")
    gang = mailbox.columns.get(name="I gang")
    act(action="rename", column=str(gang.pk), name="Hos brygger")
    gang.refresh_from_db()
    assert gang.name == "Hos brygger"
    act(action="down", column=str(gang.pk))
    assert [c.name for c in mailbox.columns.all()][:3] == ["Ny", "Afventer svar", "Hos brygger"]
    act(action="up", column=str(gang.pk))
    assert [c.name for c in mailbox.columns.all()][:3] == ["Ny", "Hos brygger", "Afventer svar"]
    make_thread(mailbox, "I brygger", column=gang)
    assert "1 sag flyttet til" in act(action="delete", column=str(gang.pk))
    assert mailbox.threads.get().column == col(mailbox, ColumnRole.NY)
    assert "funktion" in act(action="delete", column=str(col(mailbox, ColumnRole.NY).pk))
    assert "Ukendt" in act(action="rename", column="99999", name="x")
    assert "Ukendt" in act(action="vrøvl")


def test_the_columns_page_sets_the_roles(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    url = reverse("postkasser:columns", args=["ak"])
    fresh = columns.add(mailbox, "Nyt")
    waiting = col(mailbox, ColumnRole.AFVENTER)
    client.post(
        url,
        {
            "action": "roles",
            "role_ny": col(mailbox, ColumnRole.NY).pk,
            "role_afventer": fresh.pk,
            "role_faerdig": col(mailbox, ColumnRole.FAERDIG).pk,
        },
    )
    assert col(mailbox, ColumnRole.AFVENTER) == fresh and waiting.pk != fresh.pk
    client.post(
        url,
        {
            "action": "roles",
            "role_ny": col(mailbox, ColumnRole.NY).pk,
            "role_afventer": "",
            "role_faerdig": col(mailbox, ColumnRole.FAERDIG).pk,
        },
    )
    assert columns.for_role(mailbox, ColumnRole.AFVENTER) is None
    page = client.post(
        url, {"action": "roles", "role_ny": "", "role_afventer": "", "role_faerdig": ""}, follow=True
    )
    assert "Vælg en kolonne" in page.content.decode()
    assert col(mailbox, ColumnRole.NY)  # unchanged


def test_columns_of_two_mailboxes_are_independent(client: Client, ak_member: Resident) -> None:
    ak = make_mailbox(roles=["ak"])
    fest = make_mailbox("fest", name="Festgruppen", roles=["ak"])
    columns.rename(col(fest, ColumnRole.NY), "Henvendelser")
    assert col(ak, ColumnRole.NY).name == "Ny"
    page = client.get(reverse("postkasser:board", args=["fest"])).content.decode()
    assert "Henvendelser" in page
    # A column pk from the other mailbox is not editable through this one.
    client.post(
        reverse("postkasser:columns", args=["ak"]),
        {"action": "rename", "column": str(col(fest, ColumnRole.NY).pk), "name": "Hacket"},
    )
    assert col(fest, ColumnRole.NY).name == "Henvendelser"


# --- automation follows roles, never names --------------------------------------------------------------


def test_automation_follows_a_renamed_waiting_column(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    waiting = col(mailbox, ColumnRole.AFVENTER)
    columns.rename(waiting, "Venter på brygger")
    (message,) = synced(server, mailbox, raw_mail("Spørgsmål"))
    client.post(
        reverse("postkasser:reply", args=["ak", message.pk]),
        {"to": "mette@example.com", "subject": "Re: Spørgsmål", "body": "x"},
    )
    sag = mailbox.threads.get()
    assert sag.column == waiting and sag.column.name == "Venter på brygger"
    assert MailEvent.objects.get(thread=sag, action=MailEvent.Action.STATUS).detail == "Venter på brygger"


def test_automation_follows_the_role_when_it_moves_to_another_column(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    successor = columns.add(mailbox, "Svar udestår")
    columns.set_roles(
        mailbox,
        {
            "ny": col(mailbox, ColumnRole.NY).pk,
            "afventer": successor.pk,
            "faerdig": col(mailbox, ColumnRole.FAERDIG).pk,
        },
    )
    client.post(
        reverse("postkasser:compose", args=["ak"]), {"to": "a@example.com", "subject": "Nyt", "body": "Hej"}
    )
    assert mailbox.threads.get().column == successor
    # And the customer's reply goes back to whichever column holds "ny", even a renamed, moved one.
    renamed = col(mailbox, ColumnRole.NY)
    columns.rename(renamed, "Indbakke-sager")
    sent_id = mail.outbox[0].extra_headers["Message-ID"]
    server.put(1, raw_mail("Re: Nyt", in_reply_to=sent_id))
    services.sync(mailbox)
    assert mailbox.threads.get().column == renamed


def test_a_reply_leaves_the_column_alone_when_the_group_has_no_waiting_column(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    columns.delete(col(mailbox, ColumnRole.AFVENTER))
    (message,) = synced(server, mailbox, raw_mail("Spørgsmål"))
    sag = message.thread
    assert sag is not None
    gang = mailbox.columns.get(name="I gang")
    sag.column = gang
    sag.save()
    client.post(
        reverse("postkasser:reply", args=["ak", message.pk]),
        {"to": "mette@example.com", "subject": "Re: Spørgsmål", "body": "x"},
    )
    sag.refresh_from_db()
    assert sag.column == gang and sag.assignee == ak_member
    assert not MailEvent.objects.filter(thread=sag, action=MailEvent.Action.STATUS).exists()


def test_compose_without_a_waiting_column_lands_in_ny(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    columns.delete(col(mailbox, ColumnRole.AFVENTER))
    client.post(
        reverse("postkasser:compose", args=["ak"]), {"to": "a@example.com", "subject": "Nyt", "body": "Hej"}
    )
    assert mailbox.threads.get().column == col(mailbox, ColumnRole.NY)


def test_sync_files_new_sager_in_whichever_column_holds_ny(server: FakeServer) -> None:
    mailbox = make_mailbox()
    columns.set_roles(
        mailbox,
        {
            "ny": mailbox.columns.get(name="I gang").pk,
            "afventer": None,
            "faerdig": col(mailbox, ColumnRole.FAERDIG).pk,
        },
    )
    server.put(1, raw_mail("Spørgsmål"))
    services.sync(mailbox)
    assert mailbox.threads.get().column.name == "I gang"


# --- claims, races, admin, parsing ----------------------------------------------------------------------
# Concurrent syncs, the admin inline, defensive parsing — then moving mail between one.com folders.


def hold_claim(mailbox: Mailbox, age: timedelta = timedelta(0)) -> None:
    """Somebody else (another process) is syncing this mailbox."""
    Mailbox.objects.filter(pk=mailbox.pk).update(sync_started_at=timezone.now() - age)


def claimed_at(mailbox: Mailbox) -> object:
    return Mailbox.objects.values_list("sync_started_at", flat=True).get(pk=mailbox.pk)


def test_a_lost_insert_race_leaves_no_empty_sag(
    client: Client, ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two syncs of one folder: the other committed the same (folder, uid) a moment earlier. The loser
    used to have created the sag already, and — with a failing folder switching the cleanup off — left
    an empty card on the board for good."""
    mailbox = make_mailbox(roles=["ak"])
    synced(server, mailbox)
    server.add_folder("Broken")
    server.broken.add("Broken")
    server.put(5, raw_mail(subject="Dobbelt"))
    original = services._save

    def racing_save(message: MailMessage) -> bool:
        if not MailMessage.objects.filter(folder=message.folder, uid=message.uid).exists():
            MailMessage.objects.create(
                mailbox=message.mailbox,
                folder=message.folder,
                uid=message.uid,
                message_id=message.message_id,
                subject="Dobbelt",
            )
        return original(message)

    monkeypatch.setattr(services, "_save", racing_save)
    services.sync(mailbox)
    assert not MailThread.objects.filter(messages__isnull=True).exists()
    assert MailThread.objects.count() == 0
    assert "0 mails" not in board(client)


def test_a_sag_is_only_created_after_its_message_was_saved(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox)
    server.put(3, raw_mail(subject="Ny"))
    monkeypatch.setattr(services, "_save", lambda message: False)
    services.sync(mailbox)
    assert MailThread.objects.count() == 0


def test_a_claim_is_exclusive_and_released() -> None:
    mailbox = make_mailbox()
    with services.claim(mailbox) as first:
        assert first and claimed_at(mailbox) is not None
        with services.claim(mailbox) as second:
            assert not second
        assert claimed_at(mailbox) is not None  # the loser must not release the winner's claim
    assert claimed_at(mailbox) is None
    with services.claim(mailbox) as again:
        assert again


def test_a_claim_is_released_when_the_work_fails() -> None:
    mailbox = make_mailbox()
    with pytest.raises(RuntimeError), services.claim(mailbox):
        raise RuntimeError("boom")
    assert claimed_at(mailbox) is None


def test_a_stale_claim_is_taken_over_and_the_old_run_cannot_release_the_new_one() -> None:
    mailbox = make_mailbox()
    hold_claim(mailbox, services.CLAIM_TTL + timedelta(minutes=1))
    with services.claim(mailbox) as taken:
        assert taken
        newer = claimed_at(mailbox)
    assert claimed_at(mailbox) is None
    # a run that outlived its TTL finishes after someone else took over: its release is a no-op
    with services.claim(mailbox) as outer:
        assert outer
        Mailbox.objects.filter(pk=mailbox.pk).update(sync_started_at=timezone.now() + timedelta(seconds=5))
    assert claimed_at(mailbox) is not None
    assert newer is not None


def test_a_fresh_claim_blocks_sync_and_the_refresh_button_says_so(
    client: Client, ak_member: Resident
) -> None:
    mailbox = make_mailbox(roles=["ak"])  # no `server` fixture: touching one.com would fail the test
    hold_claim(mailbox)
    with pytest.raises(services.SyncBusy):
        services.sync(mailbox)
    response = client.post(reverse("postkasser:refresh", args=["ak"]), follow=True)
    assert "Postkassen synkroniseres allerede — prøv igen om lidt." in response.content.decode()


def test_the_beat_command_skips_a_busy_mailbox_quietly(capsys: pytest.CaptureFixture[str]) -> None:
    from django.core.management import call_command

    mailbox = make_mailbox()
    hold_claim(mailbox)
    call_command("sync_mailboxes")
    captured = capsys.readouterr()
    assert "FEJL" not in captured.err and "nye" not in captured.out
    assert Mailbox.objects.get(pk=mailbox.pk).last_sync_error == ""


def test_sync_releases_its_claim_even_when_it_fails(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.error = imap.MailboxError("nede")
    with pytest.raises(imap.MailboxError):
        services.sync(mailbox)
    assert claimed_at(mailbox) is None
    server.error = None
    synced(server, mailbox)
    assert claimed_at(mailbox) is None


def test_an_empty_sag_is_only_stamped_while_a_folder_fails_and_deleted_by_a_clean_run(
    server: FakeServer,
) -> None:
    mailbox = make_mailbox()
    server.add_folder("Fest")
    synced(server, mailbox, raw_mail("Forsvinder"))
    inbox = col(mailbox, ColumnRole.NY)
    orphan = MailThread.objects.create(mailbox=mailbox, subject="Tom", column=inbox)
    pending = MailThread.objects.create(
        mailbox=mailbox, subject="Sendt", column=inbox, pending_message_id="<ny@x>"
    )
    server.take(1)
    server.broken.add("Fest")
    services.sync(mailbox)
    subjects = set(MailThread.objects.values_list("subject", flat=True))
    assert subjects == {"Forsvinder", "Tom", "Sendt"}  # its mail may be in the failing folder
    assert MailThread.objects.get(pk=orphan.pk).emptied_at is not None
    assert MailThread.objects.get(pk=pending.pk).emptied_at is None  # compose is pending
    server.broken.clear()
    services.sync(mailbox)  # every folder answers: now the mail is known to be gone
    assert set(MailThread.objects.values_list("subject", flat=True)) == {"Sendt"}


def test_a_sag_whose_mail_moved_into_a_failing_folder_survives_that_run(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.add_folder("Fest")
    synced(server, mailbox, raw_mail("Flyttet", message_id="<f@x>"))
    sag = MailThread.objects.get()
    server.take(1)
    server.put(1, raw_mail("Flyttet", message_id="<f@x>"), folder="Fest")
    server.broken.add("Fest")
    services.sync(mailbox)
    assert MailThread.objects.filter(pk=sag.pk).exists()  # its mail is probably in the folder that failed


# --- admin inline: really saving -----------------------------------------------------------------------


def columns_formset(  # noqa: ANN201
    mailbox: Mailbox,
    changes: dict[str, dict[str, str]] | None = None,
    extra: list[dict[str, str]] | None = None,
):
    from django.forms import inlineformset_factory

    from postkasser.admin import BoardColumnFormSet

    factory = inlineformset_factory(
        Mailbox, BoardColumn, formset=BoardColumnFormSet, fields=["name", "position", "role"], extra=0
    )
    rows = list(mailbox.columns.all())
    extra = extra or []
    out = {
        "columns-TOTAL_FORMS": str(len(rows) + len(extra)),
        "columns-INITIAL_FORMS": str(len(rows)),
        "columns-MIN_NUM_FORMS": "0",
        "columns-MAX_NUM_FORMS": "1000",
    }
    for i, c in enumerate(rows):
        out |= {f"columns-{i}-id": str(c.pk), f"columns-{i}-mailbox": str(mailbox.pk),
                f"columns-{i}-name": c.name, f"columns-{i}-position": str(c.position), f"columns-{i}-role": c.role}  # fmt: skip
        out |= {f"columns-{i}-{k}": v for k, v in (changes or {}).get(c.name, {}).items()}
    for j, fields in enumerate(extra, start=len(rows)):
        out |= {
            f"columns-{j}-id": "",
            f"columns-{j}-mailbox": str(mailbox.pk),
            f"columns-{j}-position": str(j),
        }
        out |= {f"columns-{j}-{k}": v for k, v in fields.items()}
    return factory(out, instance=mailbox)


def roles_by_name(mailbox: Mailbox) -> dict[str, str]:
    return dict(mailbox.columns.values_list("name", "role"))


def test_the_admin_inline_can_swap_the_roles_of_two_columns() -> None:
    mailbox = make_mailbox()
    formset = columns_formset(mailbox, {"Ny": {"role": "faerdig"}, "Færdig": {"role": "ny"}})
    assert formset.is_valid(), formset.non_form_errors()
    formset.save()  # used to raise IntegrityError on the partial unique index
    assert roles_by_name(mailbox) == {
        "Ny": "faerdig",
        "I gang": "",
        "Afventer svar": "afventer",
        "Færdig": "ny",
    }


def test_the_admin_inline_can_move_a_role_to_an_earlier_column() -> None:
    mailbox = make_mailbox()
    formset = columns_formset(mailbox, {"I gang": {"role": "faerdig"}, "Færdig": {"role": ""}})
    assert formset.is_valid(), formset.non_form_errors()
    formset.save()
    assert roles_by_name(mailbox)["I gang"] == "faerdig" and roles_by_name(mailbox)["Færdig"] == ""
    assert roles_by_name(mailbox)["Ny"] == "ny" and roles_by_name(mailbox)["Afventer svar"] == "afventer"


def test_the_admin_inline_keeps_the_roles_of_rows_it_does_not_touch() -> None:
    mailbox = make_mailbox()
    formset = columns_formset(mailbox, {"I gang": {"name": "Under behandling"}})
    assert formset.is_valid()
    formset.save()
    assert roles_by_name(mailbox) == {
        "Ny": "ny",
        "Under behandling": "",
        "Afventer svar": "afventer",
        "Færdig": "faerdig",
    }


def test_the_admin_inline_deletes_a_column_with_sager_by_moving_them_to_ny() -> None:
    mailbox = make_mailbox()
    sag = make_thread(mailbox, column=mailbox.columns.get(name="I gang"))
    formset = columns_formset(mailbox, {"I gang": {"DELETE": "on"}})
    assert formset.is_valid(), formset.non_form_errors()
    formset.save()  # used to raise RestrictedError
    sag.refresh_from_db()
    assert sag.column == col(mailbox, ColumnRole.NY)
    assert not mailbox.columns.filter(name="I gang").exists()


def test_the_admin_inline_can_replace_the_ny_column_that_holds_sager() -> None:
    mailbox = make_mailbox()
    sag = make_thread(mailbox)  # in Ny
    formset = columns_formset(mailbox, {"Ny": {"DELETE": "on"}, "I gang": {"role": "ny"}})
    assert formset.is_valid(), formset.non_form_errors()
    formset.save()
    sag.refresh_from_db()
    assert sag.column.name == "I gang" and sag.column.role == "ny"
    assert not mailbox.columns.filter(name="Ny").exists()


def test_the_admin_inline_refuses_deleting_a_column_with_sager_when_ny_is_a_new_row() -> None:
    mailbox = make_mailbox()
    make_thread(mailbox)
    formset = columns_formset(mailbox, {"Ny": {"DELETE": "on"}}, extra=[{"name": "Helt ny", "role": "ny"}])
    assert not formset.is_valid()
    assert "sagerne flyttes" in str(formset.non_form_errors())
    assert mailbox.columns.filter(name="Ny").exists()


def test_a_role_swap_through_the_admin_page_itself_saves(client: Client, make_resident: MakeResident) -> None:
    import json

    mailbox = make_mailbox()
    admin = make_resident("adm@intern.dk", roles=(Role.ADMINISTRATOR,))
    Resident.objects.filter(pk=admin.pk).update(is_superuser=True, is_staff=True)
    client.force_login(admin)
    url = reverse("admin:postkasser_mailbox_change", args=[mailbox.pk])
    form = client.get(url).context["adminform"].form
    data: dict[str, object] = {}
    for name, form_field in form.fields.items():
        value = form.initial.get(name, form_field.initial)
        if name == "workgroups":
            continue
        if isinstance(value, bool):
            if value:
                data[name] = "on"
            continue
        data[name] = json.dumps(value) if isinstance(value, list) else ("" if value is None else value)
    rows = list(mailbox.columns.all())
    data |= {
        "columns-TOTAL_FORMS": str(len(rows)),
        "columns-INITIAL_FORMS": str(len(rows)),
        "columns-MIN_NUM_FORMS": "0",
        "columns-MAX_NUM_FORMS": "1000",
    }
    data |= {
        "tags-TOTAL_FORMS": "0",
        "tags-INITIAL_FORMS": "0",
        "tags-MIN_NUM_FORMS": "0",
        "tags-MAX_NUM_FORMS": "1000",
    }
    swapped = {"Ny": "faerdig", "Færdig": "ny"}
    for i, c in enumerate(rows):
        data |= {
            f"columns-{i}-id": str(c.pk),
            f"columns-{i}-mailbox": str(mailbox.pk),
            f"columns-{i}-name": c.name,
            f"columns-{i}-position": str(c.position),
            f"columns-{i}-role": swapped.get(c.name, c.role),
        }
    response = client.post(url, data)
    assert response.status_code == 302
    assert roles_by_name(mailbox)["Ny"] == "faerdig" and roles_by_name(mailbox)["Færdig"] == "ny"


# --- hand-typed ids ------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["²", "٣", "１２", "1_0", " 1", "-1", "9" * 40, "abc"])
def test_unicode_digits_and_junk_are_never_a_500(client: Client, ak_member: Resident, raw: str) -> None:
    mailbox = make_mailbox(roles=["ak"])
    sag = make_thread(mailbox)
    ajax = {"X-Requested-With": "XMLHttpRequest"}
    assert client.get(reverse("postkasser:board", args=["ak"]), {"mappe": raw}).status_code == 200
    status = client.post(reverse("postkasser:set_status", args=["ak", sag.pk]), {"status": raw}, headers=ajax)
    assert status.status_code == 200 and status.json()["ok"] is False
    assign = client.post(
        reverse("postkasser:set_assignee", args=["ak", sag.pk]), {"assignee": raw}, headers=ajax
    )
    assert assign.status_code == 200 and assign.json()["ok"] is False
    url = reverse("postkasser:columns", args=["ak"])
    assert client.post(url, {"action": "rename", "column": raw, "name": "x"}).status_code == 302
    assert client.post(url, {"action": "roles", "role_ny": raw, "role_faerdig": raw}).status_code == 302
    assert (
        client.post(reverse("postkasser:move_thread", args=["ak", sag.pk]), {"mappe": raw}).status_code == 302
    )
    sag.refresh_from_db()
    assert sag.column.role == "ny" and sag.assignee is None


def test_a_refused_move_answers_200_so_the_script_can_show_why(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    sag = make_thread(mailbox)
    response = client.post(
        reverse("postkasser:set_status", args=["ak", sag.pk]),
        {"status": "x"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False and response.json()["error"] == "Ukendt kolonne."


# --- kolonner: races ---------------------------------------------------------------------------------------


def test_deleting_a_column_checks_its_role_on_a_fresh_read() -> None:
    mailbox = make_mailbox()
    stale = mailbox.columns.get(name="I gang")
    columns.set_roles(
        mailbox,
        {"ny": col(mailbox, ColumnRole.NY).pk, "afventer": None, "faerdig": stale.pk},
    )  # somebody else gave it the Færdig role after this page was loaded
    assert stale.role == ""
    with pytest.raises(columns.ColumnError, match="funktionen"):
        columns.delete(stale)
    assert BoardColumn.objects.filter(pk=stale.pk).exists()


def test_deleting_a_column_somebody_else_deleted_is_a_clean_error() -> None:
    mailbox = make_mailbox()
    gone = mailbox.columns.get(name="I gang")
    BoardColumn.objects.filter(pk=gone.pk).delete()
    with pytest.raises(columns.ColumnError, match="findes ikke længere"):
        columns.delete(gone)


def test_a_sag_arriving_during_a_delete_is_a_friendly_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox = make_mailbox()
    victim = mailbox.columns.get(name="I gang")

    def restricted(self: BoardColumn, *args: object, **kwargs: object) -> None:
        raise RestrictedError("held", set())

    monkeypatch.setattr(BoardColumn, "delete", restricted)
    with pytest.raises(columns.ColumnError, match="prøv"):
        columns.delete(victim)


def test_the_columns_page_turns_a_refused_delete_into_a_message(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    ny = col(mailbox, ColumnRole.NY)
    response = client.post(
        reverse("postkasser:columns", args=["ak"]), {"action": "delete", "column": str(ny.pk)}, follow=True
    )
    assert response.status_code == 200 and "Giv funktionen" in response.content.decode()


def test_faerdig_column_repairs_itself_like_ny() -> None:
    mailbox = make_mailbox()
    BoardColumn.objects.filter(mailbox=mailbox, role="faerdig").update(role="")
    repaired = columns.faerdig_column(mailbox)
    assert repaired.role == "faerdig" and repaired.name == "Færdig 2"
    assert columns.faerdig_column(mailbox).pk == repaired.pk
    assert mailbox.columns.get(name="Færdig").role == ""  # nothing that was in use got turned into it


def test_a_first_sync_with_no_faerdig_column_still_works(server: FakeServer) -> None:
    mailbox = make_mailbox()
    BoardColumn.objects.filter(mailbox=mailbox, role="faerdig").update(role="")
    synced(
        server,
        mailbox,
        raw_mail(
            "Læst",
        ),
    )
    server.flags[1] = {r"\Seen"}
    server.put(2, raw_mail("Også læst"), {r"\Seen"})
    server.add_folder("Fest")
    server.put(1, raw_mail("Fest-mail"), {r"\Seen"}, folder="Fest")
    services.sync(mailbox)
    assert MailThread.objects.get(subject="Fest-mail").column.role == "faerdig"


# --- sync details -------------------------------------------------------------------------------------------


def test_a_uidvalidity_reset_counts_as_a_first_sync(server: FakeServer) -> None:
    mailbox = make_mailbox()
    synced(server, mailbox, raw_mail("Gammel"))
    server.put(2, raw_mail("Ny efter nulstilling"), {r"\Seen"})
    server.uidvalidity = 99
    services.sync(mailbox)
    # the sag that is new because of the reset is history, not something for "Ny"
    assert MailThread.objects.get(subject="Ny efter nulstilling").column.role == "faerdig"
    assert MailThread.objects.get(subject="Gammel").column.role == "ny"  # kept by Message-ID, not re-filed


def test_the_name_guess_is_only_made_for_top_level_folders_and_those_under_inbox(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.add_folder("Fest.Spam")
    server.add_folder("INBOX.Spam")
    server.add_folder("Fest.Sendte")
    server.add_folder("Papirkurv")
    synced(server, mailbox)
    uses = {f.path: f.special_use for f in mailbox.folders.all()}
    assert uses["Fest/Spam"] == "" and uses["Fest/Sendte"] == ""
    assert uses["INBOX/Spam"] == "junk" and uses["Papirkurv"] == "trash"


def test_the_sent_folder_prefers_a_top_level_one() -> None:
    mailbox = make_mailbox()
    mailbox.sent_folder = "Findes ikke"
    for name, depth in (("Fest.Sent", 1), ("Sent", 0)):
        MailFolder.objects.create(
            mailbox=mailbox, name=name, display_name="Sent", path=name.replace(".", "/"), depth=depth,
            special_use="sent",
        )  # fmt: skip
    assert services.sent_folder_name(mailbox) == "Sent"


def test_refreshing_folders_saves_only_what_changed() -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    mailbox = make_mailbox()
    infos = [
        imap.FolderInfo("INBOX", ".", frozenset()),
        imap.FolderInfo("Fest", ".", frozenset()),
    ]
    services._refresh_folders(services._Run(mailbox), infos)
    with CaptureQueriesContext(connection) as queries:
        services._refresh_folders(services._Run(mailbox), infos)
    writes = [q["sql"] for q in queries.captured_queries if q["sql"].startswith(("UPDATE", "INSERT"))]
    assert writes == []
    renamed = [infos[0], imap.FolderInfo("Fest", "/", frozenset({r"\noselect"}))]
    services._refresh_folders(services._Run(mailbox), renamed)
    fest = mailbox.folders.get(name="Fest")
    assert fest.delimiter == "/" and not fest.selectable


def test_mailmessage_is_indexed_by_message_id() -> None:
    assert any(index.fields == ["mailbox", "message_id"] for index in MailMessage._meta.indexes)


def test_a_faerdig_sag_without_a_message_date_is_on_the_board_or_in_the_archive_never_both(
    client: Client, ak_member: Resident
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    faerdig = col(mailbox, ColumnRole.FAERDIG)
    recent = make_thread(mailbox, "Nylig uden dato", column=faerdig)
    stale = make_thread(mailbox, "Gammel uden dato", column=faerdig)
    MailThread.objects.filter(pk__in=[recent.pk, stale.pk]).update(last_message_at=None)
    MailThread.objects.filter(pk=stale.pk).update(updated_at=timezone.now() - timedelta(days=90))
    on_board = subjects_in(client, faerdig, mailbox)
    in_archive = [c.subject for c in client.get(reverse("postkasser:archive", args=["ak"])).context["page"]]
    assert on_board == ["Nylig uden dato"]
    assert in_archive == ["Gammel uden dato"]


def test_the_archive_builds_cards_for_one_page_only(
    client: Client, ak_member: Resident, monkeypatch: pytest.MonkeyPatch
) -> None:
    from postkasser import views

    mailbox = make_mailbox(roles=["ak"])
    faerdig = col(mailbox, ColumnRole.FAERDIG)
    for i in range(5):
        make_thread(mailbox, f"Arkiv {i}", column=faerdig, age_days=100 + i)
    monkeypatch.setattr(views, "PAGE_SIZE", 2)
    built: list[int] = []
    original = views._cards

    def counting(threads):  # noqa: ANN001, ANN202
        cards = original(threads)
        built.append(len(cards))
        return cards

    monkeypatch.setattr(views, "_cards", counting)
    response = client.get(reverse("postkasser:archive", args=["ak"]), {"side": "2"})
    assert built == [2]
    assert [c.subject for c in response.context["page"]] == ["Arkiv 2", "Arkiv 3"]


def test_a_failed_sent_copy_after_compose_says_how_long_the_sag_is_kept(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    make_mailbox(roles=["ak"])
    server.append_error = imap.MailboxError("quota")
    response = client.post(
        reverse("postkasser:compose", args=["ak"]),
        {"to": "a@example.com", "subject": "Nyt", "body": "Hej"},
        follow=True,
    )
    page = response.content.decode()
    assert "Sagen beholdes i 2 dage, medmindre kopien dukker op eller nogen svarer." in page


# --- moving mail: the wire ---------------------------------------------------------------------------------


def compress_uids(uids: list[int]) -> str:
    runs: list[list[int]] = []
    for uid in sorted(uids):
        if runs and runs[-1][1] == uid - 1:
            runs[-1][1] = uid
        else:
            runs.append([uid, uid])
    return ",".join(str(a) if a == b else f"{a}:{b}" for a, b in runs)


class FakeImapd:
    """A minimal IMAP server on a local socket, so that `imap.Connection.move` is exercised through the
    REAL imaplib of the running Python (3.13 in production, 3.14 in dev) and what goes on the wire is what
    the test asserts. Speaks CAPABILITY, LOGIN, SELECT, LOGOUT and UID MOVE / COPY / STORE / EXPUNGE;
    `commands` is every line received. A plain EXPUNGE deletes every \\Deleted mail, as a real server
    would — which is exactly what must never be sent."""

    def __init__(
        self, capabilities: str, folders: dict[str, dict[int, set[str]]], *, copyuid: bool = True
    ) -> None:
        self.capabilities = capabilities
        self.folders = folders
        self.copyuid = copyuid
        self.uidvalidity = dict.fromkeys(folders, 7) | {"Fest": 42}
        self.commands: list[str] = []
        self.server = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), self._handler(), bind_and_activate=True
        )
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self) -> type[socketserver.StreamRequestHandler]:
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                outer.serve(self)

        return Handler

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def _name(text: str) -> str:
        match = re.fullmatch(r'"((?:[^"\\]|\\.)*)"', text.strip())
        return re.sub(r"\\(.)", r"\1", match.group(1)) if match else text.strip()

    def serve(self, handler: socketserver.StreamRequestHandler) -> None:
        def send(line: str) -> None:
            handler.wfile.write(line.encode() + b"\r\n")
            handler.wfile.flush()

        selected = ""
        send("* OK fake imapd ready")
        for raw in handler.rfile:
            line = raw.decode().rstrip("\r\n")
            self.commands.append(line)
            tag, _, rest = line.partition(" ")
            word, _, args = rest.partition(" ")
            word = word.upper()
            if word == "CAPABILITY":
                send(f"* CAPABILITY {self.capabilities}")
                send(f"{tag} OK done")
            elif word == "LOGIN":
                send(f"{tag} OK LOGIN done")
            elif word == "SELECT":
                selected = self._name(args)
                if selected not in self.folders:
                    send(f"{tag} NO [NONEXISTENT] no such folder")
                    continue
                send(f"* {len(self.folders[selected])} EXISTS")
                send(f"* OK [UIDVALIDITY {self.uidvalidity[selected]}] ok")
                send(f"{tag} OK [READ-WRITE] done")
            elif word == "EXPUNGE":  # the unsafe one
                for uid in [u for u, f in self.folders[selected].items() if r"\Deleted" in f]:
                    del self.folders[selected][uid]
                send(f"{tag} OK done")
            elif word == "UID":
                self.uid_command(send, tag, selected, args)
            elif word == "LOGOUT":
                send("* BYE")
                send(f"{tag} OK bye")
                return
            else:
                send(f"{tag} BAD unknown {word}")

    def uid_command(self, send: Callable[[str], None], tag: str, selected: str, args: str) -> None:
        sub, _, rest = args.partition(" ")
        sub = sub.upper()
        uidset, _, tail = rest.partition(" ")
        known = {"MOVE", "COPY", "STORE", "EXPUNGE"}
        needed = {"MOVE": "MOVE", "EXPUNGE": "UIDPLUS"}.get(sub)
        if sub not in known or (needed and needed not in self.capabilities.split()):
            send(f"{tag} BAD command not available")
            return
        source = self.folders[selected]
        uids = [u for u in imap.expand_uid_set(uidset.encode()) if u in source]
        if sub in {"MOVE", "COPY"}:
            target = self._name(tail)
            if target not in self.folders:
                send(f"{tag} NO [TRYCREATE] no such folder")
                return
            destination = self.folders[target]
            mapping = {}
            for uid in uids:
                new = max(destination, default=0) + 1
                destination[new] = set(source[uid])
                mapping[uid] = new
            code = (
                f"[COPYUID {self.uidvalidity[target]} {compress_uids(list(mapping))} "
                f"{compress_uids(list(mapping.values()))}] "
                if self.copyuid and mapping
                else ""
            )
            if sub == "MOVE":
                if code:
                    send(f"* OK {code}moved")
                for n, uid in enumerate(uids, start=1):
                    del source[uid]
                    send(f"* {n} EXPUNGE")
                send(f"{tag} OK MOVE done")
            else:
                send(f"{tag} OK {code}COPY done")
        elif sub == "STORE":
            for uid in uids:
                source[uid].add(r"\Deleted")
            send(f"{tag} OK STORE done")
        else:  # UID EXPUNGE
            for n, uid in enumerate([u for u in uids if r"\Deleted" in source[u]], start=1):
                del source[uid]
                send(f"* {n} EXPUNGE")
            send(f"{tag} OK EXPUNGE done")


@pytest.fixture
def wire() -> Iterator[Callable[..., tuple[FakeImapd, imap.Connection]]]:
    daemons: list[FakeImapd] = []
    clients: list[imaplib.IMAP4] = []

    def make(
        capabilities: str = "IMAP4rev1 MOVE UIDPLUS",
        folders: dict[str, dict[int, set[str]]] | None = None,
        *,
        copyuid: bool = True,
    ) -> tuple[FakeImapd, imap.Connection]:
        daemon = FakeImapd(
            capabilities,
            folders if folders is not None else {"INBOX": {1: set(), 2: set(), 3: set()}, "Fest": {}},
            copyuid=copyuid,
        )
        daemons.append(daemon)
        client = imaplib.IMAP4("127.0.0.1", daemon.port, timeout=5)
        clients.append(client)
        client.login("ak@gahk.dk", "hemmeligt")
        return daemon, imap.Connection(client)

    yield make
    for client in clients:
        try:
            client.logout()
        except (OSError, imaplib.IMAP4.error):
            pass
    for daemon in daemons:
        daemon.stop()


def mutating(daemon: FakeImapd) -> list[str]:
    return [c for c in daemon.commands if re.search(r" (UID (MOVE|COPY|STORE|EXPUNGE)|EXPUNGE)\b", c)]


def test_move_uses_uid_move_when_the_server_has_it(
    wire: Callable[..., tuple[FakeImapd, imap.Connection]],
) -> None:
    daemon, conn = wire()
    conn.select("INBOX")
    result = conn.move([3, 1], "Fest")
    assert [c.split(" ", 1)[1] for c in mutating(daemon)] == ['UID MOVE 1,3 "Fest"']
    assert result == imap.MoveResult(uidvalidity=42, mapping={1: 1, 3: 2})
    assert sorted(daemon.folders["INBOX"]) == [2] and sorted(daemon.folders["Fest"]) == [1, 2]


def test_move_reads_a_copyuid_range_from_the_answer(
    wire: Callable[..., tuple[FakeImapd, imap.Connection]],
) -> None:
    _daemon, conn = wire()
    conn.select("INBOX")
    result = conn.move([1, 2, 3], "Fest")
    assert result.mapping == {1: 1, 2: 2, 3: 3} and result.uidvalidity == 42


def test_move_without_uidplus_or_move_is_refused_and_changes_nothing(
    wire: Callable[..., tuple[FakeImapd, imap.Connection]],
) -> None:
    daemon, conn = wire("IMAP4rev1")
    conn.select("INBOX")
    with pytest.raises(imap.MailboxError, match="understøtter ikke sikker flytning"):
        conn.move([1], "Fest")
    assert mutating(daemon) == []  # in particular: no plain EXPUNGE as a fallback
    assert sorted(daemon.folders["INBOX"]) == [1, 2, 3] and daemon.folders["Fest"] == {}


def test_the_uidplus_fallback_only_expunges_the_given_uids(
    wire: Callable[..., tuple[FakeImapd, imap.Connection]],
) -> None:
    daemon, conn = wire("IMAP4rev1 UIDPLUS", {"INBOX": {1: set(), 2: {r"\Deleted"}, 3: set()}, "Fest": {}})
    conn.select("INBOX")
    result = conn.move([1, 3], "Fest")
    assert [c.split(" ", 1)[1] for c in mutating(daemon)] == [
        'UID COPY 1,3 "Fest"',
        r"UID STORE 1,3 +FLAGS.SILENT (\Deleted)",
        "UID EXPUNGE 1,3",
    ]
    assert daemon.folders["INBOX"] == {2: {r"\Deleted"}}  # somebody else's flagged mail survived
    assert sorted(daemon.folders["Fest"]) == [1, 2]
    assert result.mapping == {1: 1, 3: 2} and result.uidvalidity == 42  # COPYUID from the TAGGED answer


def test_a_server_that_says_nothing_about_copyuid_gives_an_empty_mapping(
    wire: Callable[..., tuple[FakeImapd, imap.Connection]],
) -> None:
    for capabilities in ("IMAP4rev1 MOVE", "IMAP4rev1 UIDPLUS"):
        daemon, conn = wire(capabilities, copyuid=False)
        conn.select("INBOX")
        assert conn.move([1], "Fest") == imap.MoveResult()
        assert sorted(daemon.folders["INBOX"]) == [2, 3]


def test_a_refused_move_is_a_mailbox_error_and_nothing_moves(
    wire: Callable[..., tuple[FakeImapd, imap.Connection]],
) -> None:
    daemon, conn = wire()
    conn.select("INBOX")
    with pytest.raises(imap.MailboxError, match="TRYCREATE"):
        conn.move([1], "Findes ikke")
    assert sorted(daemon.folders["INBOX"]) == [1, 2, 3]


def test_the_target_is_quoted_in_modified_utf7_on_the_wire(
    wire: Callable[..., tuple[FakeImapd, imap.Connection]],
) -> None:
    name = utf7.encode("Fest/Ærø & Åben")
    daemon, conn = wire(folders={"INBOX": {1: set()}, name: {}})
    conn.select("INBOX")
    conn.move([1], name)
    assert name.isascii() and "&AMY-" in name
    assert mutating(daemon)[0].endswith(f'UID MOVE 1 "{name}"')
    assert daemon.folders[name] and not daemon.folders["INBOX"]
    with pytest.raises(imap.MailboxError, match="ASCII"):
        conn.move([1], "Fest/Ærø")  # a name that was never encoded is refused, not sent as garbage


def test_the_capability_is_asked_once_per_connection(
    wire: Callable[..., tuple[FakeImapd, imap.Connection]],
) -> None:
    daemon, conn = wire()
    conn.select("INBOX")
    before = len([c for c in daemon.commands if " CAPABILITY" in c])  # imaplib's own, at connect/login
    conn.move([1], "Fest")
    conn.move([2], "Fest")
    assert len([c for c in daemon.commands if " CAPABILITY" in c]) == before + 1  # not one per move


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (b"4:6,9", [4, 5, 6, 9]),
        (b"7", [7]),
        (b"6:4", [6, 5, 4]),
        (b"1,3:4,8:8", [1, 3, 4, 8]),
    ],
)
def test_uid_sets_expand(text: bytes, expected: list[int]) -> None:
    assert imap.expand_uid_set(text) == expected


def test_copyuid_is_parsed_from_tagged_text_and_from_untagged_codes() -> None:
    parsed = imap.parse_copyuid([b"[COPYUID 38505 304,319:320 3956:3958] Done"])
    assert parsed == imap.MoveResult(uidvalidity=38505, mapping={304: 3956, 319: 3957, 320: 3958})
    assert imap.parse_copyuid([b"nothing", b"COPYUID 9 1:2 5,7"]).mapping == {1: 5, 2: 7}
    assert imap.parse_copyuid([b"[COPYUID 1 1:3 5:6]"]) == imap.MoveResult()  # lengths differ: not trusted
    assert imap.parse_copyuid([b"[COPYUID 1 1:99999999 1:99999999]"]) == imap.MoveResult()  # absurd
    assert imap.parse_copyuid([]) == imap.MoveResult()


# --- moving mail: services and views ---------------------------------------------------------------------


def move_setup(server: FakeServer, *folders: str) -> tuple[Mailbox, MailThread]:
    """A mailbox whose INBOX holds one mail (one sag), with the folders `Fest`, `Arkiv` and a Trash."""
    mailbox = make_mailbox(roles=["ak"])
    for name in folders or ("Fest", "Arkiv"):
        server.add_folder(name)
    server.add_folder("Trash", r"\Trash")
    synced(server, mailbox, raw_mail("Kælderen", message_id="<k@x>"))
    return mailbox, MailThread.objects.get()


def folder_of(mailbox: Mailbox, name: str) -> MailFolder:
    return mailbox.folders.get(name=name)


def move_url(mailbox: Mailbox, sag: MailThread) -> str:
    return reverse("postkasser:move_thread", args=[mailbox.slug, sag.pk])


def test_moving_a_sag_with_copyuid_updates_the_rows_in_place_and_keeps_the_column(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    gang = mailbox.columns.get(name="I gang")
    MailThread.objects.filter(pk=sag.pk).update(column=gang, assignee=ak_member)
    row = MailMessage.objects.get()
    response = client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)}, follow=True)
    assert "1 mail flyttet til «Fest»" in response.content.decode()
    assert server.moves == [("INBOX", [1], "Fest")]
    moved = MailMessage.objects.get()
    assert moved.pk == row.pk and moved.folder.name == "Fest" and moved.uid == 1  # updated, not re-created
    sag.refresh_from_db()
    assert sag.column == gang and sag.assignee == ak_member and moved.thread == sag
    assert MailThread.objects.count() == 1


def test_moving_without_copyuid_syncs_the_target_and_the_sag_keeps_its_column(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    server.copyuid = False
    gang = mailbox.columns.get(name="I gang")
    MailThread.objects.filter(pk=sag.pk).update(column=gang)
    old = MailMessage.objects.get()
    client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)})
    new = MailMessage.objects.get()
    assert new.pk != old.pk and new.folder.name == "Fest" and new.thread_id == sag.pk
    assert MailThread.objects.get().column == gang and MailThread.objects.count() == 1
    assert server.selected[-1] == "Fest"  # the target was synced


def test_a_copyuid_for_another_uidvalidity_is_not_trusted(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    server.folders["Fest"].uidvalidity = 5  # the server renumbered Fest since the last sync
    client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)})
    moved = MailMessage.objects.get()
    assert moved.folder.name == "Fest" and moved.folder.uidvalidity == 5
    assert MailThread.objects.get().pk == sag.pk  # re-joined by Message-ID


def test_moving_to_papirkurv_deletes_the_rows_and_the_sag(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    trash = folder_of(mailbox, "Trash")
    page = client.get(reverse("postkasser:thread", args=["ak", sag.pk])).content.decode()
    assert "Papirkurv" in page and "data-confirm" in page and "fjernes fra boardet" in page
    response = client.post(move_url(mailbox, sag), {"mappe": str(trash.pk)}, follow=True)
    assert server.moves == [("INBOX", [1], "Trash")]
    assert MailMessage.objects.count() == 0 and MailThread.objects.count() == 0
    assert response.redirect_chain[-1][0] == reverse("postkasser:board", args=["ak"])
    assert "fjernet fra tavlen" in response.content.decode()
    event = MailEvent.objects.get(action=MailEvent.Action.MOVE)
    assert event.detail == "Papirkurv" and event.subject == "Kælderen" and event.thread is None


def test_a_sag_with_only_sent_mail_left_goes_with_a_move_to_the_trash(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    server.put(1, raw_mail("Re: Kælderen", sender="ak@gahk.dk", in_reply_to="<k@x>"), folder="Sent")
    services.sync(mailbox)
    assert mailbox.messages.filter(thread=sag).count() == 2
    client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Trash").pk)})
    assert server.moves == [("INBOX", [1], "Trash")]  # the sent copy stayed where it is
    assert MailThread.objects.count() == 0
    assert list(MailMessage.objects.values_list("folder__name", "thread")) == [("Sent", None)]


def test_sent_mail_is_never_moved(client: Client, ak_member: Resident, server: FakeServer) -> None:
    mailbox, sag = move_setup(server)
    server.put(1, raw_mail("Re: Kælderen", sender="ak@gahk.dk", in_reply_to="<k@x>"), folder="Sent")
    services.sync(mailbox)
    fest = str(folder_of(mailbox, "Fest").pk)
    response = client.post(move_url(mailbox, sag), {"mappe": fest}, follow=True)
    assert server.moves == [("INBOX", [1], "Fest")]
    assert "Sendte mails" in response.content.decode()
    assert mailbox.messages.get(folder__name="Sent").thread_id == sag.pk
    sent = mailbox.messages.get(folder__name="Sent")
    only_sent = client.post(
        reverse("postkasser:move_message", args=["ak", sent.pk]), {"mappe": fest}, follow=True
    )
    assert server.moves == [("INBOX", [1], "Fest")]
    assert "ingen mails at flytte" in only_sent.content.decode()


def test_the_targets_exclude_sent_drafts_and_where_everything_already_is(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    server.add_folder("Drafts", r"\Drafts")
    services.sync(mailbox)
    names = lambda r: [f.move_label for f in r.context["move_targets"]]  # noqa: E731
    page = client.get(reverse("postkasser:thread", args=["ak", sag.pk]))
    assert names(page) == ["Arkiv", "Fest", "Papirkurv"]  # not the Indbakke the mail is already in
    assert "Sendt" not in names(page) and "Drafts" not in names(page)
    mail = MailMessage.objects.get()
    assert "Indbakke" not in names(client.get(reverse("postkasser:message", args=["ak", mail.pk])))


def test_a_message_move_logs_an_event_for_the_mail(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    mail = MailMessage.objects.get()
    client.post(
        reverse("postkasser:move_message", args=["ak", mail.pk]),
        {"mappe": str(folder_of(mailbox, "Arkiv").pk)},
    )
    event = MailEvent.objects.get(action=MailEvent.Action.MOVE)
    assert (
        event.detail == "Arkiv"
        and event.message == mail
        and event.thread == sag
        and event.resident == ak_member
    )
    assert event.get_action_display() == "Flyttet til mappe"
    page = client.get(reverse("postkasser:thread", args=["ak", sag.pk])).content.decode()
    assert "Flyttet til mappe Arkiv" in page


def test_a_move_of_one_of_two_mails_leaves_the_sag_with_both_in_different_folders(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    server.put(2, raw_mail("Re: Kælderen", in_reply_to="<k@x>"))
    services.sync(mailbox)
    second = mailbox.messages.get(uid=2)
    client.post(
        reverse("postkasser:move_message", args=["ak", second.pk]),
        {"mappe": str(folder_of(mailbox, "Fest").pk)},
    )
    assert sorted(mailbox.messages.filter(thread=sag).values_list("folder__name", flat=True)) == [
        "Fest",
        "INBOX",
    ]
    assert MailThread.objects.count() == 1


def test_a_partial_failure_across_two_source_folders_is_reported(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    server.put(1, raw_mail("Re: Kælderen", in_reply_to="<k@x>"), folder="Fest")
    services.sync(mailbox)
    server.refuse_moves_from.add("Fest")
    response = client.post(
        move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Arkiv").pk)}, follow=True
    )
    page = response.content.decode()
    assert "1 mail flyttet til «Arkiv»" in page and "Kunne ikke flytte mails fra «Fest»" in page
    assert sorted(mailbox.messages.values_list("folder__name", flat=True)) == ["Arkiv", "Fest"]
    assert MailThread.objects.get().pk == sag.pk
    assert MailEvent.objects.filter(action=MailEvent.Action.MOVE).count() == 1


def test_a_server_that_cannot_move_safely_changes_nothing(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    server.move_error = imap.MailboxError("Serveren understøtter ikke sikker flytning af mails.")
    response = client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)}, follow=True)
    assert "understøtter ikke sikker flytning" in response.content.decode()
    assert (
        MailMessage.objects.get().folder.name == "INBOX"
        and not MailEvent.objects.filter(action="move").exists()
    )
    assert claimed_at(mailbox) is None


def test_a_move_refuses_a_stale_cache_row_that_is_already_in_the_target(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    response = client.post(
        move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "INBOX").pk)}, follow=True
    )
    assert "ingen mails at flytte" in response.content.decode() and server.moves == []


def test_the_move_endpoints_are_scoped_to_the_mailbox_and_post_only(
    client: Client, ak_member: Resident, make_resident: MakeResident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    other = make_mailbox("fest", name="Festgruppen", roles=["ak"])
    foreign = MailFolder.objects.create(mailbox=other, name="Fremmed", display_name="Fremmed", path="Fremmed")
    mail = MailMessage.objects.get()
    fest = str(folder_of(mailbox, "Fest").pk)
    for url in (move_url(mailbox, sag), reverse("postkasser:move_message", args=["ak", mail.pk])):
        assert client.post(url, {"mappe": str(foreign.pk)}).status_code == 404  # another mailbox's folder
        assert client.post(url, {"mappe": "999999"}).status_code == 404
        assert client.get(url).status_code == 405
    wrong_slug = reverse("postkasser:move_thread", args=["fest", sag.pk])
    assert client.post(wrong_slug, {"mappe": str(foreign.pk)}).status_code == 404
    wrong_message = reverse("postkasser:move_message", args=["fest", mail.pk])
    assert client.post(wrong_message, {"mappe": str(foreign.pk)}).status_code == 404
    stranger = make_resident("fremmed@intern.dk")
    client.force_login(stranger)
    assert client.post(move_url(mailbox, sag), {"mappe": fest}).status_code == 404
    assert server.moves == [] and MailMessage.objects.get().folder.name == "INBOX"


def test_the_move_forms_need_a_csrf_token(make_resident: MakeResident, server: FakeServer) -> None:
    mailbox, sag = move_setup(server)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(make_resident("ak@intern.dk", roles=(Role.AK,)))
    response = strict.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)})
    assert response.status_code == 403 and server.moves == []


def test_a_move_without_a_chosen_folder_is_a_message_not_a_500(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    response = client.post(move_url(mailbox, sag), {}, follow=True)
    assert response.status_code == 200 and "Vælg en mappe" in response.content.decode()


def test_the_sag_and_message_pages_show_where_the_mail_is(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    _mailbox, sag = move_setup(server)
    mail = MailMessage.objects.get()
    assert "Nu i: Indbakke" in client.get(reverse("postkasser:thread", args=["ak", sag.pk])).content.decode()
    page = client.get(reverse("postkasser:message", args=["ak", mail.pk])).content.decode()
    assert "<strong>Mappe:</strong> Indbakke" in page and "Flyt til mappe" in page


# --- moving mail: concurrency with a sync ----------------------------------------------------------------


def test_a_sync_cannot_run_in_the_middle_of_a_move(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    """The mail has left the old folder and the cache does not know where it went yet: exactly when a
    sync in another process would delete the sag as empty. The move holds the same claim, so it is
    refused (and the beat job tries again) instead."""
    mailbox, sag = move_setup(server)
    server.copyuid = False  # the slow path, where the cache is behind the longest
    seen: list[str] = []

    def beat_tick() -> None:
        try:
            services.sync(mailbox)
        except services.SyncBusy:
            seen.append("busy")

    server.during_move = beat_tick
    client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)})
    assert seen == ["busy"]
    assert MailThread.objects.get().pk == sag.pk and MailMessage.objects.get().folder.name == "Fest"
    assert claimed_at(mailbox) is None
    services.sync(mailbox)  # and afterwards syncs run again
    assert MailThread.objects.get().pk == sag.pk


def test_a_move_is_refused_while_a_sync_runs(client: Client, ak_member: Resident, server: FakeServer) -> None:
    mailbox, sag = move_setup(server)
    hold_claim(mailbox)
    response = client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)}, follow=True)
    assert "Postkassen synkroniseres allerede" in response.content.decode()
    assert server.moves == [] and MailMessage.objects.get().folder.name == "INBOX"
    assert claimed_at(mailbox) is not None  # and the refused move did not release the other run's claim


def test_a_failed_target_sync_after_a_move_keeps_the_sag_for_the_next_sync(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    server.copyuid = False
    server.broken.add("Fest")
    response = client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)}, follow=True)
    assert "kunne ikke hentes endnu" in response.content.decode()
    server.broken.clear()
    services.sync(mailbox)
    assert MailThread.objects.get().pk == sag.pk and MailMessage.objects.get().folder.name == "Fest"


# --- tags (kategorier) on sager --------------------------------------------------------------------------------


def make_tag(mailbox: Mailbox, name: str = "Faktura", color: str = "roed") -> MailTag:
    return tags.add(mailbox, name, color)


def tags_url(mailbox: Mailbox) -> str:
    return reverse("postkasser:tags", args=[mailbox.slug])


def post_tag_action(client: Client, mailbox: Mailbox, **data: str) -> str:
    """POST to the Tags page and return what the page says afterwards (messages included)."""
    return client.post(tags_url(mailbox), data, follow=True).content.decode()


def test_the_tags_page_adds_renames_recolours_reorders_and_deletes(
    client: Client, ak_member: Resident
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    assert client.get(tags_url(mailbox)).status_code == 200
    post_tag_action(client, mailbox, action="add", name="  Faktura ", color="roed")
    post_tag_action(client, mailbox, action="add", name="Brygger", color="blaa")
    faktura, brygger = mailbox.tags.all()
    assert (faktura.name, faktura.color, faktura.position) == ("Faktura", "roed", 0)
    assert (brygger.name, brygger.color, brygger.position) == ("Brygger", "blaa", 1)
    post_tag_action(client, mailbox, action="rename", tag=str(brygger.pk), name="Bryggerne")
    post_tag_action(client, mailbox, action="color", tag=str(brygger.pk), color="groen")
    post_tag_action(client, mailbox, action="up", tag=str(brygger.pk))
    assert list(mailbox.tags.values_list("name", "color")) == [("Bryggerne", "groen"), ("Faktura", "roed")]
    post_tag_action(client, mailbox, action="up", tag=str(brygger.pk))  # at the top: nothing happens
    assert mailbox.tags.first().name == "Bryggerne"  # type: ignore[union-attr]
    post_tag_action(client, mailbox, action="delete", tag=str(faktura.pk))
    assert list(mailbox.tags.values_list("name", flat=True)) == ["Bryggerne"]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"action": "add", "name": "faktura"}, "allerede et tag"),  # casefolded duplicate
        ({"action": "add", "name": "FÆRDIG-ish"}, None),
        ({"action": "add", "name": "   "}, "skal have et navn"),
        ({"action": "add", "name": "x" * 41}, "højst være 40"),
        ({"action": "add", "name": "Ny", "color": "turkis"}, "Ukendt farve"),
        ({"action": "rename", "name": "Andet", "tag": "999999"}, "Ukendt tag"),
        ({"action": "rename", "name": "Andet", "tag": "²"}, "Ukendt tag"),
        ({"action": "color", "color": "roed", "tag": ""}, "Ukendt tag"),
        ({"action": "sletalt"}, "Ukendt tag"),
    ],
)
def test_the_tags_page_refuses_nonsense_without_a_500(
    client: Client, ak_member: Resident, data: dict[str, str], message: str | None
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    make_tag(mailbox, "Faktura")
    page = post_tag_action(client, mailbox, **data)
    if message:
        assert message in page
    assert mailbox.tags.filter(name="Faktura").count() == 1


def test_renaming_to_another_tags_name_is_refused_but_to_its_own_case_is_fine(
    client: Client, ak_member: Resident
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    faktura, brygger = make_tag(mailbox, "Faktura"), make_tag(mailbox, "Brygger")
    assert "allerede et tag" in post_tag_action(
        client, mailbox, action="rename", tag=str(brygger.pk), name="FAKTURA"
    )
    post_tag_action(client, mailbox, action="rename", tag=str(faktura.pk), name="FAKTURA")
    faktura.refresh_from_db()
    assert faktura.name == "FAKTURA"


def test_two_mailboxes_may_use_the_same_tag_name() -> None:
    first, second = make_mailbox("ak"), make_mailbox("fest", name="Festgruppen")
    make_tag(first, "Faktura")
    make_tag(second, "Faktura")
    assert MailTag.objects.count() == 2


def test_a_tag_that_is_gone_is_a_clean_error() -> None:
    mailbox = make_mailbox()
    tag = make_tag(mailbox)
    MailTag.objects.filter(pk=tag.pk).delete()
    for action in (
        lambda: tags.rename(tag, "x"),
        lambda: tags.recolor(tag, "roed"),
        lambda: tags.move(tag, 1),
        lambda: tags.delete(tag),
    ):
        with pytest.raises(tags.TagError, match="findes ikke længere"):
            action()


def test_deleting_a_tag_removes_it_from_the_sager_but_keeps_them(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    tag = make_tag(mailbox)
    sag = make_thread(mailbox)
    sag.tags.add(tag)
    page = client.get(tags_url(mailbox)).content.decode()
    assert "1 sag" in page and "sagerne slettes ikke" in page  # the confirmation says how many
    assert "fra 1 sag" in post_tag_action(client, mailbox, action="delete", tag=str(tag.pk))
    assert MailThread.objects.filter(pk=sag.pk).exists() and sag.tags.count() == 0


def test_tagging_a_sag_sets_exactly_the_ticked_tags_and_logs_it(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    faktura, brygger, _extra = (
        make_tag(mailbox, "Faktura"),
        make_tag(mailbox, "Brygger"),
        make_tag(mailbox, "Fest"),
    )
    sag = make_thread(mailbox)
    url = reverse("postkasser:set_tags", args=["ak", sag.pk])
    page = client.get(reverse("postkasser:thread", args=["ak", sag.pk])).content.decode()
    assert "Gem tags" in page and f'value="{faktura.pk}"' in page
    client.post(url, {"tags": [str(faktura.pk), str(brygger.pk)]})
    assert set(sag.tags.values_list("name", flat=True)) == {"Faktura", "Brygger"}
    event = MailEvent.objects.get(action=MailEvent.Action.TAG)
    assert event.detail == "Faktura, Brygger" and event.thread == sag and event.resident == ak_member
    client.post(url, {"tags": [str(faktura.pk)]})
    assert list(sag.tags.values_list("name", flat=True)) == ["Faktura"]
    client.post(url, {"tags": [str(faktura.pk)]})  # unchanged: no new event
    assert MailEvent.objects.filter(action=MailEvent.Action.TAG).count() == 2
    client.post(url, {})
    assert sag.tags.count() == 0 and MailEvent.objects.filter(action="tag").first().detail == "ingen"  # type: ignore[union-attr]
    shown = client.get(reverse("postkasser:thread", args=["ak", sag.pk])).content.decode()
    assert "Tags Faktura" in shown


def test_only_this_mailboxs_tags_are_ever_applied(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    own = make_tag(mailbox, "Faktura")
    foreign = make_tag(make_mailbox("fest", name="Festgruppen", roles=["ak"]), "Fremmed")
    sag = make_thread(mailbox)
    url = reverse("postkasser:set_tags", args=["ak", sag.pk])
    for junk in ("²", "abc", "", "9" * 40, "999999"):
        assert client.post(url, {"tags": [str(own.pk), junk]}).status_code == 302
    client.post(url, {"tags": [str(foreign.pk)]})
    assert list(sag.tags.all()) == []  # the last post asked for nothing valid: the set is now empty
    client.post(url, {"tags": [str(own.pk), str(foreign.pk)]})
    assert list(sag.tags.all()) == [own]
    assert foreign.threads.count() == 0


def test_the_tag_endpoints_are_scoped_and_post_only(
    client: Client, ak_member: Resident, make_resident: MakeResident
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    other = make_mailbox("fest", name="Festgruppen", roles=["ak"])
    tag, sag = make_tag(mailbox), make_thread(mailbox)
    url = reverse("postkasser:set_tags", args=["ak", sag.pk])
    assert client.get(url).status_code == 405
    assert client.get(reverse("postkasser:tags", args=["fest"])).status_code == 200  # ak role opens fest too
    assert (
        client.post(
            reverse("postkasser:set_tags", args=["fest", sag.pk]), {"tags": [str(tag.pk)]}
        ).status_code
        == 404
    )
    assert (
        client.post(tags_url(other), {"action": "delete", "tag": str(tag.pk)}, follow=True).status_code == 200
    )
    assert MailTag.objects.filter(pk=tag.pk).exists()  # another mailbox's page cannot touch it
    client.force_login(make_resident("fremmed@intern.dk"))
    assert client.get(tags_url(mailbox)).status_code == 404
    assert client.post(tags_url(mailbox), {"action": "add", "name": "X"}).status_code == 404
    assert client.post(url, {"tags": [str(tag.pk)]}).status_code == 404
    assert sag.tags.count() == 0


def test_the_tag_forms_need_a_csrf_token(make_resident: MakeResident) -> None:
    mailbox = make_mailbox()
    sag = make_thread(mailbox)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(make_resident("ak@intern.dk", roles=(Role.AK,)))
    assert strict.post(tags_url(mailbox), {"action": "add", "name": "X"}).status_code == 403
    assert strict.post(reverse("postkasser:set_tags", args=["ak", sag.pk]), {}).status_code == 403


def test_the_board_shows_tags_and_filters_by_them(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    faktura, brygger = make_tag(mailbox, "Faktura"), make_tag(mailbox, "Brygger")
    tagged, other, plain = (
        make_thread(mailbox, "Regning"),
        make_thread(mailbox, "Øl"),
        make_thread(mailbox, "Hej"),
    )
    tagged.tags.add(faktura)
    other.tags.add(brygger)
    ny = col(mailbox, ColumnRole.NY)
    page = board(client)
    assert "chip-tag-roed" in page and "Faktura" in page and 'name="tag"' in page
    assert sorted(subjects_in(client, ny, mailbox)) == ["Hej", "Regning", "Øl"]
    assert subjects_in(client, ny, mailbox, tag=str(faktura.pk)) == ["Regning"]
    assert subjects_in(client, ny, mailbox, tag=str(brygger.pk), q="Øl") == ["Øl"]
    assert subjects_in(client, ny, mailbox, tag=str(brygger.pk), q="Regning") == []
    for junk in ("²", "abc", "999999", str(make_tag(make_mailbox("fest", name="F"), "X").pk)):
        assert sorted(subjects_in(client, ny, mailbox, tag=junk)) == ["Hej", "Regning", "Øl"]  # ignored
    assert plain.pk


def test_the_owner_chips_keep_the_tag_filter(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    tag = make_tag(mailbox)
    page = board(client, tag=str(tag.pk))
    assert f"ansvarlig=mig&amp;tag={tag.pk}" in page or f"tag={tag.pk}&amp;ansvarlig=mig" in page


def test_the_archive_shows_and_filters_by_tags(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    tag = make_tag(mailbox, "Faktura")
    faerdig = col(mailbox, ColumnRole.FAERDIG)
    old, other = (
        make_thread(mailbox, "Gammel regning", column=faerdig, age_days=100),
        make_thread(mailbox, "Gammel øl", column=faerdig, age_days=101),
    )
    old.tags.add(tag)
    url = reverse("postkasser:archive", args=["ak"])
    everything = client.get(url)
    assert {c.subject for c in everything.context["page"]} == {"Gammel regning", "Gammel øl"}
    assert "chip-tag-roed" in everything.content.decode()
    assert [c.subject for c in client.get(url, {"tag": str(tag.pk)}).context["page"]] == ["Gammel regning"]
    assert len(client.get(url, {"tag": "²"}).context["page"]) == 2
    assert other.pk


def test_a_tag_name_is_escaped_everywhere(client: Client, ak_member: Resident, server: FakeServer) -> None:
    mailbox = make_mailbox(roles=["ak"])
    evil = make_tag(mailbox, "<script>alert(1)</script>")
    sag = make_thread(mailbox)
    old = make_thread(mailbox, "Gammel", column=col(mailbox, ColumnRole.FAERDIG), age_days=100)
    sag.tags.add(evil)
    old.tags.add(evil)
    for url in (
        reverse("postkasser:board", args=["ak"]),
        reverse("postkasser:archive", args=["ak"]),
        tags_url(mailbox),
        reverse("postkasser:thread", args=["ak", sag.pk]),
    ):
        page = client.get(url).content.decode()
        assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)" in page, url


def test_the_board_query_count_does_not_grow_with_cards_or_tags(
    client: Client, ak_member: Resident, django_assert_max_num_queries: object
) -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    mailbox = make_mailbox(roles=["ak"])
    palette = [
        make_tag(mailbox, f"Tag {i}", color) for i, color in enumerate(["roed", "blaa", "gul", "groen"])
    ]
    url = reverse("postkasser:board", args=["ak"])

    def count() -> int:
        with CaptureQueriesContext(connection) as queries:
            assert client.get(url).status_code == 200
        return len(queries)

    for i in range(3):
        make_thread(mailbox, f"Sag {i}").tags.set(palette[:1])
    few = count()
    for i in range(3, 30):
        make_thread(mailbox, f"Sag {i}").tags.set(palette)
    assert count() == few


def test_the_admin_manages_tags_inline_and_refuses_duplicates(
    client: Client, make_resident: MakeResident
) -> None:
    from django.forms import inlineformset_factory

    from postkasser.admin import MailTagFormSet

    mailbox = make_mailbox()
    tag = make_tag(mailbox, "Faktura")
    factory = inlineformset_factory(
        Mailbox, MailTag, formset=MailTagFormSet, fields=["name", "color", "position"], extra=0
    )

    def data(*rows: tuple[str, str]) -> dict[str, str]:
        out = {
            "tags-TOTAL_FORMS": str(len(rows)),
            "tags-INITIAL_FORMS": "1",
            "tags-MIN_NUM_FORMS": "0",
            "tags-MAX_NUM_FORMS": "1000",
        }
        for i, (name, color) in enumerate(rows):
            out |= {f"tags-{i}-id": str(tag.pk) if i == 0 else "", f"tags-{i}-mailbox": str(mailbox.pk),
                    f"tags-{i}-name": name, f"tags-{i}-color": color, f"tags-{i}-position": str(i)}  # fmt: skip
        return out

    bad = factory(data(("Faktura", "roed"), ("faktura", "blaa")), instance=mailbox, prefix="tags")
    assert not bad.is_valid() and "to tags" in str(bad.non_form_errors())
    good = factory(data(("Faktura", "roed"), ("Brygger", "blaa")), instance=mailbox, prefix="tags")
    assert good.is_valid(), good.non_form_errors()
    good.save()
    assert list(mailbox.tags.values_list("name", flat=True)) == ["Faktura", "Brygger"]
    admin_user = make_resident("adm@intern.dk", roles=(Role.ADMINISTRATOR,))
    Resident.objects.filter(pk=admin_user.pk).update(is_superuser=True, is_staff=True)
    client.force_login(admin_user)
    assert (
        "Brygger"
        in client.get(reverse("admin:postkasser_mailbox_change", args=[mailbox.pk])).content.decode()
    )


# --- stale UIDVALIDITY, claim heartbeat, bookkeeping saves, emptied sager --------------------------------


STALE = "er ændret siden sidste synkronisering — hent post og prøv igen"


def test_a_move_from_a_folder_whose_uidvalidity_changed_moves_nothing(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    """The folder was re-created: cached UID 1 now names a DIFFERENT mail. It must not be moved."""
    mailbox, sag = move_setup(server)
    server.uidvalidity = 999
    server.messages.clear()
    server.flags.clear()
    server.put(1, raw_mail("Helt anden mail", message_id="<other@x>"))
    response = client.post(
        move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Trash").pk)}, follow=True
    )
    assert STALE in response.content.decode()
    assert server.moves == [] and list(server.folders["INBOX"].messages) == [1]
    assert not server.folders["Trash"].messages
    assert MailThread.objects.filter(pk=sag.pk).exists() and MailMessage.objects.count() == 1


def test_a_folder_that_was_never_synced_is_not_acted_on(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    MailFolder.objects.filter(mailbox=mailbox, name="INBOX").update(uidvalidity=None)
    response = client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)}, follow=True)
    assert STALE in response.content.decode() and server.moves == []


def test_a_stale_group_is_skipped_and_the_others_are_moved(ak_member: Resident, server: FakeServer) -> None:
    mailbox, _sag = move_setup(server)
    server.put(1, raw_mail("I Arkiv", message_id="<a@x>"), folder="Arkiv")
    services.sync(mailbox)
    server.folders["Arkiv"].uidvalidity = 77  # re-created after the sync
    fest = folder_of(mailbox, "Fest")
    outcome = services.move_messages(mailbox, list(mailbox.messages.all()), fest, moved_by=ak_member)
    assert outcome.moved == 1 and len(outcome.errors) == 1
    assert "«Arkiv»" in outcome.errors[0] and STALE in outcome.errors[0]
    assert server.moves == [("INBOX", [1], "Fest")]


def test_opening_and_flagging_are_refused_for_a_stale_folder(server: FakeServer) -> None:
    mailbox = make_mailbox()
    (row,) = synced(server, mailbox, raw_mail("Gammel"))
    server.uidvalidity = 5
    with pytest.raises(services.StaleFolder, match="er ændret"):
        services.fetch_raw(mailbox, row, mark_seen=True)
    with pytest.raises(imap.MailboxError, match="er ændret"):
        services.mark_answered(mailbox, row)
    row.refresh_from_db()
    assert server.added == [] and not row.seen and not row.answered


def test_a_reply_to_a_stale_mail_says_so_instead_of_sending(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    (row,) = synced(server, mailbox, raw_mail("Gammel"))
    server.uidvalidity = 5
    response = client.get(reverse("postkasser:reply", args=["ak", row.pk]), follow=True)
    assert STALE in response.content.decode() and mail.outbox == []


class Clock:
    def __init__(self) -> None:
        self.now = timezone.now()

    def __call__(self) -> object:
        return self.now


def test_renewing_keeps_a_claim_alive_past_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox = make_mailbox()
    clock = Clock()
    monkeypatch.setattr(services.timezone, "now", clock)
    step = services.CLAIM_TTL - timedelta(seconds=10)
    with services.claim(mailbox) as token:
        clock.now += step
        token.renew()
        clock.now += step  # a whole TTL since the claim was taken, but only `step` since the renewal
        with services.claim(mailbox) as other:
            assert not other
        assert claimed_at(mailbox) == token.at
    clock.now += step
    with services.claim(mailbox) as again:
        assert again


def test_a_claim_taken_over_cannot_be_renewed_and_is_not_released(monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox = make_mailbox()
    with services.claim(mailbox) as token:
        hold_claim(mailbox)  # somebody else took it over
        taken = claimed_at(mailbox)
        with pytest.raises(services.SyncBusy):
            token.renew()
    assert claimed_at(mailbox) == taken  # the successor's claim is left alone


def test_a_sync_that_lost_its_claim_aborts_between_folders_and_drops_no_sager(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = make_mailbox()
    server.add_folder("Fest")
    synced(server, mailbox, raw_mail("Forsvinder"))
    server.take(1)  # the cleanup at the end of a sync would delete this now-empty sag
    real = services._sync_folder
    calls: list[str] = []

    def steal_after_the_first(run: services._Run, conn: object, folder: MailFolder) -> None:
        real(run, conn, folder)  # type: ignore[arg-type]
        calls.append(folder.name)
        if len(calls) == 1:
            hold_claim(mailbox)

    monkeypatch.setattr(services, "_sync_folder", steal_after_the_first)
    with pytest.raises(services.SyncBusy):
        services.sync(mailbox)
    assert calls == ["INBOX"] and MailThread.objects.count() == 1
    assert claimed_at(mailbox) is not None  # the thief still holds it


def test_a_crashed_runs_claim_is_taken_over_after_the_short_ttl() -> None:
    mailbox = make_mailbox()
    assert services.CLAIM_TTL == timedelta(minutes=3)
    hold_claim(mailbox, services.CLAIM_TTL - timedelta(seconds=5))
    with services.claim(mailbox) as early:
        assert not early
    hold_claim(mailbox, services.CLAIM_TTL + timedelta(seconds=5))
    with services.claim(mailbox) as late:
        assert late


def test_a_sync_renews_its_claim_per_folder(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox = make_mailbox()
    server.add_folder("Fest")
    renewals: list[object] = []
    real = services.Claim.renew

    def counting(self: services.Claim) -> None:
        real(self)
        renewals.append(self.at)

    monkeypatch.setattr(services.Claim, "renew", counting)
    services.sync(mailbox)
    assert len(renewals) >= 2 and claimed_at(mailbox) is None


def test_a_move_that_loses_its_claim_stops_before_the_next_group(
    ak_member: Resident, server: FakeServer
) -> None:
    mailbox, _sag = move_setup(server)
    server.put(1, raw_mail("I Arkiv", message_id="<a@x>"), folder="Arkiv")
    services.sync(mailbox)
    steal = [lambda: hold_claim(mailbox)]
    server.during_move = lambda: steal.pop()() if steal else None
    outcome = services.move_messages(
        mailbox,
        list(mailbox.messages.order_by("folder__name")),
        folder_of(mailbox, "Fest"),
        moved_by=ak_member,
    )
    assert outcome.lost_claim and outcome.moved == 1 and len(server.moves) == 1
    assert MailEvent.objects.filter(action="move").count() == 1  # what was moved is still logged


def test_a_plain_save_of_a_stale_mailbox_does_not_release_a_live_claim() -> None:
    mailbox = make_mailbox()
    stale = Mailbox.objects.get(pk=mailbox.pk)
    with services.claim(mailbox) as token:
        stale.signature = "ny signatur"
        stale.save()  # what the admin does
        assert claimed_at(mailbox) == token.at
        with services.claim(mailbox) as second:
            assert not second
    assert Mailbox.objects.get(pk=mailbox.pk).signature == "ny signatur"


def test_a_plain_save_of_a_stale_mailbox_does_not_set_a_dead_claim_or_revert_status() -> None:
    mailbox = make_mailbox()
    with services.claim(mailbox):
        during = Mailbox.objects.get(pk=mailbox.pk)  # loaded while the claim was held
    Mailbox.objects.filter(pk=mailbox.pk).update(last_sync_error="fejl", last_synced_at=timezone.now())
    during.signature = "x"
    during.save()
    fresh = Mailbox.objects.get(pk=mailbox.pk)
    assert fresh.sync_started_at is None  # no lockout for a TTL
    assert fresh.last_sync_error == "fejl" and fresh.last_synced_at is not None
    with services.claim(mailbox) as taken:
        assert taken


def test_save_with_update_fields_still_writes_the_bookkeeping_fields() -> None:
    mailbox = make_mailbox()
    mailbox.last_sync_error = "nede"
    mailbox.save(update_fields=["last_sync_error"])
    assert Mailbox.objects.get(pk=mailbox.pk).last_sync_error == "nede"


def hidden_sag_setup(server: FakeServer) -> tuple[Mailbox, MailThread]:
    """A sag whose only mail moved into `Fest`, which cannot be read."""
    mailbox = make_mailbox(roles=["ak"])
    server.add_folder("Fest")
    synced(server, mailbox, raw_mail("Flyttet", message_id="<f@x>"))
    sag = MailThread.objects.get()
    tag = tags.add(mailbox, "Faktura")
    sag.tags.add(tag)
    server.take(1)
    server.put(1, raw_mail("Flyttet", message_id="<f@x>"), folder="Fest")
    server.broken.add("Fest")
    return mailbox, sag


def test_a_sag_empty_through_two_failing_runs_survives_and_the_mail_rejoins_it(server: FakeServer) -> None:
    mailbox, sag = hidden_sag_setup(server)
    services.sync(mailbox)
    services.sync(mailbox)  # the second run no longer remembers the move itself
    stamped = MailThread.objects.get(pk=sag.pk)
    assert stamped.emptied_at is not None
    server.broken.clear()
    services.sync(mailbox)
    back = MailThread.objects.get()
    assert back.pk == sag.pk and back.emptied_at is None and back.emptied_ids == ""
    assert back.messages.count() == 1 and list(back.tags.values_list("name", flat=True)) == ["Faktura"]


def test_a_clean_run_deletes_a_stamped_sag_whose_mail_is_really_gone(server: FakeServer) -> None:
    mailbox, sag = hidden_sag_setup(server)
    services.sync(mailbox)
    server.broken.clear()
    server.take(1, "Fest")
    services.sync(mailbox)
    assert not MailThread.objects.filter(pk=sag.pk).exists()


def test_a_stamped_sag_is_deleted_after_a_week_even_if_a_folder_keeps_failing(server: FakeServer) -> None:
    mailbox, sag = hidden_sag_setup(server)
    services.sync(mailbox)
    MailThread.objects.filter(pk=sag.pk).update(emptied_at=timezone.now() - timedelta(days=8))
    services.sync(mailbox)
    assert not MailThread.objects.filter(pk=sag.pk).exists()


def test_a_stamped_sag_is_hidden_from_board_archive_and_counts(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = hidden_sag_setup(server)
    services.sync(mailbox)
    assert MailThread.objects.get(pk=sag.pk).emptied_at is not None
    assert subjects_in(client, col(mailbox, ColumnRole.NY), mailbox) == []
    MailThread.objects.filter(pk=sag.pk).update(column=col(mailbox, ColumnRole.FAERDIG))
    archive_page = client.get(reverse("postkasser:archive", args=["ak"]))
    assert list(archive_page.context["page"].object_list) == []
    columns_page = client.get(reverse("postkasser:columns", args=["ak"]))
    assert {c.name: c.thread_count for c in columns_page.context["columns"]}["Færdig"] == 0
    tags_page = client.get(tags_url(mailbox))
    assert [t.thread_count for t in tags_page.context["tags"]] == [0]


def test_a_sag_waiting_for_its_sent_copy_stays_on_the_board(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    services.start_thread(mailbox, ak_member, "Nyt emne", "<ny@x>")
    assert subjects_in(client, col(mailbox, ColumnRole.AFVENTER), mailbox) == ["Nyt emne"]


def test_a_move_fallback_rejoins_a_mail_with_no_message_id_by_fingerprint(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.add_folder("Fest")
    raw = b"\n".join(
        line for line in raw_mail("Uden id").split(b"\n") if not line.lower().startswith(b"message-id")
    )
    synced(server, mailbox, raw)
    sag = MailThread.objects.get()
    tag = tags.add(mailbox, "Faktura")
    sag.tags.add(tag)
    MailThread.objects.filter(pk=sag.pk).update(column=mailbox.columns.get(name="I gang"), assignee=ak_member)
    server.copyuid = False
    client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)})
    services.sync(mailbox)
    kept = MailThread.objects.get()
    assert kept.pk == sag.pk and kept.column.name == "I gang" and kept.assignee == ak_member
    assert list(kept.tags.all()) == [tag] and kept.messages.get().folder.name == "Fest"


def test_a_fingerprint_needs_every_part_to_match(server: FakeServer) -> None:
    mailbox = make_mailbox()
    server.add_folder("Fest")
    bare = [
        b"\n".join(
            line for line in raw_mail(subject).split(b"\n") if not line.lower().startswith(b"message-id")
        )
        for subject in ("Samme", "Samme")
    ]
    synced(server, mailbox, bare[0])
    server.put(1, bare[1] + b"\nextra", folder="Fest")  # another size: another mail
    services.sync(mailbox)
    assert MailThread.objects.count() == 2


class BadClient:
    """An imaplib client whose raw command machinery answers BAD for chosen commands."""

    def __init__(self, bad: set[str], capabilities: bytes = b"IMAP4REV1 UIDPLUS") -> None:
        self.bad, self.capabilities = set(bad), capabilities
        self.untagged_responses: dict[str, list] = {}
        self.sent: list[tuple] = []

    def capability(self) -> tuple[str, list[bytes]]:
        return "OK", [self.capabilities]

    def _simple_command(self, name: str, command: str, *args: str) -> tuple[str, list[bytes]]:
        self.sent.append((command, *args))
        if command in self.bad:
            raise imaplib.IMAP4.error(f"{command} command error: BAD [b'Error in IMAP command']")
        return "OK", [b"[COPYUID 1 4 9] done"]


def test_a_bad_answer_to_a_uid_command_is_a_mailbox_error() -> None:
    conn = imap.Connection(BadClient({"MOVE"}, b"IMAP4REV1 MOVE"))  # type: ignore[arg-type]
    with pytest.raises(imap.MailboxError, match="MOVE"):
        conn.move([4], "Fest")


def test_a_bad_expunge_after_a_copy_says_the_mail_was_copied() -> None:
    conn = imap.Connection(BadClient({"EXPUNGE"}))  # type: ignore[arg-type]
    with pytest.raises(imap.MailboxError, match="kopieret til «Fest», men kunne ikke fjernes"):
        conn.move([4], "Fest")


def test_setting_tags_takes_the_mailbox_lock(
    ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    synced(server, mailbox, raw_mail("Tag mig"))
    tag = tags.add(mailbox, "Faktura")
    locked: list[int] = []
    monkeypatch.setattr(tags, "_lock", lambda box: locked.append(box.pk))
    assert tags.set_for_thread(MailThread.objects.get(), [tag.pk], ak_member)
    assert locked == [mailbox.pk]


def test_the_boards_archive_link_keeps_the_tag_and_search_filters(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    tag = tags.add(mailbox, "Faktura")
    page = board(client, tag=str(tag.pk), q="æ&ø")
    link = reverse("postkasser:archive", args=["ak"])
    assert f'href="{link}?tag={tag.pk}&amp;q=%C3%A6%26%C3%B8"' in page
    assert f'href="{link}">Arkiv' in board(client)


def test_moved_counts_only_the_mails_the_server_moved(ak_member: Resident, server: FakeServer) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.add_folder("Fest")
    synced(server, mailbox, raw_mail("A", message_id="<a@x>"), raw_mail("B", message_id="<b@x>"))

    def delete_b() -> None:  # B was deleted at one.com a moment before the move
        server.take(2)

    server.during_move = delete_b
    outcome = services.move_messages(
        mailbox, list(mailbox.messages.all()), folder_of(mailbox, "Fest"), moved_by=ak_member
    )
    assert outcome.moved == 1 and server.moves == [("INBOX", [1], "Fest")]


def test_the_move_event_points_at_the_new_row_after_a_resync(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    server.copyuid = False
    client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)})
    event = MailEvent.objects.get(action="move")
    assert event.message is not None and event.message.folder.name == "Fest"
    assert event.message == MailMessage.objects.get()


def test_the_move_event_has_no_message_when_the_old_row_is_stale(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    server.copyuid = False
    server.broken.add("Fest")  # the target cannot be synced: the old row stays, stale
    client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)})
    assert MailEvent.objects.get(action="move").message is None


# --- Markér som ulæst -----------------------------------------------------------------------------------------


def unread_url(mailbox: Mailbox, message: MailMessage) -> str:
    return reverse("postkasser:message_unread", args=[mailbox.slug, message.pk])


def thread_unread_url(mailbox: Mailbox, sag: MailThread) -> str:
    return reverse("postkasser:thread_unread", args=[mailbox.slug, sag.pk])


def test_marking_a_mail_unread_clears_seen_at_the_server_and_in_the_cache_and_logs_it(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.put(1, raw_mail("Læst"), {r"\Seen"})
    (row,) = synced(server, mailbox)
    assert row.seen
    response = client.post(unread_url(mailbox, row), follow=True)
    assert response.redirect_chain[-1][0] == reverse(
        "postkasser:board", args=["ak"]
    )  # the board, not the mail
    assert "Markeret som ulæst." in response.content.decode()
    assert server.removed == [("INBOX", 1, (r"\Seen",))] and r"\Seen" not in server.flags[1]
    row.refresh_from_db()
    assert not row.seen
    event = MailEvent.objects.get(action="unread")
    assert event.message == row and event.thread == row.thread and event.resident == ak_member


def test_marking_a_sag_unread_picks_its_newest_incoming_mail(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.put(
        1, raw_mail("Spørgsmål", message_id="<q@x>", date="Mon, 02 Nov 2026 10:00:00 +0100"), {r"\Seen"}
    )
    server.put(
        2,
        raw_mail(
            "Re: Spørgsmål", sender="ak@gahk.dk", in_reply_to="<q@x>", date="Mon, 02 Nov 2026 11:00:00 +0100"
        ),
        {r"\Seen"},
        folder="Sent",
    )
    synced(server, mailbox)
    sag = MailThread.objects.get()
    assert sag.messages.count() == 2
    client.post(thread_unread_url(mailbox, sag))
    assert server.removed == [("INBOX", 1, (r"\Seen",))]  # not our own reply in Sent
    assert not MailMessage.objects.get(uid=1, folder__name="INBOX").seen
    assert MailMessage.objects.get(folder__name="Sent").seen


def test_marking_a_sag_of_only_sent_mail_unread_falls_back_to_its_newest_mail(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    sag = services.start_thread(mailbox, ak_member, "Fra os", "<os@x>")
    row = make_message(mailbox, 1, folder="Sent", thread=sag, seen=True, from_address="ak@gahk.dk")
    MailFolder.objects.filter(mailbox=mailbox, name="Sent").update(special_use="sent", uidvalidity=1)
    server.put(1, raw_mail("Fra os", sender="ak@gahk.dk"), {r"\Seen"}, folder="Sent")
    client.post(thread_unread_url(mailbox, sag))
    assert server.removed == [("Sent", 1, (r"\Seen",))]
    row.refresh_from_db()
    assert not row.seen


def test_a_sag_with_no_mail_cannot_be_marked_unread(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    sag = services.start_thread(mailbox, ak_member, "Venter", "<v@x>")
    response = client.post(thread_unread_url(mailbox, sag), follow=True)
    assert "ingen mails" in response.content.decode() and server.removed == []


def test_mark_unread_works_while_a_sync_holds_the_claim(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.put(1, raw_mail("Læst"), {r"\Seen"})
    (row,) = synced(server, mailbox)
    hold_claim(mailbox)
    response = client.post(unread_url(mailbox, row), follow=True)
    assert "Markeret som ulæst." in response.content.decode()
    assert claimed_at(mailbox) is not None  # and it did not take or release the claim


def test_a_server_refusal_is_a_message_and_leaves_the_cache(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.put(1, raw_mail("Læst"), {r"\Seen"})
    (row,) = synced(server, mailbox)
    server.flag_error = imap.MailboxError("STORE: NO")
    response = client.post(unread_url(mailbox, row), follow=True)
    assert (
        response.status_code == 200 and "Kunne ikke markere som ulæst: STORE: NO" in response.content.decode()
    )
    row.refresh_from_db()
    assert row.seen and not MailEvent.objects.filter(action="unread").exists()
    server.flag_error = None
    server.error = imap.MailboxError("nede")
    assert "Kunne ikke markere" in client.post(unread_url(mailbox, row), follow=True).content.decode()


def test_mark_unread_refuses_a_stale_folder(client: Client, ak_member: Resident, server: FakeServer) -> None:
    mailbox = make_mailbox(roles=["ak"])
    server.put(1, raw_mail("Læst"), {r"\Seen"})
    (row,) = synced(server, mailbox)
    server.uidvalidity = 42
    response = client.post(unread_url(mailbox, row), follow=True)
    assert STALE in response.content.decode() and server.removed == []
    row.refresh_from_db()
    assert row.seen


def test_mark_unread_is_post_only_scoped_and_needs_login(
    client: Client, ak_member: Resident, server: FakeServer, make_resident: MakeResident
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    other = make_mailbox("fest", roles=["fest"])
    server.put(1, raw_mail("Hemmelig"), {r"\Seen"})
    (row,) = synced(server, mailbox)
    foreign = make_message(other, 9, seen=True)
    foreign_sag = MailThread.objects.create(mailbox=other, subject="X", column=col(other, ColumnRole.NY))
    assert client.get(unread_url(mailbox, row)).status_code == 405
    assert client.get(thread_unread_url(mailbox, row.thread)).status_code == 405  # type: ignore[arg-type]
    assert client.post(reverse("postkasser:message_unread", args=["ak", foreign.pk])).status_code == 404
    assert client.post(reverse("postkasser:thread_unread", args=["ak", foreign_sag.pk])).status_code == 404
    assert client.post(reverse("postkasser:message_unread", args=["fest", foreign.pk])).status_code == 404
    client.logout()
    assert client.post(unread_url(mailbox, row)).status_code == 302
    client.force_login(make_resident("anden@intern.dk"))
    assert client.post(unread_url(mailbox, row)).status_code == 404
    assert server.removed == []


def test_the_buttons_are_on_the_message_and_sag_pages(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    (row,) = synced(server, mailbox, raw_mail("Knap"))
    message_page = client.get(reverse("postkasser:message", args=["ak", row.pk])).content.decode()
    sag_page = client.get(reverse("postkasser:thread", args=["ak", row.thread_id])).content.decode()
    assert unread_url(mailbox, row) in message_page and "Markér som ulæst" in message_page
    assert thread_unread_url(mailbox, row.thread) in sag_page  # type: ignore[arg-type]


# --- automation: rules for incoming mail --------------------------------------------------------------------


def arrive(server: FakeServer, mailbox: Mailbox, *raws: bytes, folder: str = "INBOX") -> None:
    """New mail on the server, then a sync — never the folder's first one (see `quiet_box`)."""
    for raw in raws:
        server.put(max(server.folders[folder].messages, default=0) + 1, raw, folder=folder)
    services.sync(mailbox)


def quiet_box(server: FakeServer, roles: list[str] | None = None) -> Mailbox:
    """A mailbox whose folders have all had their first sync (the history import), so what arrives next
    is NEW mail, which is what rules and notifications are for."""
    mailbox = make_mailbox(roles=["ak"] if roles is None else roles)
    services.sync(mailbox)
    return mailbox


def make_rule(
    mailbox: Mailbox, name: str = "Regel", *, tags_: list[MailTag] | None = None, **fields: object
) -> MailRule:
    rule = MailRule.objects.create(mailbox=mailbox, name=name, position=mailbox.rules.count(), **fields)
    if tags_:
        rule.add_tags.set(tags_)
    return rule


def rule_events(sag: MailThread) -> list[str]:
    return list(sag.events.filter(action="rule").order_by("pk").values_list("detail", flat=True))


def test_rule_conditions_are_anded_casefolded_and_empty_ones_ignored() -> None:
    mailbox = make_mailbox()
    message = MailMessage(
        mailbox=mailbox,
        folder_id=7,
        from_address="Faktura@Leverandor.dk",
        from_name="Bogholderiet",
        subject="Din REGNING for oktober",
        to="ak@gahk.dk",
    )
    rule = MailRule(mailbox=mailbox, name="r")
    assert not rules.matches(rule, message)  # no condition: matches nothing, not everything
    for attr, value in [
        ("from_contains", "LEVERANDOR"),  # address
        ("from_contains", "bogholder"),  # name
        ("subject_contains", "regning"),
        ("to_contains", "GAHK.dk"),
    ]:
        assert rules.matches(MailRule(mailbox=mailbox, name="r", **{attr: value}), message), attr
        assert not rules.matches(MailRule(mailbox=mailbox, name="r", **{attr: "zzz"}), message), attr
    both = MailRule(mailbox=mailbox, name="r", from_contains="leverandor", subject_contains="regning")
    assert rules.matches(both, message)
    both.subject_contains = "rykker"
    assert not rules.matches(both, message)
    assert rules.matches(MailRule(mailbox=mailbox, name="r", folder_id=7), message)
    assert not rules.matches(MailRule(mailbox=mailbox, name="r", folder_id=8), message)


def test_a_new_mail_gets_the_rules_actions_and_the_event_says_what_happened(
    ak_member: Resident, server: FakeServer
) -> None:
    mailbox = quiet_box(server)
    tag = tags.add(mailbox, "Brygger")
    gang = mailbox.columns.get(name="I gang")
    make_rule(
        mailbox, "Brygger-mail", tags_=[tag], from_contains="MOLLER", set_column=gang, set_assignee=ak_member
    )
    arrive(server, mailbox, raw_mail("Øl", sender="Møller <moller@brygger.dk>"))
    sag = MailThread.objects.get()
    assert sag.column == gang and sag.assignee == ak_member and list(sag.tags.all()) == [tag]
    assert rule_events(sag) == [
        f"Regel «Brygger-mail»: tag Brygger, → I gang, ansvarlig {ak_member.full_name}"
    ]
    event = sag.events.get(action="rule")
    assert event.resident is None  # the system did it


def test_rules_do_not_run_on_a_folders_first_sync_or_on_our_own_mail(
    ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    tag = tags.add(mailbox, "Alle")
    make_rule(mailbox, "Alt", tags_=[tag], from_contains="@")
    server.put(1, raw_mail("Historisk"))
    services.sync(mailbox)  # first sync: history
    assert not MailThread.objects.get().tags.exists()
    arrive(server, mailbox, raw_mail("Fra os selv", sender="ak@gahk.dk"))
    assert not MailThread.objects.get(subject="Fra os selv").tags.exists()
    arrive(
        server, mailbox, raw_mail("Sendt", sender="Mette <mette@example.com>"), folder="Sent"
    )  # sent folder
    assert not MailThread.objects.filter(subject="Sendt", tags=tag).exists()
    arrive(server, mailbox, raw_mail("Ny"))
    assert MailThread.objects.get(subject="Ny").tags.get() == tag


def test_rules_do_not_run_on_a_mail_that_merely_moved_folder(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)
    tag = tags.add(mailbox, "Alle")
    make_rule(mailbox, "Alt", tags_=[tag], from_contains="mette")
    server.copyuid = False  # the move is followed by a sync of the target: the mail turns up as a new row
    client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)})
    assert MailMessage.objects.get().folder.name == "Fest"
    assert not MailThread.objects.get().tags.exists() and not rule_events(sag)


def test_rules_run_in_position_order_and_stop_processing_ends_the_run(
    ak_member: Resident, server: FakeServer
) -> None:
    mailbox = quiet_box(server)
    gang, faerdig = mailbox.columns.get(name="I gang"), col(mailbox, ColumnRole.FAERDIG)
    make_rule(mailbox, "Først", from_contains="mette", set_column=gang, stop_processing=True)
    make_rule(mailbox, "Sidst", from_contains="mette", set_column=faerdig)
    arrive(server, mailbox, raw_mail("A"))
    assert MailThread.objects.get().column == gang  # the second never ran
    MailRule.objects.filter(name="Først").update(position=5, stop_processing=False)
    arrive(server, mailbox, raw_mail("B"))
    assert (
        MailThread.objects.get(subject="B").column == gang
    )  # "Sidst" (position 1) ran first, "Først" overwrote


def test_a_rules_column_beats_the_reopen_to_ny(ak_member: Resident, server: FakeServer) -> None:
    mailbox = quiet_box(server)
    arrive(server, mailbox, raw_mail("Sag", message_id="<s@x>"))
    sag = MailThread.objects.get()
    MailThread.objects.filter(pk=sag.pk).update(column=col(mailbox, ColumnRole.FAERDIG))
    gang = mailbox.columns.get(name="I gang")
    make_rule(mailbox, "Til gang", subject_contains="sag", set_column=gang)
    arrive(server, mailbox, raw_mail("Re: Sag", in_reply_to="<s@x>"))
    assert MailThread.objects.get().column == gang


def test_tags_are_added_never_removed_and_an_assignee_who_left_is_skipped_and_logged(
    ak_member: Resident, server: FakeServer, make_resident: MakeResident
) -> None:
    mailbox = quiet_box(server)
    old, new = tags.add(mailbox, "Gammel"), tags.add(mailbox, "Ny")
    gone = make_resident("gone@intern.dk")  # holds no role: no longer in the group
    make_rule(mailbox, "Ud", tags_=[new], from_contains="mette", set_assignee=gone)
    arrive(server, mailbox, raw_mail("Hej", message_id="<h@x>"))
    sag = MailThread.objects.get()
    sag.tags.add(old)
    arrive(server, mailbox, raw_mail("Re: Hej", in_reply_to="<h@x>"))
    sag.refresh_from_db()
    assert set(sag.tags.all()) == {old, new} and sag.assignee is None
    assert rule_events(sag) == ["Regel «Ud»: tag Ny"]


def test_a_failing_rule_does_not_stop_the_mail_being_filed(
    ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = quiet_box(server)
    make_rule(mailbox, "Går i stykker", from_contains="mette", set_assignee=ak_member)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("regel gik i stykker")

    monkeypatch.setattr(rules, "_changes", boom)
    arrive(server, mailbox, raw_mail("Kommer alligevel"))
    assert MailThread.objects.get().messages.count() == 1


def test_a_rule_form_needs_a_condition_and_an_action_and_only_this_mailboxs_choices(
    ak_member: Resident, make_resident: MakeResident
) -> None:
    from postkasser.forms import RuleForm

    mailbox, other = make_mailbox(roles=["ak"]), make_mailbox("fest", roles=["fest"])
    tag = tags.add(mailbox, "Egen")
    foreign_tag = tags.add(other, "Fremmed")
    foreign_col = other.columns.get(name="I gang")
    foreign_folder = MailFolder.objects.create(mailbox=other, name="X", display_name="X", path="X")
    outsider = make_resident("udenfor@intern.dk")
    base = {"name": "R", "from_contains": "a", "add_tags": [str(tag.pk)]}
    assert RuleForm(base, mailbox=mailbox).is_valid()
    assert "mindst én betingelse" in str(
        RuleForm({"name": "R", "add_tags": [str(tag.pk)]}, mailbox=mailbox).errors
    )
    assert "skal gøre noget" in str(RuleForm({"name": "R", "from_contains": "a"}, mailbox=mailbox).errors)
    for name, value in [
        ("add_tags", [str(foreign_tag.pk)]),
        ("set_column", str(foreign_col.pk)),
        ("folder", str(foreign_folder.pk)),
        ("set_assignee", str(outsider.pk)),
    ]:
        form = RuleForm({**base, name: value}, mailbox=mailbox)
        assert not form.is_valid() and name in form.errors, name


def test_run_on_existing_applies_to_open_sager_by_their_latest_incoming_mail_only(
    ak_member: Resident, server: FakeServer
) -> None:
    mailbox = quiet_box(server)
    arrive(
        server,
        mailbox,
        raw_mail("Åben", sender="Faktura <f@lev.dk>"),
        raw_mail("Lukket", sender="Faktura <f@lev.dk>"),
        raw_mail("Andre", sender="Anden <a@x.dk>"),
    )
    MailThread.objects.filter(subject="Lukket").update(column=col(mailbox, ColumnRole.FAERDIG))
    tag = tags.add(mailbox, "Faktura")
    rule = make_rule(mailbox, "Fakturaer", tags_=[tag], from_contains="lev.dk")
    assert rules.run_on_existing(rule, mailbox) == 1
    assert list(MailThread.objects.filter(tags=tag).values_list("subject", flat=True)) == ["Åben"]
    assert rules.run_on_existing(rule, mailbox) == 0  # idempotent
    assert claimed_at(mailbox) is None


def test_run_on_existing_takes_the_claim_and_refuses_while_one_is_held(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = quiet_box(server)
    arrive(server, mailbox, raw_mail("Åben"))
    tag = tags.add(mailbox, "T")
    rule = make_rule(mailbox, "R", tags_=[tag], from_contains="mette")
    hold_claim(mailbox)
    response = client.post(automation_url(mailbox), {"action": "rule_run", "rule": str(rule.pk)}, follow=True)
    assert services.SYNC_BUSY in response.content.decode()
    assert not MailThread.objects.get().tags.exists()


def automation_url(mailbox: Mailbox) -> str:
    return reverse("postkasser:automation", args=[mailbox.slug])


def test_the_automation_page_creates_edits_orders_toggles_runs_and_deletes_rules(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = quiet_box(server)
    tag = tags.add(mailbox, "Faktura")
    url = automation_url(mailbox)
    data = {
        "action": "rule_save",
        "name": "Første",
        "from_contains": "lev",
        "add_tags": [str(tag.pk)],
        "is_active": "on",
    }
    assert "Reglen er gemt." in client.post(url, data, follow=True).content.decode()
    client.post(url, {**data, "name": "Anden"})
    first, second = MailRule.objects.order_by("position")
    assert (first.name, second.name) == ("Første", "Anden") and first.position < second.position
    client.post(url, {"action": "rule_down", "rule": str(first.pk)})
    assert [r.name for r in MailRule.objects.order_by("position")] == ["Anden", "Første"]
    client.post(url, {"action": "rule_up", "rule": str(first.pk)})
    assert [r.name for r in MailRule.objects.order_by("position")] == ["Første", "Anden"]
    assert "Rediger" in client.get(url).content.decode()
    assert "Redigér «Første»" in client.get(url, {"regel": str(first.pk)}).content.decode()
    client.post(url, {**data, "rule": str(first.pk), "name": "Omdøbt"})
    first.refresh_from_db()
    assert first.name == "Omdøbt" and MailRule.objects.count() == 2
    client.post(url, {"action": "rule_toggle", "rule": str(first.pk)})
    first.refresh_from_db()
    assert not first.is_active
    assert (
        "slået fra"
        in client.post(url, {"action": "rule_run", "rule": str(first.pk)}, follow=True).content.decode()
    )  # an inactive rule is not run on the existing sager
    client.post(url, {"action": "rule_toggle", "rule": str(first.pk)})
    assert (
        "ændrede 0 åbne sager"
        in client.post(url, {"action": "rule_run", "rule": str(first.pk)}, follow=True).content.decode()
    )
    client.post(url, {"action": "rule_delete", "rule": str(first.pk)})
    assert [r.name for r in MailRule.objects.all()] == ["Anden"]


def test_the_automation_page_shows_form_errors_and_never_500s_on_rubbish(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    other = make_mailbox("fest", roles=["fest"])
    foreign = make_rule(other, "Fremmed", from_contains="x", set_column=other.columns.first())
    url = automation_url(mailbox)
    page = client.post(url, {"action": "rule_save", "name": "Tom"})
    assert page.status_code == 200 and "mindst én betingelse" in page.content.decode()
    assert MailRule.objects.filter(mailbox=mailbox).count() == 0
    for rubbish in ["²", "-1", "abc", "9" * 40, "", str(foreign.pk)]:
        for action in [
            "rule_up",
            "rule_toggle",
            "rule_delete",
            "rule_run",
            "time_delete",
            "time_toggle",
            "nej",
        ]:
            response = client.post(
                url, {"action": action, "rule": rubbish, "time_rule": rubbish}, follow=True
            )
            assert response.status_code == 200, (action, rubbish)
    assert MailRule.objects.filter(pk=foreign.pk, is_active=True).exists()  # another group's rule untouched
    assert client.get(url, {"regel": "²"}).status_code == 200
    assert client.get(url, {"regel": str(foreign.pk)}).status_code == 200


def test_a_rule_name_is_escaped_on_the_automation_page(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    make_rule(
        mailbox,
        '<script>alert("x")</script>',
        from_contains="<img src=x onerror=alert(1)>",
        set_column=mailbox.columns.first(),
    )
    html = client.get(automation_url(mailbox)).content.decode()
    assert "<script>alert" not in html and "<img src=x" not in html and "&lt;script&gt;" in html


def test_the_automation_page_is_scoped_like_every_other_page(
    client: Client, ak_member: Resident, make_resident: MakeResident
) -> None:
    other = make_mailbox("fest", roles=["fest"])
    assert client.get(automation_url(other)).status_code == 404
    assert client.post(automation_url(other), {"action": "rule_save", "name": "x"}).status_code == 404
    mine = make_mailbox(roles=["ak"])
    client.logout()
    assert client.get(automation_url(mine)).status_code == 302  # to the login


def test_the_automation_page_has_a_bounded_number_of_queries(client: Client, ak_member: Resident) -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    mailbox = make_mailbox(roles=["ak"])
    tag, gang = tags.add(mailbox, "T"), mailbox.columns.get(name="I gang")

    def queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            assert client.get(automation_url(mailbox)).status_code == 200
        return len(captured)

    make_rule(mailbox, "Én", tags_=[tag], from_contains="a", set_column=gang, set_assignee=ak_member)
    few = queries()
    for n in range(8):
        make_rule(mailbox, f"R{n}", tags_=[tag], from_contains="a", set_column=gang, set_assignee=ak_member)
        MailTimeRule.objects.create(
            mailbox=mailbox,
            name=f"T{n}",
            from_column=gang,
            after_days=3,
            to_column=col(mailbox, ColumnRole.FAERDIG),
        )
    assert queries() == few  # nine rules and eight time rules cost no more queries than one


# --- automation: time rules -------------------------------------------------------------------------------


def stale_sag(mailbox: Mailbox, column: BoardColumn, days: float, subject: str = "Gammel") -> MailThread:
    sag = MailThread.objects.create(mailbox=mailbox, subject=subject, column=column)
    MailMessage.objects.create(
        mailbox=mailbox,
        folder=MailFolder.objects.get_or_create(
            mailbox=mailbox, name="INBOX", defaults={"display_name": "INBOX", "path": "INBOX"}
        )[0],
        uid=sag.pk,
        thread=sag,
        subject=subject,
    )
    MailThread.objects.filter(pk=sag.pk).update(last_activity_at=timezone.now() - timedelta(days=days))
    return sag


def time_rule(
    mailbox: Mailbox,
    days: int = 7,
    *,
    from_column: BoardColumn | None = None,
    to_column: BoardColumn | None = None,
    is_active: bool = True,
) -> MailTimeRule:
    return MailTimeRule.objects.create(
        mailbox=mailbox,
        name="Afventer til færdig",
        from_column=from_column or col(mailbox, ColumnRole.AFVENTER),
        to_column=to_column or col(mailbox, ColumnRole.FAERDIG),
        after_days=days,
        is_active=is_active,
    )


def test_a_time_rule_moves_quiet_sager_at_exactly_n_days_and_not_before() -> None:
    mailbox = make_mailbox()
    waiting = col(mailbox, ColumnRole.AFVENTER)
    now = timezone.now()
    on_the_line = stale_sag(mailbox, waiting, 0, "På grænsen")
    MailThread.objects.filter(pk=on_the_line.pk).update(last_activity_at=now - timedelta(days=7))
    almost = stale_sag(mailbox, waiting, 0, "Næsten")
    MailThread.objects.filter(pk=almost.pk).update(
        last_activity_at=now - timedelta(days=7) + timedelta(minutes=1)
    )
    time_rule(mailbox, 7)
    moved = rules.apply_time_rules(now=now)
    assert [m.thread.subject for m in moved] == ["På grænsen"]
    assert MailThread.objects.get(pk=on_the_line.pk).column == col(mailbox, ColumnRole.FAERDIG)
    assert MailThread.objects.get(pk=almost.pk).column == waiting
    event = MailEvent.objects.get(action="rule")
    assert event.detail == "Tidsregel «Afventer til færdig»: → Færdig efter 7 dage" and event.resident is None


def test_an_inactive_time_rule_does_nothing_and_a_moved_sag_does_not_bounce() -> None:
    mailbox = make_mailbox()
    waiting, done = col(mailbox, ColumnRole.AFVENTER), col(mailbox, ColumnRole.FAERDIG)
    sag = stale_sag(mailbox, waiting, 30)
    rule = time_rule(mailbox, 7, is_active=False)
    assert rules.apply_time_rules() == []
    rule.is_active = True
    rule.save()
    time_rule(mailbox, 1, from_column=done, to_column=waiting)  # the reverse rule must not undo it at once
    assert len(rules.apply_time_rules()) == 1
    assert MailThread.objects.get(pk=sag.pk).column == done
    assert rules.apply_time_rules() == []  # idempotent
    assert MailThread.objects.get(pk=sag.pk).last_activity_at > timezone.now() - timedelta(minutes=1)  # type: ignore[operator]


def test_dry_run_changes_nothing_and_a_held_claim_skips_the_mailbox() -> None:
    mailbox = make_mailbox()
    sag = stale_sag(mailbox, col(mailbox, ColumnRole.AFVENTER), 30)
    time_rule(mailbox, 7)
    assert len(rules.apply_time_rules(dry_run=True)) == 1
    assert (
        MailThread.objects.get(pk=sag.pk).column == col(mailbox, ColumnRole.AFVENTER)
        and not MailEvent.objects.exists()
    )
    hold_claim(mailbox)
    assert rules.apply_time_rules() == []
    assert MailThread.objects.get(pk=sag.pk).column == col(mailbox, ColumnRole.AFVENTER)
    assert claimed_at(mailbox) is not None  # not released by the one that never held it


def test_activity_is_bumped_by_new_mail_and_by_manual_changes(
    ak_member: Resident, server: FakeServer
) -> None:
    mailbox = quiet_box(server)
    arrive(server, mailbox, raw_mail("Aktiv", message_id="<a@x>"))
    sag = MailThread.objects.get()
    assert sag.last_activity_at is not None
    old = timezone.now() - timedelta(days=30)

    def age() -> None:
        MailThread.objects.filter(pk=sag.pk).update(last_activity_at=old)

    for change in (
        lambda: services.move_thread(sag, mailbox.columns.get(name="I gang"), ak_member),
        lambda: services.assign_thread(sag, ak_member, ak_member),
        lambda: tags.set_for_thread(sag, [tags.add(mailbox, "T").pk], ak_member),
        lambda: arrive(server, mailbox, raw_mail("Re: Aktiv", in_reply_to="<a@x>", date=days_ago(0))),
    ):
        age()
        change()
        assert MailThread.objects.get(pk=sag.pk).last_activity_at > old + timedelta(days=1)  # type: ignore[operator]


def test_time_rule_form_refuses_the_same_column_other_mailboxs_columns_and_bad_days() -> None:
    from postkasser.forms import TimeRuleForm

    mailbox, other = make_mailbox(roles=["ak"]), make_mailbox("fest")
    a, b = col(mailbox, ColumnRole.AFVENTER), col(mailbox, ColumnRole.FAERDIG)
    ok = {"name": "T", "from_column": str(a.pk), "to_column": str(b.pk), "after_days": "7"}
    assert TimeRuleForm(ok, mailbox=mailbox).is_valid()
    assert not TimeRuleForm({**ok, "to_column": str(a.pk)}, mailbox=mailbox).is_valid()
    assert not TimeRuleForm(
        {**ok, "to_column": str(col(other, ColumnRole.FAERDIG).pk)}, mailbox=mailbox
    ).is_valid()
    for days in ("0", "366", "-1", "x", ""):
        assert not TimeRuleForm({**ok, "after_days": days}, mailbox=mailbox).is_valid(), days


def test_time_rules_are_edited_on_the_automation_page(client: Client, ak_member: Resident) -> None:
    mailbox = make_mailbox(roles=["ak"])
    a, b = col(mailbox, ColumnRole.AFVENTER), col(mailbox, ColumnRole.FAERDIG)
    url = automation_url(mailbox)
    client.post(
        url,
        {
            "action": "time_save",
            "name": "T",
            "from_column": str(a.pk),
            "to_column": str(b.pk),
            "after_days": "5",
            "is_active": "on",
        },
    )
    rule = MailTimeRule.objects.get()
    assert rule.mailbox == mailbox and rule.after_days == 5
    assert "5 dage" in client.get(url).content.decode()
    client.post(url, {"action": "time_toggle", "time_rule": str(rule.pk)})
    rule.refresh_from_db()
    assert not rule.is_active
    page = client.post(
        url,
        {
            "action": "time_save",
            "name": "T",
            "from_column": str(a.pk),
            "to_column": str(a.pk),
            "after_days": "5",
        },
    )
    assert "forskellige" in page.content.decode()
    client.post(url, {"action": "time_delete", "time_rule": str(rule.pk)})
    assert not MailTimeRule.objects.exists()


def test_the_time_rules_job_is_scheduled_hourly_and_the_command_works(
    capsys: pytest.CaptureFixture[str], settings: LazySettings
) -> None:
    from celery.schedules import crontab
    from django.core.management import call_command

    entry = settings.CELERY_BEAT_SCHEDULE["apply-mail-time-rules"]
    assert entry["task"] == "postkasser.tasks.apply_time_rules"
    assert entry["schedule"] == crontab(minute=17)
    others = [e["schedule"] for k, e in settings.CELERY_BEAT_SCHEDULE.items() if k != "apply-mail-time-rules"]
    assert crontab(minute=17) not in others
    mailbox = make_mailbox()
    sag = stale_sag(mailbox, col(mailbox, ColumnRole.AFVENTER), 30)
    time_rule(mailbox, 7)
    call_command("apply_mail_time_rules", "--dry-run")
    assert (
        "ville flytte" in capsys.readouterr().out
        and MailThread.objects.get(pk=sag.pk).column.role == "afventer"
    )
    call_command("apply_mail_time_rules")
    assert (
        "flyttede" in capsys.readouterr().out and MailThread.objects.get(pk=sag.pk).column.role == "faerdig"
    )
    from postkasser import tasks

    tasks.apply_time_rules()
    assert MailEvent.objects.filter(action="rule").count() == 1


# --- automation: notifications -----------------------------------------------------------------------------


def mail_notifications(category: str | None = None) -> list[Notification]:
    found = Notification.objects.filter(category__startswith="mail.")
    return list((found.filter(category=category) if category else found).order_by("pk"))


def second_member(make_resident: MakeResident) -> Resident:
    return make_resident("b@intern.dk", roles=(Role.AK,), first_name="Bente")


def test_a_new_sag_notifies_the_group_with_subject_and_sender_but_never_the_body(
    ak_member: Resident, server: FakeServer, make_resident: MakeResident
) -> None:
    bente = second_member(make_resident)
    mailbox = quiet_box(server)
    arrive(
        server, mailbox, raw_mail("Kælderen", sender="Mette <mette@example.com>", body="HEMMELIG brødtekst")
    )
    sag = MailThread.objects.get()
    rows = mail_notifications("mail.new_sag")
    assert {r.recipient_id for r in rows} == {ak_member.pk, bente.pk}
    for row in rows:
        assert row.title == "Ny sag i AK-gruppen" and row.body == "Kælderen — fra Mette"
        assert row.url == reverse("postkasser:thread", args=["ak", sag.pk])
        assert "HEMMELIG" not in row.title + row.body


def test_nothing_is_sent_for_a_first_sync_or_a_moved_mail(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox, sag = move_setup(server)  # the history import of the setup notified nobody
    assert mail_notifications() == []
    server.copyuid = False
    client.post(move_url(mailbox, sag), {"mappe": str(folder_of(mailbox, "Fest").pk)})
    assert mail_notifications() == []


def test_one_notification_per_sag_per_sync_however_many_mails(
    ak_member: Resident, server: FakeServer
) -> None:
    mailbox = quiet_box(server)
    arrive(
        server,
        mailbox,
        raw_mail("Tråd", message_id="<t1@x>"),
        raw_mail("Re: Tråd", in_reply_to="<t1@x>"),
        raw_mail("Re: Tråd", in_reply_to="<t1@x>"),
    )
    assert len(mail_notifications("mail.new_sag")) == 1 and MailThread.objects.count() == 1
    sag = MailThread.objects.get()
    MailThread.objects.filter(pk=sag.pk).update(assignee=ak_member)
    arrive(
        server,
        mailbox,
        raw_mail("Re: Tråd", in_reply_to="<t1@x>"),
        raw_mail("Re: Tråd", in_reply_to="<t1@x>"),
    )
    replies = mail_notifications("mail.assigned_reply")
    assert len(replies) == 1 and replies[0].recipient_id == ak_member.pk
    assert len(mail_notifications("mail.new_sag")) == 1  # no second "new sag"


def test_a_reply_on_an_unassigned_sag_notifies_nobody(ak_member: Resident, server: FakeServer) -> None:
    mailbox = quiet_box(server)
    arrive(server, mailbox, raw_mail("Tråd", message_id="<t1@x>"))
    Notification.objects.all().delete()
    arrive(server, mailbox, raw_mail("Re: Tråd", in_reply_to="<t1@x>"))
    assert mail_notifications() == []


def test_handing_a_sag_to_someone_else_notifies_them_but_not_yourself(
    client: Client, ak_member: Resident, server: FakeServer, make_resident: MakeResident
) -> None:
    bente = second_member(make_resident)
    mailbox = make_mailbox(roles=["ak"])
    sag = MailThread.objects.create(mailbox=mailbox, subject="Sag", column=col(mailbox, ColumnRole.NY))
    url = reverse("postkasser:set_assignee", args=["ak", sag.pk])
    client.post(url, {"assignee": str(ak_member.pk)})  # to myself
    assert mail_notifications() == []
    client.post(url, {"assignee": str(bente.pk)})
    (row,) = mail_notifications("mail.assigned")
    assert row.recipient_id == bente.pk and row.body == "Sag" and row.url.endswith(f"/sag/{sag.pk}")


def test_a_rule_that_assigns_notifies_the_assignee_once(
    ak_member: Resident, server: FakeServer, make_resident: MakeResident
) -> None:
    bente = second_member(make_resident)
    mailbox = quiet_box(server)
    make_rule(mailbox, "Til Bente", from_contains="mette", set_assignee=bente)
    arrive(server, mailbox, raw_mail("Ny sag"))
    assigned = mail_notifications("mail.assigned")
    assert [r.recipient_id for r in assigned] == [bente.pk]
    new_sag = mail_notifications("mail.new_sag")
    assert [r.recipient_id for r in new_sag] == [ak_member.pk]  # Bente has her own, personal one
    arrive(server, mailbox, raw_mail("Re: Ny sag", in_reply_to=MailMessage.objects.get().message_id))
    assert len(mail_notifications("mail.assigned_reply")) == 1


def test_a_notification_failure_does_not_fail_the_sync(
    ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notifications import services as notification_services

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("push er nede")

    monkeypatch.setattr(notification_services, "notify", boom)
    mailbox = quiet_box(server)
    arrive(server, mailbox, raw_mail("Kommer alligevel"))
    assert MailThread.objects.count() == 1


def test_the_postkasser_backend_can_send_for_real_while_the_site_stays_on_another(
    client: Client, ak_member: Resident, server: FakeServer, settings: LazySettings
) -> None:
    """POSTKASSER_EMAIL_BACKEND overrides EMAIL_BACKEND for group mail only; empty follows it."""
    make_mailbox(roles=["ak"])
    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    settings.POSTKASSER_EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    data = {"to": "x@example.com", "cc": "", "subject": "Hej", "body": "Tekst"}
    client.post(reverse("postkasser:compose", args=["ak"]), data)
    assert len(mail.outbox) == 1
    settings.POSTKASSER_EMAIL_BACKEND = ""
    client.post(reverse("postkasser:compose", args=["ak"]), data)
    assert len(mail.outbox) == 1  # followed EMAIL_BACKEND (dummy) this time


# --- notifications: recipients, recency, background sending -------------------------------------------------


@pytest.fixture
def notified(monkeypatch: pytest.MonkeyPatch) -> list[tuple[bool, object]]:
    """Every `notify.after_sync`: (background, the mailbox's claim at that moment)."""
    calls: list[tuple[bool, object]] = []
    real = notify.after_sync

    def spy(mailbox: Mailbox, **kwargs: object) -> None:
        calls.append((bool(kwargs["background"]), claimed_at(mailbox)))
        real(mailbox, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(notify, "after_sync", spy)
    return calls


def test_a_reply_does_not_notify_an_assignee_who_left_the_group(
    ak_member: Resident, server: FakeServer, make_resident: MakeResident
) -> None:
    bente = second_member(make_resident)
    mailbox = quiet_box(server)
    arrive(server, mailbox, raw_mail("Tråd", message_id="<t1@x>"))
    sag = MailThread.objects.get()
    MailThread.objects.filter(pk=sag.pk).update(assignee=bente)
    RoleAssignment.objects.filter(resident=bente).delete()  # Bente left the group at the month change
    Notification.objects.all().delete()
    arrive(server, mailbox, raw_mail("Re: Tråd", in_reply_to="<t1@x>"))
    assert mail_notifications("mail.assigned_reply") == []


def test_old_mail_dragged_into_a_synced_folder_runs_no_rules_and_notifies_nobody(
    ak_member: Resident, server: FakeServer
) -> None:
    server.add_folder("Arkiv2023")
    mailbox = quiet_box(server)  # Arkiv2023 has had its first sync, while empty
    tag = tags.add(mailbox, "Faktura")
    make_rule(mailbox, "Alt", tags_=[tag], from_contains="mette")
    for uid in range(1, 31):
        server.put(
            uid,
            raw_mail(f"Gammel {uid}", message_id=f"<old{uid}@x>", date=days_ago(400)),
            {r"\Seen"},
            folder="Arkiv2023",
        )
    server.put(40, raw_mail("Gammel ulæst", message_id="<oldu@x>", date=days_ago(400)), folder="Arkiv2023")
    services.sync(mailbox)
    assert mail_notifications() == []
    assert not MailThread.objects.filter(tags=tag).exists() and not MailEvent.objects.filter(action="rule")
    # still sager, as before — read history in Færdig (like a first sync), the unread one where somebody sees it
    assert MailThread.objects.filter(column=col(mailbox, ColumnRole.FAERDIG)).count() == 30
    assert list(
        MailThread.objects.filter(column=col(mailbox, ColumnRole.NY)).values_list("subject", flat=True)
    ) == ["Gammel ulæst"]


def test_a_recent_mail_that_was_already_read_gets_the_rules_but_announces_nothing(
    ak_member: Resident, server: FakeServer
) -> None:
    mailbox = quiet_box(server)
    tag = tags.add(mailbox, "T")
    make_rule(mailbox, "R", tags_=[tag], from_contains="mette")
    server.put(1, raw_mail("Læst på mobilen"), {r"\Seen"})
    server.put(2, raw_mail("Uden dato").replace(b"Date: " + _RECENT_DATE.encode() + b"\n", b""))
    services.sync(mailbox)
    by_subject = {t.subject: t for t in MailThread.objects.all()}
    assert by_subject["Læst på mobilen"].tags.exists()  # read in webmail before the sync: rules still run
    assert by_subject["Uden dato"].tags.exists()  # no Date: treated as recent
    assert [r.body for r in mail_notifications("mail.new_sag")] == ["Uden dato — fra Mette"]


def test_a_pile_of_new_sager_announces_only_the_first_twenty(ak_member: Resident, server: FakeServer) -> None:
    mailbox = quiet_box(server)
    arrive(server, mailbox, *[raw_mail(f"Sag {n}", message_id=f"<p{n}@x>") for n in range(25)])
    assert MailThread.objects.count() == 25
    assert len(mail_notifications("mail.new_sag")) == notify.MAX_NEW_SAG_NOTICES == 20


def test_a_sag_that_ends_the_sync_in_faerdig_is_not_announced(
    ak_member: Resident, server: FakeServer
) -> None:
    mailbox = quiet_box(server)
    make_rule(mailbox, "Nyhedsbrev", from_contains="nyhed", set_column=col(mailbox, ColumnRole.FAERDIG))
    arrive(server, mailbox, raw_mail("Brev", sender="Nyhed <nyhed@lev.dk>"), raw_mail("Kunde"))
    assert [r.body for r in mail_notifications("mail.new_sag")] == ["Kunde — fra Mette"]


def test_each_notification_is_tried_on_its_own(
    ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notifications import services as notification_services

    real = notification_services.notify
    calls: list[str] = []

    def flaky(*args: object, **kwargs: object) -> int:
        calls.append(str(kwargs["body"]))
        if len(calls) == 1:
            raise RuntimeError("push er nede")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(notification_services, "notify", flaky)
    mailbox = quiet_box(server)
    arrive(server, mailbox, raw_mail("Første"), raw_mail("Anden"))
    assert len(calls) == 2 and [r.body for r in mail_notifications("mail.new_sag")] == ["Anden — fra Mette"]


def test_web_paths_notify_in_the_background_after_the_claim_is_released(
    client: Client,
    ak_member: Resident,
    server: FakeServer,
    notified: list[tuple[bool, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = quiet_box(server)
    notified.clear()
    server.put(1, raw_mail("Via knappen"))
    client.post(reverse("postkasser:refresh", args=["ak"]))
    assert notified == [(True, None)]
    # the sync command (and so the Celery task) is the one place that may push in the foreground
    from django.core.management import call_command

    server.put(2, raw_mail("Via kommandoen"))
    notified.clear()
    call_command("sync_mailboxes")
    assert notified == [(False, None)]
    # a move, a reply's follow-up sync, and "kør på eksisterende sager"
    notified.clear()
    server.add_folder("Fest")
    services.sync(mailbox)
    sag = MailThread.objects.get(subject="Via knappen")
    server.copyuid = False
    notified.clear()
    client.post(
        reverse("postkasser:move_thread", args=["ak", sag.pk]), {"mappe": str(folder_of(mailbox, "Fest").pk)}
    )
    assert notified == [(True, None)]
    notified.clear()
    message = MailMessage.objects.filter(folder__special_use="inbox").first()
    assert message is not None
    client.post(
        reverse("postkasser:reply", args=["ak", message.pk]),
        {"to": "mette@example.com", "subject": "Re: x", "body": "Hej"},
    )
    assert notified == [(True, None)]
    handed: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        notify, "assigned", lambda *a, **kw: handed.append((kw["by"], kw["background"], claimed_at(mailbox)))
    )
    rule = make_rule(mailbox, "Til mig", from_contains="mette", set_assignee=ak_member)
    MailThread.objects.update(assignee=None)
    client.post(automation_url(mailbox), {"action": "rule_run", "rule": str(rule.pk)})
    assert handed and all(entry == (ak_member, True, None) for entry in handed)


def test_run_on_existing_does_not_notify_the_person_who_pressed_the_button(
    client: Client, ak_member: Resident, server: FakeServer, make_resident: MakeResident
) -> None:
    bente = second_member(make_resident)
    mailbox = quiet_box(server)
    arrive(server, mailbox, raw_mail("Åben"))
    Notification.objects.all().delete()
    mine = make_rule(mailbox, "Til mig", from_contains="mette", set_assignee=ak_member)
    client.post(automation_url(mailbox), {"action": "rule_run", "rule": str(mine.pk)})
    assert MailThread.objects.get().assignee == ak_member and mail_notifications() == []
    hers = make_rule(mailbox, "Til Bente", from_contains="mette", set_assignee=bente)
    client.post(automation_url(mailbox), {"action": "rule_run", "rule": str(hers.pk)})
    assert [r.recipient_id for r in mail_notifications("mail.assigned")] == [bente.pk]


def test_born_sager_of_a_rolled_back_folder_are_not_announced(
    ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = quiet_box(server)
    real_save = MailFolder.save

    def failing(self: MailFolder, *args: object, **kwargs: object) -> None:
        if self.name == "INBOX" and kwargs.get("update_fields") == ["uidvalidity", "last_synced_at"]:
            raise imap.MailboxError("disken er fuld")
        real_save(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(MailFolder, "save", failing)
    server.put(1, raw_mail("Rulles tilbage"))
    services.sync(mailbox)
    assert "disken er fuld" in Mailbox.objects.get().last_sync_error
    assert MailThread.objects.count() == 0 and mail_notifications() == []


# --- rules and time rules: edge cases -----------------------------------------------------------------------


def test_a_folder_that_disappears_switches_its_rules_off_and_says_why(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    server.add_folder("Faktura")
    mailbox = quiet_box(server)
    tag = tags.add(mailbox, "F")
    both = make_rule(
        mailbox, "Både", tags_=[tag], from_contains="mette", folder=folder_of(mailbox, "Faktura")
    )
    only = make_rule(mailbox, "Kun mappe", tags_=[tag], folder=folder_of(mailbox, "Faktura"))
    server.folders["Fakturaer"] = server.folders.pop("Faktura")  # renamed at one.com
    services.sync(mailbox)
    for rule in (both, only):
        rule.refresh_from_db()
        assert rule.folder is None and not rule.is_active
        assert rule.disabled_reason == "Slået fra: mappen «Faktura» findes ikke længere"
    assert MailRule.objects.count() == 2  # kept, not cascaded away
    assert (
        "Slået fra: mappen «Faktura» findes ikke længere"
        in client.get(automation_url(mailbox)).content.decode()
    )
    # even switched on by hand, a rule with no condition left matches nothing
    only.is_active = True
    message = make_message(mailbox, 77, subject="x", from_address="mette@example.com")
    assert not rules.matches(only, message)
    # and the toggle refuses to widen "mail in Faktura from mette" to "from mette"
    client.post(automation_url(mailbox), {"action": "rule_toggle", "rule": str(both.pk)})
    both.refresh_from_db()
    assert not both.is_active
    arrive(server, mailbox, raw_mail("Ny"))
    assert not MailThread.objects.filter(tags=tag).exists()


def test_a_deleted_column_switches_its_time_rules_off_and_they_do_nothing(
    client: Client, ak_member: Resident
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    waiting = col(mailbox, ColumnRole.AFVENTER)
    stale_sag(mailbox, waiting, 30)
    keep = time_rule(mailbox, 7)
    other = time_rule(mailbox, 7, from_column=mailbox.columns.get(name="I gang"))
    columns.delete(waiting)
    keep.refresh_from_db()
    other.refresh_from_db()
    assert keep.from_column is None and not keep.is_active
    assert keep.disabled_reason == "Slået fra: kolonnen «Afventer svar» er slettet"
    assert other.is_active and other.to_column is not None  # an unrelated rule is left alone
    page = client.get(automation_url(mailbox)).content.decode()
    assert "Slået fra: kolonnen «Afventer svar» er slettet" in page
    client.post(automation_url(mailbox), {"action": "time_toggle", "time_rule": str(keep.pk)})
    assert not MailTimeRule.objects.get(pk=keep.pk).is_active  # cannot be switched on without its columns
    MailTimeRule.objects.filter(pk=keep.pk).update(is_active=True)  # forced on: still does nothing
    assert rules.apply_time_rules() == []


def test_one_mailbox_losing_its_claim_does_not_stop_the_time_rules_of_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = make_mailbox("aa", name="A"), make_mailbox("bb", name="B")
    for box in (first, second):
        stale_sag(box, col(box, ColumnRole.AFVENTER), 30)
        time_rule(box, 7)
    real = services.Claim.renew

    def renew(self: services.Claim) -> None:
        if self.mailbox.pk == first.pk:
            raise services.SyncBusy(services.SYNC_LOST)
        real(self)

    monkeypatch.setattr(services.Claim, "renew", renew)
    moves = rules.apply_time_rules()
    assert [m.thread.mailbox_id for m in moves] == [second.pk]
    assert claimed_at(first) is None and claimed_at(second) is None


def test_a_dry_run_needs_no_claim() -> None:
    mailbox = make_mailbox()
    stale_sag(mailbox, col(mailbox, ColumnRole.AFVENTER), 30)
    time_rule(mailbox, 7)
    hold_claim(mailbox)
    assert len(rules.apply_time_rules(dry_run=True)) == 1
    assert claimed_at(mailbox) is not None


def test_a_time_rule_cannot_move_the_opposite_way_of_an_active_one() -> None:
    mailbox = make_mailbox()
    waiting, done = col(mailbox, ColumnRole.AFVENTER), col(mailbox, ColumnRole.FAERDIG)
    forward = time_rule(mailbox, 7, from_column=waiting, to_column=done)
    data = {"name": "Tilbage", "from_column": done.pk, "to_column": waiting.pk, "after_days": 3}
    form = TimeRuleForm({**data, "is_active": "on"}, mailbox=mailbox)
    assert not form.is_valid() and "frem og tilbage" in str(form.errors)
    assert TimeRuleForm(data, mailbox=mailbox).is_valid()  # inactive: no ping-pong yet
    assert TimeRuleForm(
        {"name": "x", "from_column": waiting.pk, "to_column": done.pk, "after_days": 9, "is_active": "on"},
        mailbox=mailbox,
        instance=forward,
    ).is_valid()  # editing the rule itself is fine
    backward = time_rule(mailbox, 3, from_column=done, to_column=waiting, is_active=False)
    with pytest.raises(rules.RuleError):
        rules.check_time_rule_can_be_on(backward)


def test_cc_is_cached_and_to_contains_matches_to_or_cc(ak_member: Resident, server: FakeServer) -> None:
    mailbox = quiet_box(server)
    tag = tags.add(mailbox, "Styret")
    make_rule(mailbox, "Til styret", tags_=[tag], to_contains="styret@")
    arrive(
        server,
        mailbox,
        raw_mail("Med cc", cc="Styret <styret@gahk.dk>, andre@x.dk"),
        raw_mail("Uden"),
    )
    with_cc = MailMessage.objects.get(subject="Med cc")
    assert "styret@gahk.dk" in with_cc.cc and MailMessage.objects.get(subject="Uden").cc == ""
    assert list(MailThread.objects.filter(tags=tag).values_list("subject", flat=True)) == ["Med cc"]
    assert RuleForm(mailbox=mailbox).fields["to_contains"].label == "Til/Cc indeholder"


def test_the_automation_page_points_to_the_notification_settings_and_confirms_destructive_buttons(
    client: Client, ak_member: Resident
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    make_rule(mailbox, 'R "<b>"', from_contains="x", set_column=col(mailbox, ColumnRole.NY))
    time_rule(mailbox, 7)
    page = client.get(automation_url(mailbox)).content.decode()
    assert reverse("notifications:preferences") in page and "Nye sager i gruppens postkasse" in page
    assert 'data-confirm="Slet «R &quot;&lt;b&gt;&quot;»?"' in page  # escaped, never raw
    assert "<b>" not in page.split("data-confirm=")[1]
    assert 'data-confirm="Køre «R &quot;&lt;b&gt;&quot;»' in page
    assert page.count("confirm(this.dataset.confirm)") == 3  # rule: run, delete; time rule: delete


def test_editing_a_rule_whose_assignee_left_shows_and_refuses_them(make_resident: MakeResident) -> None:
    bente = second_member(make_resident)
    mailbox = make_mailbox(roles=["ak"])
    rule = make_rule(mailbox, "Til Bente", from_contains="x", set_assignee=bente)
    RoleAssignment.objects.filter(resident=bente).delete()
    form = RuleForm(instance=rule, mailbox=mailbox)
    labels = dict(form.fields["set_assignee"].choices)  # type: ignore[attr-defined]
    assert labels[bente.pk] == "Bente Beboer (ikke længere i gruppen)"
    data = {"name": "Til Bente", "from_contains": "x", "set_assignee": str(bente.pk), "is_active": "on"}
    bound = RuleForm(data, instance=rule, mailbox=mailbox)
    assert not bound.is_valid() and "kan ikke længere åbne postkassen" in str(bound.errors["set_assignee"])
    assert (
        RuleForm({**data, "set_assignee": "", "add_tags": []}, instance=rule, mailbox=mailbox).errors.get(
            "set_assignee"
        )
        is None
    )
    other = make_mailbox("fest", roles=["fest"])  # someone else's former member is still not a choice
    assert bente.pk not in dict(RuleForm(mailbox=other).fields["set_assignee"].choices)  # type: ignore[attr-defined]


def test_rules_are_loaded_inside_the_same_savepoint_as_their_application(
    ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from django.db import connection

    mailbox = quiet_box(server)
    depths: list[int] = []
    real_automate, real_load = services._automate, rules.load

    def automate(*args: object, **kwargs: object) -> None:
        depths.append(len(connection.savepoint_ids))
        real_automate(*args, **kwargs)  # type: ignore[arg-type]

    def load(box: Mailbox) -> list[MailRule]:
        depths.append(len(connection.savepoint_ids))
        return real_load(box)

    monkeypatch.setattr(services, "_automate", automate)
    monkeypatch.setattr(rules, "load", load)
    arrive(server, mailbox, raw_mail("Ny"))
    assert depths[1] == depths[0] + 1


def test_a_failing_rule_load_still_files_the_mail(
    ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(box: Mailbox) -> list[MailRule]:
        raise RuntimeError("regler er nede")

    monkeypatch.setattr(rules, "load", boom)
    mailbox = quiet_box(server)
    arrive(server, mailbox, raw_mail("Ny"))
    assert MailThread.objects.count() == 1 and len(mail_notifications("mail.new_sag")) == 1


def test_activity_is_when_the_mail_arrived_not_its_date_header(
    ak_member: Resident, server: FakeServer
) -> None:
    mailbox = quiet_box(server)
    arrive(server, mailbox, raw_mail("Gårsdagens", date=days_ago(1)))
    sag = MailThread.objects.get()
    assert sag.last_message_at is not None and sag.last_message_at < timezone.now() - timedelta(hours=23)
    assert sag.last_activity_at is not None and sag.last_activity_at > timezone.now() - timedelta(minutes=1)


# --- imaplib's BAD answers -------------------------------------------------------------------------------------


Answer = tuple[str, list]


class ScriptedClient:
    """The slice of imaplib's client that `imap.Connection` calls, over a FakeServer — so a test runs the
    REAL Connection (and its BAD handling) through a whole sync, move or unread."""

    def __init__(self, server: FakeServer, bad_select: set[str]) -> None:
        self.server, self.bad_select = server, bad_select
        self.selected = ""
        self.untagged_responses: dict[str, list] = {}

    @property
    def data(self) -> FolderData:
        return self.server.folders[self.selected]

    def select(self, quoted: str) -> Answer:
        name = quoted.strip('"')
        if name in self.bad_select:
            raise imaplib.IMAP4.error("SELECT command error: BAD [b'Error in IMAP command']")
        self.selected = name
        return "OK", [b"1"]

    def response(self, code: str) -> Answer:
        return code, [str(self.data.uidvalidity).encode()]

    def list(self, _a: str, _b: str) -> Answer:
        return "OK", self.server.list_lines()

    def capability(self) -> Answer:
        return "OK", [b"IMAP4REV1 MOVE"]

    def uid(self, command: str, *args: str) -> Answer:
        data = self.data
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in sorted(data.messages)).encode()]
        if command == "FETCH":
            uidset, spec = args
            uids = (
                [u for u in data.messages if u >= int(uidset[:-2])]
                if uidset.endswith(":*")
                else [int(u) for u in uidset.split(",")]
            )
            out: list = []
            for uid in uids:
                raw, flags = data.messages[uid], " ".join(sorted(data.flags[uid]))
                if "HEADER" in spec:
                    head = raw.split(b"\n\n", 1)[0] + b"\n\n"
                    meta = f"{uid} (UID {uid} FLAGS ({flags}) RFC822.SIZE {len(raw)} BODY[HEADER] {{{len(head)}}}"
                    out += [(meta.encode(), head), b")"]
                else:
                    out.append(f"{uid} (UID {uid} FLAGS ({flags}))".encode())
            return "OK", out
        store_uid, op, text = args  # STORE
        stored = set(text.strip("()").split())
        if op.startswith("-"):
            data.flags[int(store_uid)] -= stored
        else:
            data.flags[int(store_uid)] |= stored
        return "OK", [b"done"]

    def _simple_command(self, _name: str, command: str, wanted: str, quoted: str) -> Answer:
        assert command == "MOVE"
        target = self.server.folders[quoted.strip('"')]
        for uid in imap.expand_uid_set(wanted.encode()):
            raw, flags = self.server.take(uid, self.selected)
            new = max(target.messages, default=0) + 1
            target.messages[new], target.flags[new] = raw, flags
        return "OK", [b"done"]  # no COPYUID: the target folder is synced instead


def use_scripted_client(monkeypatch: pytest.MonkeyPatch, server: FakeServer, *, bad_select: set[str]) -> None:
    @contextmanager
    def connect(mailbox: Mailbox) -> Iterator[imap.Connection]:
        yield imap.Connection(ScriptedClient(server, bad_select))  # type: ignore[arg-type]

    monkeypatch.setattr(imap, "connect", connect)


def test_every_imaplib_call_turns_a_bad_answer_into_a_mailbox_error() -> None:
    class Refusing:
        def __getattr__(self, name: str) -> Callable[..., None]:
            def call(*_args: object, **_kwargs: object) -> None:
                raise imaplib.IMAP4.error(f"{name} command error: BAD")

            return call

    conn = imap.Connection(Refusing())  # type: ignore[arg-type]
    for call in (
        lambda: conn.select("INBOX"),
        conn.all_uids,
        lambda: conn.fetch([1], "(UID FLAGS)"),
        lambda: conn.fetch_message(1),
        lambda: conn.fetch_flags(1),
        lambda: conn.add_flags(1, r"\Seen"),
        lambda: conn.remove_flags(1, r"\Seen"),
        lambda: conn.append("Sent", b"x"),
        conn.capabilities,
        conn.list_folders,
    ):
        with pytest.raises(imap.MailboxError, match="BAD"):
            call()


def test_a_bad_select_skips_only_that_folder_in_a_sync(
    ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.add_folder("Fest")
    server.add_folder("Arkiv")
    mailbox = quiet_box(server)
    use_scripted_client(monkeypatch, server, bad_select={"Fest"})
    server.put(1, raw_mail("Fest-mail"), folder="Fest")
    server.put(1, raw_mail("Arkiv-mail"), folder="Arkiv")
    server.put(1, raw_mail("Indbakke-mail"))
    services.sync(mailbox)
    assert set(MailMessage.objects.values_list("subject", flat=True)) == {"Arkiv-mail", "Indbakke-mail"}
    error = Mailbox.objects.get().last_sync_error
    assert "Fest" in error and "BAD" in error
    assert Mailbox.objects.get().last_synced_at is not None


def test_a_bad_select_in_one_folder_does_not_stop_mark_unread_or_a_move_in_the_others(
    ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.add_folder("Fest")
    server.add_folder("Arkiv")
    mailbox = make_mailbox(roles=["ak"])
    server.put(1, raw_mail("Indbakke", date=days_ago(1)), {r"\Seen"})
    server.put(1, raw_mail("Fest", date=days_ago(2)), {r"\Seen"}, folder="Fest")
    services.sync(mailbox)
    inbox_mail, fest_mail = (
        MailMessage.objects.order_by("folder__path")[1],
        MailMessage.objects.order_by("folder__path")[0],
    )
    assert (inbox_mail.subject, fest_mail.subject) == ("Indbakke", "Fest")
    use_scripted_client(monkeypatch, server, bad_select={"INBOX"})  # the first group is the one refused
    with pytest.raises(imap.MailboxError, match="BAD"):
        services.mark_unread(mailbox, [inbox_mail, fest_mail], by=ak_member)
    assert (
        not MailMessage.objects.get(pk=fest_mail.pk).seen and MailMessage.objects.get(pk=inbox_mail.pk).seen
    )
    outcome = services.move_messages(
        mailbox, [inbox_mail, fest_mail], folder_of(mailbox, "Arkiv"), moved_by=ak_member
    )
    assert outcome.moved == 1 and len(outcome.errors) == 1 and "BAD" in outcome.errors[0]
    assert list(server.folders["Arkiv"].messages) == [1] and 1 in server.folders["INBOX"].messages


# --- the sent copy and the sent folder ----------------------------------------------------------------------


def test_a_reply_shows_its_sent_copy_in_the_sag_at_once(
    client: Client, ak_member: Resident, server: FakeServer, notified: list[tuple[bool, object]]
) -> None:
    mailbox = quiet_box(server)
    arrive(server, mailbox, raw_mail("Spørgsmål", message_id="<q@x>"))
    message = MailMessage.objects.get()
    server.selected.clear()
    notified.clear()
    client.post(
        reverse("postkasser:reply", args=["ak", message.pk]),
        {"to": "mette@example.com", "subject": "Re: Spørgsmål", "body": "Kl. 20"},
    )
    sag = MailThread.objects.get()
    assert sag.messages.count() == 2  # the question and the sent copy, no separate sync in between
    assert sag.messages.filter(folder__special_use="sent").exists()
    assert server.selected[-1] == "Sent" and "INBOX" not in server.selected[server.selected.index("Sent") :]
    assert notified == [(True, None)] and claimed_at(mailbox) is None


def test_a_new_mail_from_the_intranet_joins_its_sag_at_once_and_a_busy_claim_skips_silently(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    mailbox = quiet_box(server)
    client.post(
        reverse("postkasser:compose", args=["ak"]), {"to": "a@example.com", "subject": "Nyt", "body": "Hej"}
    )
    sag = MailThread.objects.get()
    assert sag.messages.count() == 1 and sag.pending_message_id == ""
    hold_claim(mailbox)
    response = client.post(
        reverse("postkasser:compose", args=["ak"]), {"to": "a@example.com", "subject": "Andet", "body": "Hej"}
    )
    assert response.status_code == 302 and len(mail.outbox) == 2
    other = MailThread.objects.get(subject="Andet")
    assert other.messages.count() == 0 and other.pending_message_id  # the next sync joins it
    assert claimed_at(mailbox) is not None  # still the other holder's
    Mailbox.objects.filter(pk=mailbox.pk).update(sync_started_at=None)
    services.sync(mailbox)
    other.refresh_from_db()
    assert other.messages.count() == 1 and other.pending_message_id == ""


def folders_for(mailbox: Mailbox, *names: str) -> None:
    for name in names:
        MailFolder.objects.create(
            mailbox=mailbox,
            name=name,
            display_name=name.rsplit(".", 1)[-1],
            path=name.replace(".", "/"),
            depth=name.count("."),
            delimiter=".",
            special_use="sent" if "Sent" in name else ("inbox" if name == "INBOX" else ""),
        )


def test_the_sent_folder_is_chosen_among_the_mailboxs_own_folders() -> None:
    mailbox = make_mailbox(sent_folder="")
    assert MailboxForm().fields["sent_folder"].choices == [("", AUTOMATIC_SENT)]  # type: ignore[attr-defined]
    folders_for(mailbox, "INBOX", "INBOX.Sent", "INBOX.Sendte")
    choices = dict(MailboxForm(instance=mailbox).fields["sent_folder"].choices)  # type: ignore[attr-defined]
    assert list(choices) == ["", "INBOX", "INBOX.Sendte", "INBOX.Sent"]
    assert choices[""] == AUTOMATIC_SENT and "INBOX/Sendte" in choices["INBOX.Sendte"]
    assert "INBOX.Sendte" in choices["INBOX.Sendte"]  # display path and the raw name
    base = {
        "name": "AK-gruppen", "slug": "ak", "address": "ak@gahk.dk", "username": "", "password": "",
        "imap_host": "imap.one.com", "imap_port": 993, "smtp_host": "send.one.com", "smtp_port": 465,
        "signature": "", "roles": "[]", "is_active": "on",
    }  # fmt: skip
    for good in ("", "INBOX.Sendte"):
        assert MailboxForm({**base, "sent_folder": good}, instance=mailbox).is_valid()
    assert "sent_folder" in MailboxForm({**base, "sent_folder": "Sendt"}, instance=mailbox).errors
    mailbox.sent_folder = "Sendt"  # a stale value somebody typed earlier stays acceptable (and visible)
    form = MailboxForm({**base, "sent_folder": "Sendt"}, instance=mailbox)
    assert form.is_valid() and "Sendt" in dict(form.fields["sent_folder"].choices)  # type: ignore[attr-defined]


def test_an_empty_or_stale_sent_folder_falls_back_to_the_server_marked_one(
    client: Client, make_resident: MakeResident
) -> None:
    mailbox = make_mailbox(sent_folder="")
    assert services.sent_folder_name(mailbox) == "" and not services.sent_folder_is_stale(mailbox)
    folders_for(mailbox, "INBOX", "INBOX.Sent", "Sent")
    assert services.sent_folder_name(mailbox) == "Sent"  # top level first
    mailbox.sent_folder = "INBOX.Sent"
    assert services.sent_folder_name(mailbox) == "INBOX.Sent" and not services.sent_folder_is_stale(mailbox)
    mailbox.sent_folder = "Sendt"
    mailbox.save()
    assert services.sent_folder_name(mailbox) == "Sent" and services.sent_folder_is_stale(mailbox)
    admin = make_resident("adm3@intern.dk", roles=(Role.ADMINISTRATOR,))
    Resident.objects.filter(pk=admin.pk).update(is_superuser=True)
    client.force_login(admin)
    page = client.get(reverse("admin:postkasser_mailbox_changelist")).content.decode()
    assert "Sendt-mappe findes ikke" in page and "«Sendt» findes ikke på serveren" in page
    Mailbox.objects.filter(pk=mailbox.pk).update(sent_folder="Sent")
    assert (
        "findes ikke på serveren"
        not in client.get(reverse("admin:postkasser_mailbox_changelist")).content.decode()
    )


def test_a_mailbox_with_no_known_sent_folder_says_so_when_sending(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    make_mailbox(roles=["ak"], sent_folder="")
    response = client.post(
        reverse("postkasser:compose", args=["ak"]),
        {"to": "a@example.com", "subject": "Nyt", "body": "Hej"},
        follow=True,
    )
    assert len(mail.outbox) == 1 and "kender ikke en Sendt-mappe endnu" in response.content.decode()
    assert server.appended == []


# --- recent mail goes by INTERNALDATE, not the sender's Date ---------------------------------------------


def tagged_subjects(mailbox: Mailbox) -> set[str]:
    return set(
        MailThread.objects.filter(mailbox=mailbox, tags__isnull=False).values_list("subject", flat=True)
    )


def test_parse_fetch_reads_internaldate() -> None:
    items = imap.parse_fetch(
        [
            (
                b'1 (UID 5 INTERNALDATE "17-Nov-2026 10:00:00 +0100" FLAGS (\\Seen) BODY[HEADER] {4}',
                b"A: b\n",
            ),
            b")",
            b'UID 6 INTERNALDATE " 7-Nov-2026 10:00:00 -0500" FLAGS ()',
            b"UID 7 FLAGS ()",
            b'UID 8 INTERNALDATE "garbage" FLAGS ()',
        ]
    )
    first, second, third, fourth = items
    assert first.internaldate == datetime(2026, 11, 17, 9, 0, tzinfo=UTC) and first.flags == {r"\Seen"}
    assert second.internaldate == datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
    assert third.internaldate is None and fourth.internaldate is None
    assert "INTERNALDATE" in services.HEADER_SPEC


def test_a_small_batch_is_live_whatever_the_dates_say(ak_member: Resident, server: FakeServer) -> None:
    """A wrong Date header, and a mail rescued from Spam (original INTERNALDATE kept): both are news."""
    mailbox = quiet_box(server)
    tag = tags.add(mailbox, "T")
    make_rule(mailbox, "R", tags_=[tag], from_contains="mette")
    server.put(1, raw_mail("Forkert dato", date=days_ago(400)))
    server.put(2, raw_mail("Reddet fra spam"))
    server.internaldates["INBOX", 2] = timezone.now() - timedelta(days=90)
    services.sync(mailbox)
    assert tagged_subjects(mailbox) == {"Forkert dato", "Reddet fra spam"}
    assert len(mail_notifications("mail.new_sag")) == 2


def test_a_big_batch_goes_by_internaldate_not_date(ak_member: Resident, server: FakeServer) -> None:
    mailbox = quiet_box(server)
    tag = tags.add(mailbox, "T")
    make_rule(mailbox, "R", tags_=[tag], from_contains="mette")
    old_arrival = timezone.now() - timedelta(days=60)
    for uid in range(1, 26):  # 25 > LIVE_BATCH
        server.put(uid, raw_mail(f"Filler {uid}", date=days_ago(0)))
        server.internaldates["INBOX", uid] = old_arrival  # the Date says today, the server got it long ago
    server.put(26, raw_mail("Ny, forkert dato", date=days_ago(400)))  # received just now
    server.internaldates["INBOX", 26] = timezone.now()
    services.sync(mailbox)
    assert MailThread.objects.count() == 26
    assert tagged_subjects(mailbox) == {"Ny, forkert dato"}


def test_without_internaldate_a_big_batch_falls_back_to_date(ak_member: Resident, server: FakeServer) -> None:
    mailbox = quiet_box(server)
    tag = tags.add(mailbox, "T")
    make_rule(mailbox, "R", tags_=[tag], from_contains="mette")
    server.no_internaldate = True
    for uid in range(1, 26):
        server.put(uid, raw_mail(f"Gammel {uid}", date=days_ago(400)))
    server.put(26, raw_mail("Frisk"))
    services.sync(mailbox)
    assert tagged_subjects(mailbox) == {"Frisk"}


def test_last_message_at_is_the_date_or_else_the_internaldate(
    ak_member: Resident, server: FakeServer
) -> None:
    mailbox = quiet_box(server)
    received = timezone.now() - timedelta(hours=3)
    server.put(1, raw_mail("Med dato", date=days_ago(5)))
    server.put(2, raw_mail("Uden dato").replace(b"Date: " + _RECENT_DATE.encode() + b"\n", b""))
    server.internaldates["INBOX", 1] = received
    server.internaldates["INBOX", 2] = received
    services.sync(mailbox)
    dated = MailThread.objects.get(subject="Med dato").last_message_at
    undated = MailThread.objects.get(subject="Uden dato").last_message_at
    assert dated is not None and undated is not None
    assert abs(dated - (timezone.now() - timedelta(days=5))) < timedelta(minutes=1)
    assert abs(undated - received) < timedelta(seconds=1)


# --- fetch_raw, aborted syncs, the Sent folder -----------------------------------------------------------


def test_opening_a_mail_still_shows_it_when_marking_it_read_is_refused(
    client: Client, ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = make_mailbox(roles=["ak"])
    (message,) = synced(server, mailbox, raw_mail("Skrivebeskyttet"))

    def refuse(self: FakeConnection, uid: int, *flags: str) -> None:
        raise imap.MailboxError("NO [READ-ONLY]")

    monkeypatch.setattr(FakeConnection, "add_flags", refuse)
    assert b"Skrivebeskyttet" in services.fetch_raw(mailbox, message, mark_seen=True)
    message.refresh_from_db()
    assert not message.seen
    response = client.get(reverse("postkasser:message", args=["ak", message.pk]))
    assert response.status_code == 200 and "Skrivebeskyttet" in response.content.decode()


def test_notices_of_committed_folders_are_sent_when_a_later_folder_fails_or_the_claim_is_lost(
    ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.add_folder("Zeta")
    server.add_folder("Yankee")
    mailbox = quiet_box(server)
    real = services._sync_folder

    def failing(kind: type[Exception]) -> Callable[..., None]:
        def sync_folder(run: services._Run, conn: object, folder: MailFolder, **kw: object) -> None:
            if folder.name == "Zeta":
                raise kind("later folder")
            real(run, conn, folder, **kw)  # type: ignore[arg-type]

        return sync_folder

    # An unexpected error in a later folder is that folder's error; the others carry on
    monkeypatch.setattr(services, "_sync_folder", failing(RuntimeError))
    arrive(server, mailbox, raw_mail("Fra indbakken"))
    assert [r.body for r in mail_notifications("mail.new_sag")] == ["Fra indbakken — fra Mette"]
    mailbox.refresh_from_db()
    assert "Zeta: later folder" in mailbox.last_sync_error
    server.put(1, raw_mail("Fra Yankee"), folder="Yankee")
    # The claim lost while a later folder runs: the sync aborts, but what committed is still announced
    Notification.objects.all().delete()
    monkeypatch.setattr(services, "_sync_folder", failing(services.SyncBusy))
    server.put(2, raw_mail("Anden fra indbakken"))
    with pytest.raises(services.SyncBusy):
        services.sync(mailbox)
    assert {r.body for r in mail_notifications("mail.new_sag")} == {
        "Anden fra indbakken — fra Mette",
        "Fra Yankee — fra Mette",
    }


def test_a_socket_error_in_one_folder_aborts_the_run_instead_of_reading_on(
    ak_member: Resident, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout mid-FETCH leaves unread lines on the connection; carrying on to the next folder would
    cache them under that folder's UIDs. So an OSError is not one folder's error — it ends the run."""
    server.add_folder("Zeta")
    mailbox = quiet_box(server)
    real = services._sync_folder
    seen: list[str] = []

    def sync_folder(run: services._Run, conn: object, folder: MailFolder, **kw: object) -> None:
        seen.append(folder.name)
        if folder.name == "INBOX":
            raise TimeoutError("read timed out")
        real(run, conn, folder, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(services, "_sync_folder", sync_folder)
    with pytest.raises(OSError):
        services.sync(mailbox)
    assert seen == ["INBOX"]


def test_a_sync_that_is_busy_sends_nothing_and_raises(ak_member: Resident, server: FakeServer) -> None:
    mailbox = quiet_box(server)
    hold_claim(mailbox)
    with pytest.raises(services.SyncBusy):
        services.sync(mailbox)
    assert mail_notifications("mail.new_sag") == []


def test_a_mailbox_with_synced_folders_but_no_sent_folder_points_to_admin(
    client: Client, ak_member: Resident, server: FakeServer
) -> None:
    server.folders.pop("Sent")
    server.add_folder("Arkiv")
    mailbox = make_mailbox(roles=["ak"], sent_folder="")
    services.sync(mailbox)
    url = reverse("postkasser:compose", args=["ak"])
    new = client.post(url, {"to": "a@example.com", "subject": "Nyt", "body": "Hej"}, follow=True)
    page = new.content.decode()
    assert "vælg Sendt-mappen i admin" in page and "hent post først" not in page
    assert "Sagen beholdes i 2 dage, medmindre kopien dukker op eller nogen svarer." in page
    assert server.appended == []


def test_the_cap_warning_counts_only_the_sager_that_were_eligible(
    ak_member: Resident, server: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    mailbox = quiet_box(server)
    make_rule(mailbox, "NB", subject_contains="NB", set_column=col(mailbox, ColumnRole.FAERDIG))
    with caplog.at_level("WARNING"):
        arrive(
            server,
            mailbox,
            *[raw_mail(f"NB {n}") for n in range(5)],
            *[raw_mail(f"Sag {n}") for n in range(20)],
        )
    assert len(mail_notifications("mail.new_sag")) == 20
    assert "only the first" not in caplog.text  # nothing eligible was skipped
    with caplog.at_level("WARNING"):
        arrive(server, mailbox, *[raw_mail(f"Mere {n}") for n in range(23)])
    assert "23 new sager in one sync, only the first 20" in caplog.text


def test_the_follow_up_sync_of_the_sent_folder_refreshes_only_recent_flags(
    ak_member: Resident, server: FakeServer
) -> None:
    mailbox = quiet_box(server)
    for uid in range(1, 201):
        server.put(uid, raw_mail(f"S{uid}", sender="ak@gahk.dk"), {r"\Seen"}, folder="Sent")
    services.sync(mailbox)
    for uid in range(201, 231):
        server.put(uid, raw_mail(f"S{uid}", sender="ak@gahk.dk"), {r"\Seen"}, folder="Sent")
    services.sync(mailbox)  # 230 cached now
    server.put(231, raw_mail("S231", sender="ak@gahk.dk"), {r"\Seen"}, folder="Sent")
    server.flag_ranges.clear()
    services.sync_sent_folder(mailbox)
    assert server.flag_ranges == [31]  # the newest 200 of the 230 cached
    server.flag_ranges.clear()
    services.sync(mailbox)
    assert 1 in server.flag_ranges  # the regular sync still refreshes all


def test_a_stored_sent_folder_that_cannot_take_sent_mail_is_labelled_and_replaced(
    client: Client, make_resident: MakeResident
) -> None:
    mailbox = make_mailbox(sent_folder="Drafts")
    folders_for(mailbox, "INBOX", "Sent")
    MailFolder.objects.create(
        mailbox=mailbox, name="Drafts", display_name="Drafts", path="Drafts", special_use="drafts"
    )
    assert services.sent_folder_is_stale(mailbox) and services.sent_folder_name(mailbox) == "Sent"
    label = dict(MailboxForm(instance=mailbox).fields["sent_folder"].choices)["Drafts"]  # type: ignore[attr-defined]
    assert label == "Drafts (ikke egnet som Sendt-mappe)"
    admin = make_resident("adm4@intern.dk", roles=(Role.ADMINISTRATOR,))
    Resident.objects.filter(pk=admin.pk).update(is_superuser=True)
    client.force_login(admin)
    page = client.get(reverse("admin:postkasser_mailbox_changelist")).content.decode()
    assert "«Drafts» er ikke egnet som Sendt-mappe" in page
    mailbox.sent_folder = "Findes ikke"
    assert dict(MailboxForm(instance=mailbox).fields["sent_folder"].choices)["Findes ikke"].endswith(  # type: ignore[attr-defined]
        "(findes ikke på serveren)"
    )
    Mailbox.objects.filter(pk=mailbox.pk).update(sent_folder="Sent")
    assert not services.sent_folder_is_stale(Mailbox.objects.get(pk=mailbox.pk))
