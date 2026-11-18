"""Postkasser views. Every one starts from access.get_mailbox / visible_mailboxes, and every message,
sag and column is looked up THROUGH its mailbox (mailbox.messages.get, mailbox=mailbox), so an id from
another group's inbox is a 404, never a read.

Each IMAP round trip is synchronous: the board renders from the header cache, but opening a sag,
replying and downloading talk to one.com while the request waits — about a second. A one.com failure
becomes a message on the page, not a 500 (imap.MailboxError).
"""

from collections.abc import Iterable
from datetime import datetime, timedelta
from smtplib import SMTPException
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.mail import BadHeaderError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, F, OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import content_disposition_header, urlencode
from django.views.decorators.http import require_POST

from residents.models import Resident
from residents.permissions import current_resident

from . import access, columns, imap, rules, services, tags
from . import render as mailrender
from .forms import ComposeForm, RuleForm, TimeRuleForm
from .models import (
    DRAFT_MAX_ADDRESSES,
    DRAFT_MAX_BODY,
    DRAFT_MAX_SUBJECT,
    BoardColumn,
    ColumnRole,
    Mailbox,
    MailDraft,
    MailEvent,
    MailFolder,
    MailMessage,
    MailTag,
    MailThread,
    TagColor,
    shown,
)

PAGE_SIZE = 40
# A Færdig sag stays on the board this long after its last mail; after that it is in Arkiv.
BOARD_WINDOW = timedelta(days=30)
# Never shown, never on the board (services.SKIPPED_USES).
HIDDEN_USES = [use.value for use in services.SKIPPED_USES]


def _int(raw: str | None) -> int | None:
    """An id from a query string or form field, or None. `str.isdigit()` accepts "²" and Arabic-Indic
    digits, which `int()` then rejects or reads as something else — a 500 from a hand-typed URL — so
    only ASCII 0-9 count (and not so many that a database column would overflow)."""
    if raw and raw.isascii() and raw.isdigit() and len(raw) <= 18:
        return int(raw)
    return None


def _folders(mailbox: Mailbox) -> list[MailFolder]:
    """The folders the board can be filtered by, with their unread counts: inbox first, then by path,
    each carrying `indent` (non-breaking spaces by depth) for the <select>."""
    found: list[Any] = list(
        mailbox.folders.filter(selectable=True)
        .exclude(special_use__in=HIDDEN_USES)
        .annotate(unread=Count("messages", filter=Q(messages__seen=False)))
    )
    found.sort(key=lambda f: (f.special_use != MailFolder.SpecialUse.INBOX, f.path.lower()))
    for folder in found:
        folder.indent = "\u00a0\u00a0" * folder.depth
    return found


@login_required
def index(request: HttpRequest) -> HttpResponse:
    mailboxes = list(access.visible_mailboxes(request).order_by("name"))
    if len(mailboxes) == 1:  # the common case: one group, one inbox — skip the chooser
        return redirect("postkasser:board", slug=mailboxes[0].slug)
    for mailbox in mailboxes:
        mailbox.unread = mailbox.messages.filter(seen=False).count()  # type: ignore[attr-defined]
    return render(request, "postkasser/index.html", {"mailboxes": mailboxes})


def _search(threads: "QuerySet[MailThread]", mailbox: Mailbox, query: str) -> "QuerySet[MailThread]":
    """The sag's subject, or the name/address of anyone who wrote in it — what a card shows."""
    if not query:
        return threads
    by_sender = mailbox.messages.filter(Q(from_name__icontains=query) | Q(from_address__icontains=query))
    return threads.filter(Q(subject__icontains=query) | Q(pk__in=by_sender.values("thread_id")))


def _tag_filter(request: HttpRequest, mailbox_tags: list[MailTag]) -> int | None:
    """`?tag=<pk>`, if it is one of this mailbox's tags; anything else shows everything."""
    wanted = _int(request.GET.get("tag"))
    return wanted if wanted in {t.pk for t in mailbox_tags} else None


def _with_tag(threads: "QuerySet[MailThread]", tag_pk: int) -> "QuerySet[MailThread]":
    # A subquery, not a join: a join would repeat a sag once per matching tag.
    return threads.filter(
        pk__in=MailThread.tags.through.objects.filter(mailtag_id=tag_pk).values("mailthread_id")
    )


def _stamped(threads: "QuerySet[MailThread]") -> "QuerySet[MailThread]":
    """Each sag with `board_stamp`: when its last mail came, or — for a sag with no dated mail — when it
    last changed. The board and the archive both go by it, so a sag is in exactly one of them."""
    return threads.annotate(board_stamp=Coalesce("last_message_at", "updated_at"))


def _archived() -> Q:
    """A Færdig sag whose stamp is older than the board's window: on `_stamped` sager only."""
    return Q(column__role=ColumnRole.FAERDIG, board_stamp__lt=timezone.now() - BOARD_WINDOW)


def _latest(field: str) -> Subquery:
    return Subquery(
        MailMessage.objects.filter(thread=OuterRef("pk"))
        .order_by(F("date").desc(nulls_last=True), "-uid")
        .values(field)[:1]
    )


def _cards(threads: "QuerySet[MailThread]") -> list[MailThread]:
    """The sager as board cards: counts, unread, the latest sender and which folders (other than the
    inbox) hold their mail — one query each, not one per card."""
    cards: list[Any] = list(
        threads.select_related("assignee")
        .prefetch_related("tags")
        .annotate(
            message_count=Count("messages"),
            unread_count=Count("messages", filter=Q(messages__seen=False)),
            attachment_count=Count("messages", filter=Q(messages__has_attachments=True)),
            latest_name=_latest("from_name"),
            latest_address=_latest("from_address"),
        )
        .order_by(F("last_message_at").desc(nulls_last=True), "-pk")
    )
    chips: dict[int, list[str]] = {}
    rows = (
        MailMessage.objects.filter(thread__in=[c.pk for c in cards])
        .exclude(folder__special_use=MailFolder.SpecialUse.INBOX)
        .values_list("thread_id", "folder__display_name")
        .distinct()
    )
    for thread_id, name in rows:
        chips.setdefault(thread_id, []).append(name)
    # Who has a kladde on each sag: one more query for the whole page, not one per card.
    writers: dict[int, set[str]] = {}
    for thread_id, first, last in MailDraft.objects.filter(thread__in=[c.pk for c in cards]).values_list(
        "thread_id", "author__first_name", "author__last_name"
    ):
        writers.setdefault(thread_id, set()).add(f"{first} {last}".strip())
    for card in cards:
        card.latest_sender = card.latest_name or card.latest_address
        card.folder_chips = sorted(chips.get(card.pk, []))
        card.draft_authors = sorted(writers.get(card.pk, []))
    return cards


