"""A mailbox's board columns: looking one up by role, and the edits the "Kolonner" page makes.

The invariants (see models.BoardColumn) are enforced HERE, in one place the page, the admin inline
and the tests all go through — not in a template that merely hides the buttons:

  * exactly one column holds the role `ny`, exactly one `faerdig`;
  * at most one holds `afventer` (a group may not want a "waiting" column at all);
  * a column has at most one role, names are unique within a mailbox (case-insensitively).

Everything that moves a sag by itself (sync, compose, reply) asks `for_role`, never for a name.
"""

from collections.abc import Iterable

from django.db import transaction
from django.db.models import Q, RestrictedError

from .models import BoardColumn, ColumnRole, Mailbox, MailThread, MailTimeRule

MAX_NAME = 60
REQUIRED_ROLES = (ColumnRole.NY, ColumnRole.FAERDIG)


class ColumnError(Exception):
    """A refused edit; the message is Danish and meant to be shown to the person who tried it."""


def for_role(mailbox: Mailbox, role: str) -> BoardColumn | None:
    return mailbox.columns.filter(role=role).first()


def ny_column(mailbox: Mailbox) -> BoardColumn:
    """Where a new sag goes. Always exists by the invariants; if the data was broken some other way
    (a hand-edited database), it is repaired rather than crashing a sync."""
    column = for_role(mailbox, ColumnRole.NY)
    if column is None:
        column = mailbox.columns.filter(role="").first() or BoardColumn.objects.create(
            mailbox=mailbox, name=_free_name(mailbox, "Ny"), position=_next_position(mailbox)
        )
        column.role = ColumnRole.NY
        column.save(update_fields=["role"])
    return column


def faerdig_column(mailbox: Mailbox) -> BoardColumn:
    """The column for finished sager; repaired like `ny_column` if the data was broken. A role-less
    column is never turned into it (that would silently close what is still being worked on): a new one
    is made."""
    column = for_role(mailbox, ColumnRole.FAERDIG)
    if column is None:
        column = BoardColumn.objects.create(
            mailbox=mailbox,
            name=_free_name(mailbox, "Færdig"),
            position=_next_position(mailbox),
            role=ColumnRole.FAERDIG,
        )
    return column


def _lock(mailbox: Mailbox) -> list[BoardColumn]:
    """Lock the mailbox's columns for the rest of the transaction (a no-op on SQLite, which has no row
    locks and serialises writers anyway) and return them as they are NOW, so an edit validates what
    is really there rather than what the page showed a minute ago."""
    return list(mailbox.columns.select_for_update().order_by("pk"))


def validate_layout(columns: Iterable[tuple[str, str]]) -> None:
    """Check a whole proposed set of (name, role) pairs. The admin inline uses this on the formset's
    result; the page's single-step edits keep the invariant by construction and use the pieces."""
    names: set[str] = set()
    roles: dict[str, int] = {}
    for name, role in columns:
        cleaned = clean_name(name)
        if cleaned.lower() in names:
            raise ColumnError(f"Der er to kolonner der hedder «{cleaned}».")
        names.add(cleaned.lower())
        if role:
            roles[role] = roles.get(role, 0) + 1
    for role in REQUIRED_ROLES:
        if roles.get(role, 0) != 1:
            raise ColumnError(f"Præcis én kolonne skal have funktionen «{ColumnRole(role).label}».")
    if roles.get(ColumnRole.AFVENTER, 0) > 1:
        raise ColumnError("Højst én kolonne kan have funktionen «Afventer svar».")


def clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ColumnError("Kolonnen skal have et navn.")
    if len(cleaned) > MAX_NAME:
        raise ColumnError(f"Navnet må højst være {MAX_NAME} tegn.")
    return cleaned


def _taken(mailbox: Mailbox, name: str, *, except_pk: int | None = None) -> bool:
    # Compared in Python: the database's own case-insensitive match only folds ASCII on SQLite, and
    # these are Danish names ("Færdig" vs "FÆRDIG").
    wanted = name.casefold()
    return any(
        existing.casefold() == wanted
        for pk, existing in mailbox.columns.values_list("pk", "name")
        if pk != except_pk
    )


def _free_name(mailbox: Mailbox, base: str) -> str:
    name, n = base, 2
    while _taken(mailbox, name):
        name, n = f"{base} {n}", n + 1
    return name


def _next_position(mailbox: Mailbox) -> int:
    last = mailbox.columns.order_by("-position").values_list("position", flat=True).first()
    return 0 if last is None else last + 1


