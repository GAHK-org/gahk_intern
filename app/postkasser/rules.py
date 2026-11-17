"""Automations on a mailbox: rules for incoming mail (MailRule) and rules for sager that have gone quiet
(MailTimeRule). Like columns.py and tags.py, the logic lives here, and the sync, the "kør på eksisterende
sager" button, the hourly job and the tests all go through it.

Out of scope, on purpose: sending automatic replies on the group's behalf, and rules that move mail between
one.com's folders (one.com's own filters do that).

A rule never raises into its caller: a broken rule is logged and skipped (see `apply_to_new_mail`).
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import F, Q, QuerySet
from django.db.models.functions import Coalesce
from django.utils import timezone

from residents.models import Resident

from . import access, notify, services
from .models import (
    BoardColumn,
    ColumnRole,
    Mailbox,
    MailEvent,
    MailFolder,
    MailMessage,
    MailRule,
    MailThread,
    MailTimeRule,
    shown,
)

logger = logging.getLogger(__name__)

RENEW_EVERY = 25  # sager between two claim renewals in a long run


def has_conditions(rule: MailRule) -> bool:
    return bool(
        rule.from_contains.strip()
        or rule.subject_contains.strip()
        or rule.to_contains.strip()
        or rule.folder_id
    )


def has_actions(rule: MailRule) -> bool:
    return bool(rule.set_column_id or rule.set_assignee_id or rule.add_tags.all())


def _has(needle: str, *haystacks: str) -> bool:
    needle = needle.strip().casefold()
    return any(needle in h.casefold() for h in haystacks)


def matches(rule: MailRule, message: MailMessage) -> bool:
    """Every condition that is filled in holds (AND), casefolded; a rule with none matches nothing. The
    sender is matched on address OR name, the recipient on To OR Cc; the folder is the one the mail lies in
    (a rule whose folder is gone has none, and then only its other conditions count — if there are none,
    it matches nothing)."""
    if not has_conditions(rule):
        return False
    if rule.from_contains.strip() and not _has(rule.from_contains, message.from_address, message.from_name):
        return False
    if rule.subject_contains.strip() and not _has(rule.subject_contains, message.subject):
        return False
    if rule.to_contains.strip() and not _has(rule.to_contains, message.to, message.cc):
        return False
    return not (rule.folder_id and rule.folder_id != message.folder_id)


def load(mailbox: Mailbox) -> list[MailRule]:
    """The mailbox's active rules in order, with what applying them needs, in a fixed number of queries."""
    return list(
        mailbox.rules.filter(is_active=True)
        .select_related("set_column", "set_assignee")
        .prefetch_related("add_tags")
    )


@dataclass
class Outcome:
    """What running the rules over one mail did to its sag."""

    applied: list[str] = field(default_factory=list)  # one per rule that changed something
    assigned: Resident | None = None  # a rule made them the assignee


def _touch(thread: MailThread, *fields: str) -> None:
    thread.last_activity_at = timezone.now()
    thread.save(update_fields=[*fields, "last_activity_at", "updated_at"])


def _changes(rule: MailRule, thread: MailThread, member_ids: set[int]) -> tuple[list[str], Resident | None]:
    """Apply one rule's actions to `thread`; returns the descriptions of what changed and the new assignee.
    Everything is checked against the sag's own mailbox again, whatever the rule row says."""
    parts: list[str] = []
    assigned: Resident | None = None
    current = set(thread.tags.values_list("pk", flat=True))
    new_tags = [t for t in rule.add_tags.all() if t.mailbox_id == thread.mailbox_id and t.pk not in current]
    if new_tags:
        thread.tags.add(*new_tags)
        parts.append("tag " + ", ".join(t.name for t in new_tags))
        _touch(thread)
    column = rule.set_column
    if column is not None and column.mailbox_id == thread.mailbox_id and thread.column_id != column.pk:
        thread.column = column
        _touch(thread, "column")
        parts.append(f"→ {column.name}")
    assignee = rule.set_assignee
    if assignee is not None and thread.assignee_id != assignee.pk:
        if assignee.pk in member_ids:
            thread.assignee = assignee
            _touch(thread, "assignee")
            parts.append(f"ansvarlig {assignee.full_name}")
            assigned = assignee
        else:
            logger.info(
                "postkasser: rule «%s»: %s may no longer open the mailbox, not assigned",
                rule.name,
                assignee.pk,
            )
    return parts, assigned


def _log(rule_name: str, thread: MailThread, parts: list[str], *, kind: str = "Regel") -> None:
    services.log(
        thread.mailbox,
        None,
        MailEvent.Action.RULE,
        message=None,
        subject=thread.subject,
        thread=thread,
        detail=f"{kind} «{rule_name}»: {', '.join(parts)}",
    )


@dataclass
class RuleSet:
    """The rules of one sync, loaded once, and the members they may assign to (looked up once, on first use)."""

    mailbox: Mailbox
    rules: list[MailRule]
    _members: set[int] | None = None

    @property
    def member_ids(self) -> set[int]:
        if self._members is None:
            self._members = set(access.members(self.mailbox).values_list("pk", flat=True))
        return self._members