@login_required
def board(request: HttpRequest, slug: str) -> HttpResponse:
    mailbox = access.get_mailbox(request, slug)
    resident = current_resident(request)
    query = (request.GET.get("q") or "").strip()
    folders = _folders(mailbox)
    folder_ids = {f.pk for f in folders}
    folder_filter = _int(request.GET.get("mappe"))
    if folder_filter not in folder_ids:
        folder_filter = None  # an unknown folder shows everything rather than 404ing
    mailbox_tags = list(mailbox.tags.all())
    tag_filter = _tag_filter(request, mailbox_tags)
    owner = request.GET.get("ansvarlig") or ""
    if owner not in {"mig", "ingen"}:
        owner = ""

    threads = _search(mailbox.threads.filter(shown()), mailbox, query)
    if folder_filter is not None:
        threads = threads.filter(pk__in=mailbox.messages.filter(folder_id=folder_filter).values("thread_id"))
    if tag_filter is not None:
        threads = _with_tag(threads, tag_filter)
    if owner == "mig":
        threads = threads.filter(assignee=resident)
    elif owner == "ingen":
        threads = threads.filter(assignee__isnull=True)
    # Færdig is a window, not a heap: older ones are in Arkiv.
    threads = _stamped(threads).exclude(_archived())

    cards = _cards(threads)
    board_columns = [(c, [t for t in cards if t.column_id == c.pk]) for c in mailbox.columns.all()]
    return render(
        request,
        "postkasser/board.html",
        {
            "mailbox": mailbox,
            "columns": board_columns,
            "folders": folders,
            "query": query,
            "folder_filter": folder_filter,
            "owner": owner,
            "tags": mailbox_tags,
            "tag_filter": tag_filter,
            "filtered": bool(query or folder_filter or owner or tag_filter),
            "unread_count": mailbox.messages.filter(seen=False).count(),
            "draft_count": MailDraft.objects.filter(mailbox=mailbox, author=resident).count(),
            "mailboxes": access.visible_mailboxes(request).order_by("name"),
        },
    )


@login_required
def archive(request: HttpRequest, slug: str) -> HttpResponse:
    """Færdig sager the board no longer shows: a searchable list, newest first."""
    mailbox = access.get_mailbox(request, slug)
    query = (request.GET.get("q") or "").strip()
    mailbox_tags = list(mailbox.tags.all())
    tag_filter = _tag_filter(request, mailbox_tags)
    found = _stamped(mailbox.threads.filter(shown())).filter(_archived())
    if tag_filter is not None:
        found = _with_tag(found, tag_filter)
    old = _search(found, mailbox, query).order_by(F("last_message_at").desc(nulls_last=True), "-pk")
    # Page first, build cards for that page only: the archive grows without end.
    page = Paginator(old, PAGE_SIZE).get_page(request.GET.get("side"))
    page.object_list = _cards(mailbox.threads.filter(pk__in=[t.pk for t in page.object_list]))
    return render(
        request,
        "postkasser/archive.html",
        {"mailbox": mailbox, "page": page, "query": query, "tags": mailbox_tags, "tag_filter": tag_filter},
    )


@login_required
@require_POST
def refresh(request: HttpRequest, slug: str) -> HttpResponse:
    """Sync now, instead of waiting for the next scheduled run."""
    mailbox = access.get_mailbox(request, slug)
    try:
        new = services.sync(mailbox)
    except services.SyncBusy as exc:
        messages.warning(request, str(exc) or services.SYNC_BUSY)
    except imap.MailboxError as exc:
        messages.error(request, f"Kunne ikke hente post: {exc}")
    else:
        messages.success(request, f"{new} ny{'e' if new != 1 else ''} mail{'s' if new != 1 else ''}.")
        if mailbox.last_sync_error:  # some folder failed, the others went through
            messages.warning(request, f"Nogle mapper kunne ikke hentes: {mailbox.last_sync_error}")
    return redirect("postkasser:board", slug=slug)


def _get_message(mailbox: Mailbox, pk: int) -> MailMessage:
    return get_object_or_404(MailMessage.objects.select_related("folder", "thread"), pk=pk, mailbox=mailbox)


def _get_thread(mailbox: Mailbox, pk: int) -> MailThread:
    return get_object_or_404(MailThread.objects.select_related("column", "assignee"), pk=pk, mailbox=mailbox)


def _is_incoming(mailbox: Mailbox, msg: MailMessage) -> bool:
    return (
        msg.folder.special_use != MailFolder.SpecialUse.SENT
        and msg.from_address.lower() != mailbox.address.lower()
    )


def _frame(request: HttpRequest, raw: bytes) -> tuple[mailrender.RenderedMessage, str, bool]:
    rendered = mailrender.render(raw)
    show_images = request.GET.get("billeder") == "1"
    body_html = rendered.html if rendered.html is not None else mailrender.text_as_html(rendered.text)
    return rendered, mailrender.frame_document(body_html, allow_images=show_images), show_images


