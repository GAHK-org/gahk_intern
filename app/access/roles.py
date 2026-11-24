"""Roles are Django groups, one per role code (access.policy), and an account's groups are kept in
step with the facts by a sync.

An account's roles:

  * its stage, from its resident's months on the alumneliste (access.stages): moving in, resident,
    moved out …;
  * the role of its embedsgruppe on the list in effect (residents.models.active_period,
    WORKGROUP_ROLE);
  * every role granted to it that holds today (access.models.Grant): administrator, the kitchen
    staff's KITCHEN, the exceptions;
  * a superuser: ADMINISTRATOR, and RESIDENT whatever its months say.

An account with none of these has no access at all, and the front page says so.

The sync writes these into the account's groups. It runs when the facts change (a grant given or
ended, an account saved, a resident's months changed), just after midnight (a list made ahead takes
effect on the 1st, the months pass, grants start and end) and after every migrate (which also writes
each group's permissions from the table). Everything else reads the groups, the standard way.

"Se side som": an administrator may look at the site with other roles. The middleware puts them on
the request's account (PREVIEW_ATTRIBUTE), and Account.has_perm answers from them instead.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.contrib.auth.models import Group, Permission
from django.db.models import QuerySet
from django.http import HttpRequest

from residents.models import PREVIEW_ATTRIBUTE, WORKGROUP_ROLE, Account, Residency, active_period

from . import stages
from .models import Grant
from .permissions import APP_LABEL, codenames_of
from .policy import ADMINISTRATOR, KITCHEN, POLICY, RESIDENT, ROLE_LABELS, STAGES
from .stages import Stage, month_index, stages_of

# The session key of "Se side som": the roles the site is shown as, a list of role codes.
PREVIEW_SESSION_KEY = "preview_roles"
# In a preview's list instead of a stage: the account has no stage at all.
NO_STAGE = "none"
ROLE_CODES = frozenset(POLICY)
# In a preview's list: "person:<pk>", the site as that account's own roles open it.
PREVIEW_PERSON = "person:"

# ---- the rule ---------------------------------------------------------------------------------------


def combine(
    stage: Stage | None, listed: Iterable[str], granted: Iterable[str], superuser: bool
) -> frozenset[str]:
    """An account's roles from its facts (the kitchen aside)."""
    roles = {*listed, *granted}
    if superuser:
        roles.add(ADMINISTRATOR)
        stage = Stage.RESIDENT
    elif stage is None and KITCHEN not in roles:  # the kitchen staff are no residents
        stage = stages.NEVER_LISTED
    if stage is not None:
        roles.add(stage.value)
    return frozenset(roles)


def roles_of_many(account_ids: Iterable[int]) -> dict[int, frozenset[str]]:
    """Each account's roles by the facts, in four queries however many. Inactive ones have none."""
    ids = list(dict.fromkeys(account_ids))
    year, month = active_period()
    stage = stages_of(ids, now=month_index(year, month))
    listed: dict[int, set[str]] = {}
    for pk, workgroup in Residency.objects.filter(
        resident__person__account__in=ids, year=year, month=month, workgroup__isnull=False
    ).values_list("resident__person__account", "workgroup__name"):
        if given := WORKGROUP_ROLE.get(workgroup):
            listed.setdefault(pk, set()).add(given)
    granted: dict[int, set[str]] = {}
    for pk, role in Grant.objects.active().filter(account_id__in=ids).values_list("account_id", "role"):
        granted.setdefault(pk, set()).add(role)
    out: dict[int, frozenset[str]] = {}
    for pk, active, superuser in Account.objects.filter(pk__in=ids).values_list(
        "pk", "is_active", "is_superuser"
    ):
        if not active:
            out[pk] = frozenset()
        else:
            out[pk] = combine(stage.get(pk), listed.get(pk, ()), granted.get(pk, ()), superuser)
    return out


# ---- the sync --------------------------------------------------------------------------------------


def sync_group_permissions() -> None:
    """Make a group for every role and give it exactly the permissions the table gives the role."""
    perms = {p.codename: p for p in Permission.objects.filter(content_type__app_label=APP_LABEL)}
    for role in ROLE_CODES:
        group, _ = Group.objects.get_or_create(name=role)
        wanted = {perms[c] for c in codenames_of({role}) if c in perms}
        if set(group.permissions.all()) != wanted:
            group.permissions.set(wanted)