def _renumber(mailbox: Mailbox) -> list[BoardColumn]:
    """Positions as 0..n-1 in the current order, so a swap is always between neighbours."""
    ordered = list(mailbox.columns.all())
    for position, column in enumerate(ordered):
        if column.position != position:
            column.position = position
            column.save(update_fields=["position"])
    return ordered


def rename(column: BoardColumn, name: str) -> None:
    cleaned = clean_name(name)
    if _taken(column.mailbox, cleaned, except_pk=column.pk):
        raise ColumnError(f"Der er allerede en kolonne der hedder «{cleaned}».")
    column.name = cleaned
    column.save(update_fields=["name"])


def add(mailbox: Mailbox, name: str) -> BoardColumn:
    """New columns carry no role: the roles are spoken for, and a role is something to reassign on
    purpose (set_roles), not something a new column silently takes."""
    cleaned = clean_name(name)
    if _taken(mailbox, cleaned):
        raise ColumnError(f"Der er allerede en kolonne der hedder «{cleaned}».")
    return BoardColumn.objects.create(mailbox=mailbox, name=cleaned, position=_next_position(mailbox))


def move(column: BoardColumn, direction: int) -> None:
    """-1 = one step towards the left of the board, +1 = right. At an end it is a no-op."""
    with transaction.atomic():
        ordered = _renumber(column.mailbox)
        index = next(i for i, c in enumerate(ordered) if c.pk == column.pk)
        other = index + direction
        if not 0 <= other < len(ordered):
            return
        a, b = ordered[index], ordered[other]
        a.position, b.position = b.position, a.position
        a.save(update_fields=["position"])
        b.save(update_fields=["position"])


def deactivate_time_rules(column: BoardColumn) -> None:
    """Switch off the time rules that use `column`, with the reason, before it is deleted (their link to it
    is then cleared): a rule that lost a column does nothing, and the page says why."""
    MailTimeRule.objects.filter(Q(from_column=column) | Q(to_column=column)).update(
        is_active=False, disabled_reason=f"Slået fra: kolonnen «{column.name}» er slettet"
    )


def delete(column: BoardColumn) -> int:
    """Delete a column; sager still in it go to the `ny` column first. Returns how many moved.
    The `ny` and `faerdig` columns cannot go until their role has been given to another column.

    Everything is decided INSIDE the lock, on a fresh read: two people editing the board at once must
    not each pass the role check on a stale copy."""
    mailbox = column.mailbox
    with transaction.atomic():
        current = {c.pk: c for c in _lock(mailbox)}
        fresh = current.get(column.pk)
        if fresh is None:
            raise ColumnError("Kolonnen findes ikke længere.")
        if fresh.role in REQUIRED_ROLES:
            raise ColumnError(
                f"«{fresh.name}» har funktionen «{ColumnRole(fresh.role).label}». "
                "Giv funktionen til en anden kolonne, før den kan slettes."
            )
        try:
            with transaction.atomic():
                target = ny_column(mailbox)
                moved = MailThread.objects.filter(column=fresh).update(column=target)
                deactivate_time_rules(fresh)
                fresh.delete()
        except RestrictedError as exc:  # a sag arrived after the move above
            raise ColumnError("Kolonnen fik en sag imens — prøv at slette den igen.") from exc
        _renumber(mailbox)
    return moved


def set_roles(mailbox: Mailbox, chosen: dict[str, int | None]) -> None:
    """`chosen` maps each of ny / afventer / faerdig to the pk of the column that should hold it
    (afventer may be None: no such column). Replaces every assignment in one go, because swapping two
    roles is impossible one at a time under the one-column-per-role constraint."""
    with transaction.atomic():
        by_pk = {c.pk: c for c in _lock(mailbox)}
        picked: dict[str, BoardColumn] = {}
        for role in (ColumnRole.NY, ColumnRole.AFVENTER, ColumnRole.FAERDIG):
            pk = chosen.get(role)
            if pk is None:
                if role in REQUIRED_ROLES:
                    raise ColumnError(f"Vælg en kolonne til funktionen «{ColumnRole(role).label}».")
                continue
            if pk not in by_pk:
                raise ColumnError("Ukendt kolonne.")
            picked[role] = by_pk[pk]
        if len({c.pk for c in picked.values()}) != len(picked):
            raise ColumnError("En kolonne kan kun have én funktion.")
        mailbox.columns.exclude(role="").update(role="")
        for held, column in picked.items():
            BoardColumn.objects.filter(pk=column.pk).update(role=held)