@login_required
def thread(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    """The sag page: status and assignee (plain POST forms, so it works without JavaScript), the history,
    the mails in order, and the newest one shown in full — the one live IMAP fetch of this page."""
    mailbox = access.get_mailbox(request, slug)
    sag = _get_thread(mailbox, pk)
    mails = list(sag.messages.select_related("folder").order_by(F("date").asc(nulls_first=True), "uid"))
    newest = mails[-1] if mails else None
    rendered = frame = None
    show_images = False
    fetch_error = ""
    if newest is not None:
        try:
            raw = services.fetch_raw(mailbox, newest, mark_seen=True)
        except imap.MailboxError as exc:
            fetch_error = f"Kunne ikke hente mailen: {exc}"
        else:
            rendered, frame, show_images = _frame(request, raw)
            services.log(
                mailbox,
                current_resident(request),
                MailEvent.Action.READ,
                message=newest,
                subject=newest.subject,
            )
    incoming = [m for m in mails if _is_incoming(mailbox, m)]
    resident = current_resident(request)
    drafts: list[Any] = list(sag.drafts.select_related("author"))
    for draft in drafts:
        draft.mine = draft.author_id == resident.pk
        draft.continue_url = _continue_url(mailbox, draft, fallback=incoming[-1] if incoming else None)
    return render(
        request,
        "postkasser/thread.html",
        {
            "drafts": drafts,
            "mailbox": mailbox,
            "sag": sag,
            "mails": mails,
            "newest": newest,
            "reply_to": incoming[-1] if incoming else None,
            "rendered": rendered,
            "frame": frame,
            "show_images": show_images,
            "fetch_error": fetch_error,
            "board_columns": mailbox.columns.all(),
            "members": access.members(mailbox),
            "tags": mailbox.tags.all(),
            "sag_tag_ids": set(sag.tags.values_list("pk", flat=True)),
            "move_targets": services.move_targets(mailbox, mails),
            "folder_names": sorted({m.folder.display_name for m in mails}),
            # Read events are noise next to answers; the history shows who acted on it.
            "history": sag.events.exclude(action=MailEvent.Action.READ).select_related("resident"),
        },
    )


@login_required
@require_POST
def set_tags(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    """The tick boxes on the sag page: the sag gets exactly the ticked tags. A pk that is not one of this
    mailbox's tags (another group's, or hand-typed rubbish) is ignored — it can never be applied."""
    mailbox = access.get_mailbox(request, slug)
    sag = _get_thread(mailbox, pk)
    chosen = [tag_id for raw in request.POST.getlist("tags") if (tag_id := _int(raw)) is not None]
    if tags.set_for_thread(sag, chosen, current_resident(request)):
        messages.success(request, "Tags gemt.")
    return redirect("postkasser:thread", slug=slug, pk=sag.pk)


@login_required
def tag_page(request: HttpRequest, slug: str) -> HttpResponse:
    """ "Tags": the group's own categories — add, rename, colour, reorder, delete. Open to anyone who can
    open the mailbox. Like the Kolonner page: one URL, one `action` field, every edit a POST that
    redirects back, a refused one says why in a message."""
    mailbox = access.get_mailbox(request, slug)
    if request.method == "POST":
        _edit_tags(request, mailbox)
        return redirect("postkasser:tags", slug=slug)
    return render(
        request,
        "postkasser/tags.html",
        {
            "mailbox": mailbox,
            "tags": mailbox.tags.annotate(thread_count=Count("threads", filter=shown("threads__"))),
            "colors": TagColor.choices,
        },
    )


def _edit_tags(request: HttpRequest, mailbox: Mailbox) -> None:
    action = request.POST.get("action", "")
    tag_pk = _int(request.POST.get("tag"))
    tag = mailbox.tags.filter(pk=tag_pk).first() if tag_pk is not None else None
    try:
        if action == "add":
            tags.add(mailbox, request.POST.get("name", ""), request.POST.get("color", TagColor.GRAA))
        elif tag is None:
            raise tags.TagError("Ukendt tag.")
        elif action == "rename":
            tags.rename(tag, request.POST.get("name", ""))
        elif action == "color":
            tags.recolor(tag, request.POST.get("color", ""))
        elif action in {"up", "down"}:
            tags.move(tag, -1 if action == "up" else 1)
        elif action == "delete":
            carried = tags.delete(tag)
            messages.success(
                request,
                f"«{tag.name}» er slettet"
                + (f" fra {carried} sag{'er' if carried != 1 else ''}." if carried else "."),
            )
        else:
            raise tags.TagError("Ukendt handling.")
    except tags.TagError as exc:
        messages.error(request, str(exc))


def _thread_response(
    request: HttpRequest, mailbox: Mailbox, sag: MailThread, error: str = "", *, status: int = 200
) -> HttpResponse:
    """One answer for both callers: JSON for the board's drag (frontend/src/reparationer.ts — it posts
    `status` and reads `ok`, `error` and `status` back), a redirect for a plain form POST. `status`
    is the column's pk as a string, which is what the script compares to what it sent."""
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(
            {
                "ok": not error,
                "error": error,
                "status": str(sag.column_id),
                "status_label": sag.column.name,
                "assignee": sag.assignee_id,
            },
            status=status,
        )
    if error:
        messages.error(request, error)
    if request.POST.get("next") == "board":
        return redirect("postkasser:board", slug=mailbox.slug)
    return redirect("postkasser:thread", slug=mailbox.slug, pk=sag.pk)


@login_required
@require_POST
def set_status(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    """Move a sag to another column. `status` is the column's pk; a column of another mailbox is a 404,
    like any other id that is not this mailbox's. A refusal is a 200 with `ok: false` and the reason, as
    in reparationer: the drag script puts the card back and shows it, which a 4xx would not let it."""
    mailbox = access.get_mailbox(request, slug)
    sag = _get_thread(mailbox, pk)
    column_pk = _int(request.POST.get("status"))
    if column_pk is None:
        return _thread_response(request, mailbox, sag, "Ukendt kolonne.")
    column = get_object_or_404(BoardColumn, pk=column_pk, mailbox=mailbox)
    services.move_thread(sag, column, current_resident(request))
    return _thread_response(request, mailbox, sag)


@login_required
@require_POST
def set_assignee(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    """Hand a sag to someone who can open the mailbox now, or ("") to nobody."""
    mailbox = access.get_mailbox(request, slug)
    sag = _get_thread(mailbox, pk)
    raw = request.POST.get("assignee", "")
    assignee: Resident | None = None
    if raw:
        assignee = access.members(mailbox).filter(pk=_int(raw) or 0).first()
        if assignee is None:
            return _thread_response(request, mailbox, sag, "Den valgte har ikke adgang til postkassen.")
    services.assign_thread(sag, assignee, current_resident(request))
    return _thread_response(request, mailbox, sag)


@login_required
def message(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    mailbox = access.get_mailbox(request, slug)
    msg = _get_message(mailbox, pk)
    try:
        raw = services.fetch_raw(mailbox, msg, mark_seen=True)
    except imap.MailboxError as exc:
        messages.error(request, f"Kunne ikke hente mailen: {exc}")
        return redirect("postkasser:board", slug=slug)
    rendered, frame, show_images = _frame(request, raw)
    services.log(mailbox, current_resident(request), MailEvent.Action.READ, message=msg, subject=msg.subject)
    return render(
        request,
        "postkasser/message.html",
        {
            "mailbox": mailbox,
            "msg": msg,
            "sag": _get_thread(mailbox, msg.thread_id) if msg.thread_id else None,
            "rendered": rendered,
            "frame": frame,
            "show_images": show_images,
            "move_targets": services.move_targets(mailbox, [msg]),
            # Read events are noise next to answers; the history shows who acted on it.
            "history": msg.events.exclude(action=MailEvent.Action.READ).select_related("resident"),
        },
    )


def _move(
    request: HttpRequest, mailbox: Mailbox, mails: list[MailMessage], *, sag: MailThread | None
) -> HttpResponse:
    """The part of the two move views that is the same: the target folder (of THIS mailbox, else 404),
    the move itself, and a message for however it went — a refused or failed move is shown, never a 500.
    Back to the sag, or to the board if the sag went with the mail (Papirkurv/Spam)."""
    folder_pk = _int(request.POST.get("mappe"))
    to_board = redirect("postkasser:board", slug=mailbox.slug)
    back = (
        redirect("postkasser:thread", slug=mailbox.slug, pk=sag.pk)
        if sag is not None
        else redirect("postkasser:message", slug=mailbox.slug, pk=mails[0].pk)
    )
    if folder_pk is None:
        messages.error(request, "Vælg en mappe at flytte til.")
        return back
    target = get_object_or_404(MailFolder, pk=folder_pk, mailbox=mailbox)
    try:
        outcome = services.move_messages(mailbox, mails, target, moved_by=current_resident(request))
        if outcome.notices is not None:  # the claim is released by now; web requests never push inline
            outcome.notices.send(background=True)
    except services.SyncBusy as exc:
        messages.error(request, str(exc) or services.SYNC_BUSY)
        return back
    except services.MoveRefused as exc:
        messages.error(request, str(exc))
        return back
    except imap.MailboxError as exc:
        messages.error(request, f"Mailen kunne ikke flyttes: {exc}")
        return back
    label = services.move_label(target)
    if outcome.moved:
        count = outcome.moved
        messages.success(request, f"{count} mail{'s' if count != 1 else ''} flyttet til «{label}».")
    for error in outcome.errors:
        (messages.warning if outcome.moved else messages.error)(request, error)
    if outcome.skipped and outcome.moved:
        messages.info(request, "Sendte mails (og mails der allerede lå dér) blev ikke flyttet.")
    if outcome.thread_removed:
        messages.info(request, "Sagen er fjernet fra tavlen; mails i Sendt ligger stadig i one.com.")
        return to_board
    if sag is None and not MailMessage.objects.filter(pk=mails[0].pk).exists():
        thread_id = mails[0].thread_id
        if thread_id and MailThread.objects.filter(pk=thread_id).exists():
            return redirect("postkasser:thread", slug=mailbox.slug, pk=thread_id)
        return to_board
    return back


@login_required
@require_POST
def move_thread_mail(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    """Move every mail of a sag to another folder at one.com (`mappe` = the folder's pk). The sag keeps
    its column on the board; only the mail moves."""
    mailbox = access.get_mailbox(request, slug)
    sag = _get_thread(mailbox, pk)
    return _move(request, mailbox, list(sag.messages.select_related("folder")), sag=sag)


@login_required
@require_POST
def move_message(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    """Move one mail to another folder at one.com."""
    mailbox = access.get_mailbox(request, slug)
    msg = _get_message(mailbox, pk)
    return _move(request, mailbox, [msg], sag=None)


def _unread(request: HttpRequest, mailbox: Mailbox, mails: list[MailMessage]) -> HttpResponse:
    """Mark `mails` unread and go to the BOARD — not back to the mail, whose GET would mark it read again."""
    try:
        services.mark_unread(mailbox, mails, by=current_resident(request))
    except imap.MailboxError as exc:
        messages.error(request, f"Kunne ikke markere som ulæst: {exc}")
    else:
        messages.success(request, "Markeret som ulæst.")
    return redirect("postkasser:board", slug=mailbox.slug)


@login_required
@require_POST
def message_unread(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    """Mark one mail unread."""
    mailbox = access.get_mailbox(request, slug)
    return _unread(request, mailbox, [_get_message(mailbox, pk)])


@login_required
@require_POST
def thread_unread(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    """Mark a sag unread: its newest INCOMING mail (not sent, not from the mailbox itself) — the one
    somebody should look at again — or, if it has none, its newest mail."""
    mailbox = access.get_mailbox(request, slug)
    sag = _get_thread(mailbox, pk)
    mails = list(sag.messages.select_related("folder").order_by(F("date").desc(nulls_last=True), "-uid"))
    target = next((m for m in mails if _is_incoming(mailbox, m)), mails[0] if mails else None)
    if target is None:
        messages.error(request, "Sagen har ingen mails at markere.")
        return redirect("postkasser:board", slug=slug)
    return _unread(request, mailbox, [target])


@login_required
def attachment(request: HttpRequest, slug: str, pk: int, index: int) -> HttpResponse:
    """Always a DOWNLOAD (Content-Disposition: attachment, nosniff from SecurityMiddleware): an HTML
    or SVG attachment rendered inline would run on this origin, with the reader's session."""
    mailbox = access.get_mailbox(request, slug)
    msg = _get_message(mailbox, pk)
    try:
        raw = services.fetch_raw(mailbox, msg)
    except imap.MailboxError as exc:
        messages.error(request, f"Kunne ikke hente vedhæftningen: {exc}")
        return redirect("postkasser:message", slug=slug, pk=pk)
    for found, part in mailrender.attachments_of(mailrender.parse(raw)):
        if found.index == index:
            return HttpResponse(
                mailrender.part_bytes(part),
                content_type="application/octet-stream",
                headers={"Content-Disposition": content_disposition_header(True, found.filename)},
            )
    return HttpResponse(status=404)


def _unique(addresses: Iterable[str]) -> list[str]:
    """Addresses without blanks or case-insensitive repeats, first spelling and order kept."""
    seen: set[str] = set()
    unique = []
    for address in addresses:
        if address and address.lower() not in seen:
            seen.add(address.lower())
            unique.append(address)
    return unique


def _send(
    request: HttpRequest, mailbox: Mailbox, form: ComposeForm, outgoing: services.Outgoing
) -> services.Sent | None:
    try:
        sent = services.send(mailbox, outgoing, sent_by=current_resident(request))
    except imap.MailboxError as exc:
        form.add_error(None, f"Mailen blev ikke sendt: {exc}")
        return None
    except BadHeaderError:
        form.add_error(None, "Mailen blev ikke sendt: emne eller adresser indeholder linjeskift.")
        return None
    except (SMTPException, OSError) as exc:
        form.add_error(None, f"Mailen blev ikke sendt: {exc}")
        return None
    for warning in sent.warnings:
        messages.warning(request, warning)
    return sent


# --- kladder (drafts) ---------------------------------------------------------------------------------
# Kept in the intranet only (models.MailDraft). The compose/reply form posts to the send view as ever; a
# draft is saved by a separate POST (the "Gem kladde" button, or the autosave in postkasser-draft.ts).


def _own_draft(mailbox: Mailbox, resident: Resident, raw_pk: str | None) -> MailDraft | None:
    """The requester's own draft in THIS mailbox, or None — for a pk from a query string or a hidden
    field, where somebody else's (or a stale) pk just means "no draft" and never reveals anything."""
    pk = _int(raw_pk)
    if pk is None:
        return None
    return MailDraft.objects.filter(pk=pk, mailbox=mailbox, author=resident).first()


def _reply_drafts(mailbox: Mailbox, resident: Resident, msg: MailMessage) -> "QuerySet[MailDraft]":
    """The requester's draft for answering `msg`: the one on its sag, or the one that names the mail itself
    — saved while the mail had no sag yet, and still the right one once it has one. Built before sending,
    since a reply can give the mail a sag."""
    own = MailDraft.objects.filter(mailbox=mailbox, author=resident)
    if msg.thread_id is not None:
        return own.filter(Q(thread_id=msg.thread_id) | Q(thread=None, reply_to=msg))
    return own.filter(thread=None, reply_to=msg)


def _continue_url(mailbox: Mailbox, draft: MailDraft, *, fallback: MailMessage | None = None) -> str:
    """Where a draft is continued: the reply page of the mail it answers (`fallback`, or the sag page,
    when that header is gone), or the compose page for a new mail."""
    slug = mailbox.slug
    reply_pk = draft.reply_to_id or (fallback.pk if fallback is not None else None)
    if reply_pk is not None:
        return reverse("postkasser:reply", args=[slug, reply_pk]) + ("?alle=1" if draft.reply_all else "")
    if draft.thread_id is not None:
        return reverse("postkasser:thread", args=[slug, draft.thread_id])
    return reverse("postkasser:compose", args=[slug]) + "?" + urlencode({"kladde": draft.pk})


def _draft_initial(draft: MailDraft) -> dict[str, str]:
    return {"to": draft.to, "cc": draft.cc, "subject": draft.subject, "body": draft.body}


@login_required
def compose(request: HttpRequest, slug: str) -> HttpResponse:
    mailbox = access.get_mailbox(request, slug)
    resident = current_resident(request)
    # A draft is resumed through `?kladde=<pk>` (its own, a new mail); while sending, the hidden `draft`
    # field says which one this form came from, so it is deleted once the mail is out.
    draft = _own_draft(mailbox, resident, request.POST.get("draft") or request.GET.get("kladde"))
    if draft is not None and draft.thread_id is not None:
        draft = None  # a reply's draft is resumed through its reply page
    initial = _draft_initial(draft) if draft else {"body": services.initial_body(mailbox, resident)}
    form = ComposeForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        outgoing = services.Outgoing(to=data["to"], cc=data["cc"], subject=data["subject"], body=data["body"])
        if sent := _send(request, mailbox, form, outgoing):
            sag = services.start_thread(mailbox, resident, data["subject"], sent.message_id)
            # Sending deletes the draft (a failed send never gets here) — AFTER the SEND event is logged,
            # which is what save_draft looks for to refuse a late autosave that would bring it back.
            if draft is not None:
                draft.delete()
            if sent.filed:  # the sag exists now, so the sent copy can join it right away
                services.sync_sent_folder(mailbox)
            messages.success(request, "Mailen er sendt.")
            return redirect("postkasser:thread", slug=slug, pk=sag.pk)
    return render(
        request,
        "postkasser/compose.html",
        {
            "mailbox": mailbox,
            "form": form,
            "reply_to": None,
            "draft": draft,
            "draft_started": timezone.now().isoformat(),
        },
    )


@login_required
def reply(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    mailbox = access.get_mailbox(request, slug)
    resident = current_resident(request)
    msg = _get_message(mailbox, pk)
    try:
        rendered = mailrender.render(services.fetch_raw(mailbox, msg))
    except imap.MailboxError as exc:
        messages.error(request, f"Kunne ikke hente mailen: {exc}")
        # Back to the sag the reply was started from (the single mail needs one.com too, and would fail).
        if msg.thread_id:
            return redirect("postkasser:thread", slug=slug, pk=msg.thread_id)
        return redirect("postkasser:message", slug=slug, pk=pk)

    drafts = _reply_drafts(mailbox, resident, msg)
    # The draft on the sag wins over one saved before the mail had a sag.
    draft = drafts.filter(thread_id=msg.thread_id).first() if msg.thread_id else None
    draft = draft or drafts.first()
    if draft is not None and draft.thread_id is None and msg.thread_id is not None:
        # Saved before the mail had a sag: it joins the sag now (nobody has a draft there, or the draft on
        # the sag would have won above).
        try:
            with transaction.atomic():
                MailDraft.objects.filter(pk=draft.pk).update(thread_id=msg.thread_id)
            draft.thread_id = msg.thread_id
        except IntegrityError:  # a draft on the sag appeared meanwhile: just resume that one
            draft = drafts.filter(thread_id=msg.thread_id).first()
    reply_all = request.GET.get("alle") == "1" or request.POST.get("alle") == "1"
    if draft is not None and request.method != "POST":
        reply_all = reply_all or draft.reply_all
    to_addresses = _unique(
        addr for _n, addr in mailrender.getaddresses([rendered.reply_to or rendered.from_header])
    )
    cc_addresses: list[str] = []
    if reply_all:
        taken = {a.lower() for a in to_addresses} | {mailbox.address.lower()}
        cc_addresses = [
            a
            for a in _unique(addr for _n, addr in mailrender.getaddresses([rendered.to, rendered.cc]))
            if a.lower() not in taken
        ]
    quoted = services.quote(rendered.text, msg.sender, rendered.date)
    # Resuming a draft replaces the fresh quote and greeting with what was written.
    initial = _draft_initial(draft) if draft is not None else {
        "to": ", ".join(to_addresses),
        "cc": ", ".join(cc_addresses),
        "subject": services.reply_subject(rendered.subject),
        "body": services.initial_body(mailbox, resident, quoted),
    }  # fmt: skip
    form = ComposeForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        outgoing = services.Outgoing(
            to=data["to"],
            cc=data["cc"],
            subject=data["subject"],
            body=data["body"],
            in_reply_to=rendered.message_id,
            references=rendered.references,
        )
        if sent := _send(request, mailbox, form, outgoing):
            try:
                services.mark_answered(mailbox, msg)
            except services.StaleFolder as exc:  # the reply is out; only the server-side flag is skipped
                messages.warning(
                    request, f"Svaret er sendt, men mailen blev ikke markeret som besvaret: {exc}"
                )
            sag = services.record_reply(mailbox, msg, resident, data["subject"], sent.message_id)
            # Sending deletes the draft (a failed send never gets here): the one on this sag, and the
            # one the form says it came from, which may be a different row. Only AFTER the REPLY event is
            # logged, which is what save_draft looks for to refuse a late autosave that would bring it back.
            drafts.delete()
            if posted := _own_draft(mailbox, resident, request.POST.get("draft")):
                posted.delete()
            if sent.filed:
                services.sync_sent_folder(mailbox)
            messages.success(request, "Svaret er sendt.")
            return redirect("postkasser:thread", slug=slug, pk=sag.pk)
    return render(
        request,
        "postkasser/compose.html",
        {
            "mailbox": mailbox,
            "form": form,
            "reply_to": msg,
            "reply_all": reply_all,
            "draft": draft,
            "draft_started": timezone.now().isoformat(),
            # The "you are continuing" notice is for the page that opens, not for a failed send.
            "resumed": draft if request.method != "POST" else None,
        },
    )


def _draft_problem(fields: dict[str, str]) -> str:
    """Why a draft cannot be saved, in Danish, or "". Over the limit is refused, never truncated."""
    if len(fields["body"]) > DRAFT_MAX_BODY:
        return f"Kladden blev ikke gemt: teksten er for lang (højst {DRAFT_MAX_BODY:,} tegn).".replace(
            ",", "."
        )
    if len(fields["subject"]) > DRAFT_MAX_SUBJECT:
        return f"Kladden blev ikke gemt: emnet er for langt (højst {DRAFT_MAX_SUBJECT} tegn)."
    if len(fields["to"]) > DRAFT_MAX_ADDRESSES or len(fields["cc"]) > DRAFT_MAX_ADDRESSES:
        return "Kladden blev ikke gemt: modtagerlisten er for lang."
    return ""


def _linked(model: type[MailThread] | type[MailMessage], mailbox: Mailbox, raw: str) -> int | None:
    """The pk of the row `raw` names if it is in this mailbox; blank is None, anything else a 404."""
    if not raw:
        return None
    row = model.objects.filter(pk=_int(raw) or 0, mailbox=mailbox).first()
    if row is None:
        raise Http404
    return row.pk


@login_required
@require_POST
def save_draft(request: HttpRequest, slug: str) -> HttpResponse:
    """Create or update the requester's kladde. Answers JSON for the autosave script (it asks for
    `Accept: application/json`) and a redirect back to the page otherwise, for the plain "Gem kladde"
    button. The text is stored as typed: addresses are NOT validated here, since half an address must
    save; they are checked when the mail is sent."""
    mailbox = access.get_mailbox(request, slug)
    resident = current_resident(request)
    post = request.POST
    wants_json = (
        "application/json" in request.headers.get("accept", "")
        or request.headers.get("x-requested-with") == "XMLHttpRequest"
    )
    # NUL cannot be stored in PostgreSQL text: a pasted one would be a 500, so it is dropped.
    fields = {name: post.get(name, "").replace("\x00", "") for name in ("to", "cc", "subject", "body")}

    # Every id is looked up THROUGH the mailbox (and, for the draft, the author): somebody else's draft,
    # or another group's sag or mail, is a 404 like anywhere else in this app.
    draft: MailDraft | None = None
    if post.get("draft"):
        draft = MailDraft.objects.filter(
            pk=_int(post["draft"]) or 0, mailbox=mailbox, author=resident
        ).first()
        if draft is None:
            raise Http404
    thread_id = _linked(MailThread, mailbox, post.get("thread", ""))
    reply_to_id = _linked(MailMessage, mailbox, post.get("reply_to", ""))
    if reply_to_id is not None:  # a reply belongs to the sag of the mail it answers (None: no sag yet)
        thread_id = MailMessage.objects.values_list("thread_id", flat=True).get(pk=reply_to_id)
    reply_all = post.get("alle") == "1"

    started = _parse_started(post.get("draft_started", ""))

    def refuse(error: str, status: int, keep: MailDraft | None = None) -> HttpResponse:
        if wants_json:
            return JsonResponse({"ok": False, "error": error}, status=status)
        # Back to the form WITH the text, which a redirect would lose. `keep` is the draft the form goes
        # on naming; a draft that is gone or was sent is not named again.
        messages.error(request, error)
        reply_to = _get_message(mailbox, reply_to_id) if reply_to_id else None
        return render(
            request,
            "postkasser/compose.html",
            {
                "mailbox": mailbox,
                "form": ComposeForm(initial=fields),
                "reply_to": reply_to,
                "reply_all": reply_all,
                "draft": keep,
                "draft_started": post.get("draft_started", ""),
            },
            status=status,
        )

    if error := _draft_problem(fields):
        return refuse(error, 400, draft)

    if draft is None:
        # No pk yet (the first autosave, or a second tab): the author's draft on this sag is THE draft —
        # or one that named the mail before it had a sag.
        if thread_id is not None:
            draft = MailDraft.objects.filter(mailbox=mailbox, author=resident, thread_id=thread_id).first()
            if draft is None and reply_to_id is not None:
                draft = MailDraft.objects.filter(
                    mailbox=mailbox, author=resident, thread=None, reply_to_id=reply_to_id
                ).first()
        elif reply_to_id is not None:
            draft = MailDraft.objects.filter(
                mailbox=mailbox, author=resident, thread=None, reply_to_id=reply_to_id
            ).first()
    saved = timezone.now()
    if draft is None:
        # A late autosave must not bring back a draft that Send has just deleted. The page says when its
        # draft session began; an answer from this author on the sag since then means the draft was sent.
        if started is not None:
            answered = MailEvent.objects.filter(
                mailbox=mailbox,
                resident=resident,
                action__in=[MailEvent.Action.REPLY, MailEvent.Action.SEND],
                created_at__gt=started,
            )
            if thread_id is not None:
                answered = answered.filter(thread_id=thread_id)
            elif reply_to_id is not None:
                answered = answered.filter(message_id=reply_to_id)  # a mail with no sag yet: its REPLY event
            else:
                # A NEW mail has no sag to match on, so any mail this author sent from the mailbox since
                # the page opened counts (at worst a second tab's unrelated send refuses one autosave).
                answered = answered.filter(action=MailEvent.Action.SEND)
            if answered.exists():
                return refuse("Mailen er allerede sendt, så kladden blev ikke gemt.", 409)
        try:
            with transaction.atomic():
                draft = MailDraft.objects.create(
                    mailbox=mailbox,
                    author=resident,
                    thread_id=thread_id,
                    reply_to_id=reply_to_id,
                    reply_all=reply_all,
                    **fields,
                )
            return _saved(request, mailbox, draft, draft.updated_at, wants_json)
        except IntegrityError:  # two autosaves raced for the same sag; the other one won
            if thread_id is None:
                raise
            draft = MailDraft.objects.filter(mailbox=mailbox, author=resident, thread_id=thread_id).first()
            if draft is None:
                raise Http404 from None
    # An UPDATE, never save(): a save() of a row that Send deleted meanwhile would INSERT it again.
    # Two tabs on one draft are last-write-wins; that is accepted.
    values: dict[str, Any] = {**fields, "reply_all": reply_all, "updated_at": saved}
    if reply_to_id is not None and draft.thread_id in (None, thread_id):
        values |= {"reply_to_id": reply_to_id, "thread_id": thread_id}  # a draft saved before the sag existed
    try:
        with transaction.atomic():
            rows = MailDraft.objects.filter(pk=draft.pk, mailbox=mailbox, author=resident).update(**values)
    except IntegrityError:  # it would join a sag where the author has another draft already
        return refuse("Du har allerede en kladde på sagen.", 409)
    if rows == 0:
        return refuse("Kladden findes ikke længere (den er sendt eller slettet).", 404)
    return _saved(request, mailbox, draft, saved, wants_json)


def _parse_started(raw: str) -> datetime | None:
    """The page's `draft_started`, or None for anything that is not a timestamp — also one that is
    well-formed but impossible (February 30th, an offset of +99:00), which parse_datetime raises on."""
    try:
        started = parse_datetime(raw)
    except ValueError:
        return None
    if started is not None and timezone.is_naive(started):
        started = timezone.make_aware(started)
    return started


def _saved(
    request: HttpRequest,
    mailbox: Mailbox,
    draft: MailDraft,
    saved_at: datetime,
    wants_json: bool,
) -> HttpResponse:
    if wants_json:
        return JsonResponse(
            {"ok": True, "draft": draft.pk, "saved_at": timezone.localtime(saved_at).strftime("%H:%M")}
        )
    messages.success(request, "Kladden er gemt.")
    # The reply page, or the compose page, which names the draft itself (`?kladde=`; a reply finds it by sag).
    return redirect(_continue_url(mailbox, draft))


@login_required
@require_POST
def delete_draft(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    """Delete the requester's own draft. Somebody else's is a 404: only the author may delete it, even if
    the author has since lost access to the mailbox (the purge removes it in the end)."""
    mailbox = access.get_mailbox(request, slug)
    draft = get_object_or_404(MailDraft, pk=pk, mailbox=mailbox, author=current_resident(request))
    reply_url = _continue_url(mailbox, draft)
    thread_id = draft.thread_id
    draft.delete()
    messages.success(request, "Kladden er slettet.")
    # `next` picks one of a few known places; it is never a URL taken from the request.
    where = request.POST.get("next")
    if where == "kladder":
        return redirect("postkasser:drafts", slug=slug)
    if where == "svar":  # "Start forfra": the reply page again, now with a fresh quote and greeting
        return redirect(reply_url)
    if where == "board" or thread_id is None:
        return redirect("postkasser:board", slug=slug)
    return redirect("postkasser:thread", slug=slug, pk=thread_id)


@login_required
def drafts(request: HttpRequest, slug: str) -> HttpResponse:
    """ "Mine kladder": the requester's own drafts in this mailbox, new mails and replies, newest first."""
    mailbox = access.get_mailbox(request, slug)
    mine: list[Any] = list(MailDraft.objects.filter(mailbox=mailbox, author=current_resident(request)))
    for draft in mine:
        draft.continue_url = _continue_url(mailbox, draft)
    return render(request, "postkasser/drafts.html", {"mailbox": mailbox, "drafts": mine})


@login_required
def board_columns(request: HttpRequest, slug: str) -> HttpResponse:
    """ "Kolonner": the group edits its own board — rename, reorder, add, delete, and say which column
    plays which part (columns.py). Open to anyone who can open the mailbox; there is no manager tier.
    One URL, one `action` field: every edit is a POST that redirects back (so a reload never
    repeats it), and a refused one says why in a message."""
    mailbox = access.get_mailbox(request, slug)
    if request.method == "POST":
        _edit_columns(request, mailbox)
        return redirect("postkasser:columns", slug=slug)
    cols = list(mailbox.columns.annotate(thread_count=Count("threads", filter=shown("threads__"))))
    return render(
        request,
        "postkasser/columns.html",
        {
            "mailbox": mailbox,
            "columns": cols,
            "roles": [
                (role, role.label, columns.for_role(mailbox, role))
                for role in (ColumnRole.NY, ColumnRole.AFVENTER, ColumnRole.FAERDIG)
            ],
            "ny": columns.ny_column(mailbox),
        },
    )


def _edit_columns(request: HttpRequest, mailbox: Mailbox) -> None:
    action = request.POST.get("action", "")
    column_pk = _int(request.POST.get("column"))
    column = mailbox.columns.filter(pk=column_pk).first() if column_pk is not None else None
    try:
        if action == "add":
            columns.add(mailbox, request.POST.get("name", ""))
        elif action == "roles":
            chosen: dict[str, int | None] = {}
            for role in (ColumnRole.NY, ColumnRole.AFVENTER, ColumnRole.FAERDIG):
                value = request.POST.get(f"role_{role.value}", "")
                chosen[role.value] = _int(value)
            columns.set_roles(mailbox, chosen)
            messages.success(request, "Funktionerne er gemt.")
        elif column is None:
            raise columns.ColumnError("Ukendt kolonne.")
        elif action == "rename":
            columns.rename(column, request.POST.get("name", ""))
        elif action in {"up", "down"}:
            columns.move(column, -1 if action == "up" else 1)
        elif action == "delete":
            moved = columns.delete(column)
            if moved:
                messages.success(
                    request,
                    f"{moved} sag{'er' if moved != 1 else ''} flyttet til «{columns.ny_column(mailbox).name}».",
                )
        else:
            raise columns.ColumnError("Ukendt handling.")
    except columns.ColumnError as exc:
        messages.error(request, str(exc))


@login_required
def automation(request: HttpRequest, slug: str) -> HttpResponse:
    """ "Automatisering": rules for incoming mail and rules for sager that have gone quiet. Open to anyone who
    can open the mailbox, like Kolonner and Tags. One URL, one `action` field: every edit is a POST that
    redirects back, a refused one says why in a message — except a form with errors, which is shown again
    with them. `?regel=<pk>` / `?tidsregel=<pk>` open an existing rule in the form."""
    mailbox = access.get_mailbox(request, slug)
    rule_form: RuleForm | None = None
    time_form: TimeRuleForm | None = None
    if request.method == "POST":
        rule_form, time_form = _edit_automation(request, mailbox)
        if rule_form is None and time_form is None:
            return redirect("postkasser:automation", slug=slug)
    if rule_form is None:
        editing = mailbox.rules.filter(pk=_int(request.GET.get("regel")) or 0).first()
        rule_form = RuleForm(instance=editing, mailbox=mailbox)
    if time_form is None:
        editing_time = mailbox.time_rules.filter(pk=_int(request.GET.get("tidsregel")) or 0).first()
        time_form = TimeRuleForm(instance=editing_time, mailbox=mailbox)
    rule_list = list(
        mailbox.rules.select_related("folder", "set_column", "set_assignee").prefetch_related("add_tags")
    )
    for rule in rule_list:
        rule.conditions, rule.actions = rules.describe(rule)  # type: ignore[attr-defined]
    return render(
        request,
        "postkasser/automation.html",
        {
            "mailbox": mailbox,
            "rules": rule_list,
            "time_rules": mailbox.time_rules.select_related("from_column", "to_column"),
            "rule_form": rule_form,
            "time_form": time_form,
        },
    )


def _edit_automation(request: HttpRequest, mailbox: Mailbox) -> tuple[RuleForm | None, TimeRuleForm | None]:
    """Handle one POST of the Automatisering page. Returns the bound form to show again if it had errors
    (otherwise (None, None), and the caller redirects)."""
    action = request.POST.get("action", "")
    rule_pk = _int(request.POST.get("rule"))
    rule = mailbox.rules.filter(pk=rule_pk).first() if rule_pk is not None else None
    time_pk = _int(request.POST.get("time_rule"))
    time_rule = mailbox.time_rules.filter(pk=time_pk).first() if time_pk is not None else None
    try:
        if action == "rule_save":
            if rule_pk is not None and rule is None:
                raise rules.RuleError("Ukendt regel.")
            form = RuleForm(request.POST, instance=rule, mailbox=mailbox)
            if not form.is_valid():
                return form, None
            with transaction.atomic():
                rules._lock(mailbox)
                saved = form.save(commit=False)
                if rule is None:
                    saved.position = rules.next_position(mailbox)
                saved.mailbox = mailbox
                if saved.is_active:  # saved again by a person: no longer "switched off by the system"
                    saved.disabled_reason = ""
                saved.save()
                form.save_m2m()
            messages.success(request, "Reglen er gemt.")
        elif action == "time_save":
            if time_pk is not None and time_rule is None:
                raise rules.RuleError("Ukendt tidsregel.")
            time_form = TimeRuleForm(request.POST, instance=time_rule, mailbox=mailbox)
            if not time_form.is_valid():
                return None, time_form
            saved_time = time_form.save(commit=False)
            saved_time.mailbox = mailbox
            if saved_time.is_active:  # picked its columns again: no longer "switched off by the system"
                saved_time.disabled_reason = ""
            saved_time.save()
            messages.success(request, "Tidsreglen er gemt.")
        elif action.startswith("time_"):
            if time_rule is None:
                raise rules.RuleError("Ukendt tidsregel.")
            if action == "time_delete":
                time_rule.delete()
            elif action == "time_toggle":
                if not time_rule.is_active:
                    rules.check_time_rule_can_be_on(time_rule)
                time_rule.is_active = not time_rule.is_active
                time_rule.save(update_fields=["is_active"])
            else:
                raise rules.RuleError("Ukendt handling.")
        elif rule is None:
            raise rules.RuleError("Ukendt regel.")
        elif action in {"rule_up", "rule_down"}:
            rules.move_rule(rule, -1 if action == "rule_up" else 1)
        elif action == "rule_toggle":
            if not rule.is_active and rule.disabled_reason:
                raise rules.RuleError("Reglen blev slået fra, fordi dens mappe er væk — redigér den først.")
            rule.is_active = not rule.is_active
            rule.save(update_fields=["is_active"])
        elif action == "rule_delete":
            rule.delete()
            messages.success(request, f"«{rule.name}» er slettet.")
        elif action == "rule_run":
            changed = rules.run_on_existing(rule, mailbox, by=current_resident(request))
            messages.success(
                request,
                f"«{rule.name}» ændrede {changed} {'åbne sager' if changed != 1 else 'åben sag'}.",
            )
        else:
            raise rules.RuleError("Ukendt handling.")
    except services.SyncBusy as exc:
        messages.error(request, str(exc) or services.SYNC_BUSY)
    except rules.RuleError as exc:
        messages.error(request, str(exc))
    return None, None