def apply_to_new_mail(ruleset: RuleSet, thread: MailThread, message: MailMessage) -> Outcome:
    """Run the rules, in order, over a new incoming mail of `thread`. A matching rule's actions are applied
    (tags added, column and assignee overwritten) and logged; `stop_processing` ends the run. A rule that
    fails is logged and skipped, in a savepoint, so it cannot spoil the sync or the other rules."""
    outcome = Outcome()
    for rule in ruleset.rules:
        if not matches(rule, message):
            continue
        try:
            with transaction.atomic():
                parts, assigned = _changes(rule, thread, ruleset.member_ids)
                if parts:
                    _log(rule.name, thread, parts)
        except Exception:
            logger.exception("postkasser: rule %s failed on message %s", rule.pk, message.pk)
            thread.refresh_from_db()
            continue
        if parts:
            outcome.applied.append(rule.name)
        if assigned is not None:
            outcome.assigned = assigned
        if rule.stop_processing:
            break
    return outcome


def _latest_incoming(mailbox: Mailbox, thread: MailThread) -> MailMessage | None:
    for message in thread.messages.select_related("folder").order_by(F("date").desc(nulls_last=True), "-uid"):
        if (
            message.folder.special_use != MailFolder.SpecialUse.SENT
            and message.from_address.lower() != mailbox.address.lower()
        ):
            return message
    return None


def run_on_existing(rule: MailRule, mailbox: Mailbox, *, by: Resident | None = None) -> int:
    """ "Kør på eksisterende sager": apply `rule` to the open sager (not in the Færdig column) whose latest
    incoming mail matches. Returns how many sager changed. Holds the mailbox's claim, so it does not race a
    sync — raises services.SyncBusy if one is running; RuleError for a rule that is switched off.
    Assignments notify the assignee (not `by`, who pressed the button) once the claim is released, in the
    background."""
    if not rule.is_active:
        raise RuleError("Reglen er slået fra — slå den til, før den køres på eksisterende sager.")
    ruleset = RuleSet(mailbox, [rule])
    changed = 0
    handed: list[tuple[MailThread, Resident]] = []
    with services.claim(mailbox) as claimed:
        if not claimed:
            raise services.SyncBusy(services.SYNC_BUSY)
        open_threads = (
            mailbox.threads.filter(shown())
            .exclude(column__role=ColumnRole.FAERDIG)
            .select_related("column", "mailbox")
        )
        for number, thread in enumerate(open_threads):
            if number % RENEW_EVERY == 0:
                claimed.renew()
            message = _latest_incoming(mailbox, thread)
            if message is None or not matches(rule, message):
                continue
            try:
                with transaction.atomic():
                    parts, assigned = _changes(rule, thread, ruleset.member_ids)
                    if parts:
                        _log(rule.name, thread, parts)
            except Exception:
                logger.exception("postkasser: rule %s failed on sag %s", rule.pk, thread.pk)
                continue
            if parts:
                changed += 1
            if assigned is not None:
                handed.append((thread, assigned))
    for thread, assignee in handed:
        try:
            notify.assigned(thread, assignee, by=by, background=True)
        except Exception:
            logger.exception("postkasser: notifying the assignee of sag %s failed", thread.pk)
    return changed


# --- editing (the Automatisering page) ----------------------------------------------------------------


class RuleError(Exception):
    """A refused edit; the message is Danish and meant to be shown to the person who tried it."""


def _lock(mailbox: Mailbox) -> None:
    """Serialise edits of one mailbox's rules (a no-op on SQLite, which serialises writers anyway)."""
    Mailbox.objects.select_for_update().filter(pk=mailbox.pk).first()


def next_position(mailbox: Mailbox) -> int:
    last = mailbox.rules.order_by("-position").values_list("position", flat=True).first()
    return 0 if last is None else last + 1


def move_rule(rule: MailRule, direction: int) -> None:
    """-1 = earlier, +1 = later; a no-op at an end. Positions are renumbered 0..n-1 first."""
    with transaction.atomic():
        _lock(rule.mailbox)
        ordered = list(rule.mailbox.rules.all())
        for position, other in enumerate(ordered):
            if other.position != position:
                other.position = position
                other.save(update_fields=["position"])
        index = next((i for i, other in enumerate(ordered) if other.pk == rule.pk), None)
        if index is None:
            raise RuleError("Reglen findes ikke længere.")
        neighbour = index + direction
        if not 0 <= neighbour < len(ordered):
            return
        a, b = ordered[index], ordered[neighbour]
        a.position, b.position = b.position, a.position
        a.save(update_fields=["position"])
        b.save(update_fields=["position"])