def sync(account_ids: Iterable[int] | None = None) -> int:
    """Put each account (every one, by default) in exactly its roles' groups; groups that are no role
    are left alone. Returns how many accounts changed."""
    accounts = (
        Account.objects.all() if account_ids is None else Account.objects.filter(pk__in=list(account_ids))
    )
    ids = list(accounts.values_list("pk", flat=True))
    wanted = roles_of_many(ids)
    group_of = {g.name: g.pk for g in Group.objects.filter(name__in=ROLE_CODES)}
    through = Account.groups.through
    held: dict[int, set[str]] = {}
    for pk, role in through.objects.filter(account_id__in=ids, group__name__in=ROLE_CODES).values_list(
        "account_id", "group__name"
    ):
        held.setdefault(pk, set()).add(role)
    changed = 0
    for pk in ids:
        now, should = held.get(pk, set()), set(wanted.get(pk, frozenset()))
        if now == should:
            continue
        changed += 1
        if gone := now - should:
            through.objects.filter(account_id=pk, group_id__in=[group_of[r] for r in gone]).delete()
        if new := should - now:
            through.objects.bulk_create(
                [through(account_id=pk, group_id=group_of[r]) for r in new if r in group_of]
            )
    return changed


_batch: ContextVar[set[int] | None] = ContextVar("access_sync_batch", default=None)
_EVERYONE = -1


def sync_soon(account_ids: Iterable[int] | None) -> None:
    """Sync these accounts (None: every account) now, in the same transaction as the change, or at
    the end of the `syncing_once()` around this. What the signals call."""
    batch = _batch.get()
    ids = {_EVERYONE} if account_ids is None else set(account_ids)
    if batch is not None:
        batch |= ids
        return
    sync(None if _EVERYONE in ids else ids)


@contextmanager
def syncing_once() -> Iterator[None]:
    """Collect what the signals would sync inside this, and sync it once at the end: for a page that
    writes a whole month's list."""
    if _batch.get() is not None:
        yield
        return
    batch: set[int] = set()
    token = _batch.set(batch)
    try:
        yield
    finally:
        _batch.reset(token)
    if batch:
        sync(None if _EVERYONE in batch else batch)


# ---- reading it -----------------------------------------------------------------------------------


def real_roles(account: object) -> frozenset[str]:
    """The roles an account holds, as its groups say (never a preview)."""
    if not isinstance(account, Account) or not account.is_authenticated:
        return frozenset()
    names = getattr(account, "_access_role_memo", None)
    if names is None:
        names = frozenset(account.groups.filter(name__in=ROLE_CODES).values_list("name", flat=True))
        account._access_role_memo = names  # type: ignore[attr-defined]
    return names


def can_preview(account: object) -> bool:
    """Only a real administrator (or superuser) may look at the site with other roles."""
    if not isinstance(account, Account) or not account.is_authenticated:
        return False
    return account.is_superuser or ADMINISTRATOR in real_roles(account)


def previewed_roles(request: HttpRequest) -> frozenset[str] | None:
    """The roles "Se side som" shows the site as, or None when it is off. Checked against the real
    roles every time, so a forged or stale session on anyone else is ignored.

    The list names roles; with no stage in it the site is shown as a resident holding them, unless
    it says NO_STAGE. The kitchen is shown as the kitchen alone."""
    if PREVIEW_SESSION_KEY not in request.session or not can_preview(request.user):
        return None
    chosen = set(request.session.get(PREVIEW_SESSION_KEY) or [])
    person = previewed_person_id(request)
    if person is not None:
        return roles_of_many([person]).get(person, frozenset())
    if KITCHEN in chosen:
        return frozenset({KITCHEN})
    roles = {role for role in chosen if role in ROLE_LABELS}
    if NO_STAGE not in chosen and not roles & STAGES:
        roles.add(RESIDENT)
    return frozenset(roles)


def previewed_person_id(request: HttpRequest) -> int | None:
    """The account "Se side som" shows the site as, when it shows one person's roles."""
    for entry in request.session.get(PREVIEW_SESSION_KEY) or []:
        if (
            isinstance(entry, str)
            and entry.startswith(PREVIEW_PERSON)
            and entry[len(PREVIEW_PERSON) :].isdigit()
        ):
            return int(entry[len(PREVIEW_PERSON) :])
    return None


def request_roles(request: HttpRequest) -> frozenset[str]:
    """The roles this request is treated as: the preview's, or the account's own."""
    preview = getattr(request.user, PREVIEW_ATTRIBUTE, None)
    return preview if preview is not None else real_roles(request.user)


def holders(permission: str) -> QuerySet[Account]:
    """The active accounts holding `permission` ("access.photo_album_manage") through their groups or
    as superusers: Django's own with_perm. What notification audiences are narrowed by."""
    return Account.objects.filter(pk__in=Account.objects.with_perm(permission).values("pk"))


def holding(roles: Iterable[str]) -> QuerySet[Account]:
    """The active accounts in any of these roles' groups."""
    held = Account.groups.through.objects.filter(group__name__in=list(roles)).values("account_id")
    return Account.objects.filter(is_active=True, pk__in=held)