def describe(rule: MailRule) -> tuple[list[str], list[str]]:
    """(conditions, actions) of a rule as short Danish phrases, for the list. Uses the prefetched tags."""
    conditions = [
        f"{label} «{value}»"
        for label, value in (
            ("afsender indeholder", rule.from_contains),
            ("emne indeholder", rule.subject_contains),
            ("til/cc indeholder", rule.to_contains),
        )
        if value.strip()
    ]
    if rule.folder is not None:
        conditions.append(f"i mappen «{rule.folder.display_name}»")
    actions = []
    if tag_names := [t.name for t in rule.add_tags.all()]:
        actions.append("tag " + ", ".join(tag_names))
    if rule.set_column is not None:
        actions.append(f"flyt til «{rule.set_column.name}»")
    if rule.set_assignee is not None:
        actions.append(f"ansvarlig {rule.set_assignee.full_name}")
    return conditions, actions


# --- time rules ---------------------------------------------------------------------------------------


def activity(threads: "QuerySet[MailThread]") -> "QuerySet[MailThread]":
    """Each sag with `activity`: its last_activity_at, or for a row without one its last mail, or its last change."""
    return threads.annotate(activity=Coalesce("last_activity_at", "last_message_at", "updated_at"))


@dataclass
class TimeMove:
    rule: MailTimeRule
    thread: MailThread


def time_rule_conflict(
    mailbox: Mailbox, from_column: BoardColumn, to_column: BoardColumn, *, except_pk: int | None = None
) -> MailTimeRule | None:
    """An ACTIVE rule that moves the other way (to_column -> from_column): the two would pass a sag back and
    forth every time it falls quiet."""
    found = mailbox.time_rules.filter(is_active=True, from_column=to_column, to_column=from_column)
    if except_pk is not None:
        found = found.exclude(pk=except_pk)
    return found.first()


def check_time_rule_can_be_on(rule: MailTimeRule) -> None:
    """Raises RuleError if switching `rule` on would be wrong: it has lost a column, or an active rule
    moves the other way."""
    if rule.from_column is None or rule.to_column is None or rule.disabled_reason:
        raise RuleError(
            "Tidsreglen blev slået fra, fordi en kolonne er slettet — redigér den og vælg kolonner igen."
        )
    if time_rule_conflict(rule.mailbox, rule.from_column, rule.to_column, except_pk=rule.pk) is not None:
        raise RuleError(
            "En aktiv tidsregel flytter den modsatte vej; de to ville flytte sagerne frem og tilbage."
        )


def _quiet_threads(mailbox: Mailbox, rule: MailTimeRule, now: datetime) -> "QuerySet[MailThread]":
    cutoff = now - timedelta(days=rule.after_days)
    return activity(
        mailbox.threads.filter(shown(), column=rule.from_column).select_related("mailbox")
    ).filter(Q(activity__lte=cutoff))


def _usable(rule: MailTimeRule, mailbox: Mailbox) -> bool:
    """False for a rule gone wrong (a column deleted, an admin edit): do nothing rather than guess."""
    return not (
        rule.from_column is None
        or rule.to_column is None
        or rule.from_column.mailbox_id != mailbox.pk
        or rule.to_column.mailbox_id != mailbox.pk
        or rule.from_column_id == rule.to_column_id
    )


def apply_time_rules(
    mailboxes: Iterable[Mailbox] | None = None, *, dry_run: bool = False, now: datetime | None = None
) -> list[TimeMove]:
    """Move every sag that has sat in a rule's `from_column` for `after_days` days without activity to its
    `to_column`, and log it. Idempotent: a moved sag's activity is set to now, so it does not bounce on
    into the next rule or back. With `dry_run` nothing changes (and so nothing is claimed); the moves that
    would be made are returned. A mailbox another sync or move holds, or takes over mid-run, is skipped
    (the next hourly run gets it) — the others carry on."""
    now = now or timezone.now()
    moves: list[TimeMove] = []
    boxes = (
        Mailbox.objects.filter(is_active=True, time_rules__is_active=True).distinct()
        if mailboxes is None
        else mailboxes
    )
    for mailbox in boxes:
        rules = list(mailbox.time_rules.filter(is_active=True).select_related("from_column", "to_column"))
        if not rules:
            continue
        if dry_run:
            for rule in rules:
                if _usable(rule, mailbox):
                    moves.extend(TimeMove(rule, thread) for thread in _quiet_threads(mailbox, rule, now))
            continue
        try:
            with services.claim(mailbox) as claimed:
                if not claimed:
                    continue
                for rule in rules:
                    claimed.renew()
                    to_column = rule.to_column
                    if to_column is None or not _usable(rule, mailbox):
                        continue
                    for thread in _quiet_threads(mailbox, rule, now):
                        moves.append(TimeMove(rule, thread))
                        with transaction.atomic():
                            thread.column = to_column
                            thread.last_activity_at = now
                            thread.save(update_fields=["column", "last_activity_at", "updated_at"])
                            _log(
                                rule.name,
                                thread,
                                [f"→ {to_column.name} efter {rule.after_days} dage"],
                                kind="Tidsregel",
                            )
        except services.SyncBusy:  # the claim was taken over mid-run
            logger.info("postkasser: time rules for %s interrupted: claim lost", mailbox.address)
    return moves
