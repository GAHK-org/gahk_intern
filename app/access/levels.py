"""Whose access level is what: worked out from the alumneliste, stored on the account.

  * KITCHEN: the kitchen staff's shared login (Resident.is_kitchen).
  * RESIDENT: a superuser, or holding a role in the month in effect (residents.models.active_period).
  * An override the Indstilling set (Resident.access_level_override), until its last day.
  * Otherwise the months on the alumneliste (from_months): RESIDENT on the list in effect, MOVING_IN
    on a coming list only, MOVED_OUT_* graded by how long ago they were last on one, NO_ACCESS
    when never.

The result is stored in Resident.access_level, so a request reads it with the account and asks
nothing more. It is kept current by:

  * the signals (access.signals): a change to someone's months, roles, kitchen flag or override
    works their level out again at once, and so does a list that changes which month is in effect;
  * the night (access.tasks.refresh_access_levels, just after midnight): time alone moves levels,
    since a list made ahead takes effect on the 1st and the moved-out limits pass with the months;
  * every migrate (access.apps): a deploy picks up a change to the rule itself.

Pages that write many rows at once wrap the work in `refreshed_once()` and work the levels out once
at the end, not once per row.
"""

import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from django.db.models import Case, F, IntegerField, Max, When
from django.http import HttpRequest

from audit import trail
from core.clock import current_date
from residents.models import Residency, Resident, RoleAssignment, active_period
from residents.permissions import PREVIEW_SESSION_KEY, can_preview, is_kitchen

from .policy import AccessLevel, Feature, allows

logger = logging.getLogger(__name__)

PREVIEW_LEVEL_SESSION_KEY = "preview_access_level"
_LEVEL_VALUES = frozenset(level.value for level in AccessLevel)


# ---- the rule ---------------------------------------------------------------------------------------


def _month_index(year: int, month: int) -> int:
    return year * 12 + month - 1


def from_months(now: int, last_past: int | None, last: int | None) -> AccessLevel:
    """From month indexes: the month in effect, someone's last month on a list in effect by then,
    and their last month on any list (a coming one included)."""
    if last_past == now:
        return AccessLevel.RESIDENT
    if last is not None and last > now:
        return AccessLevel.MOVING_IN
    if last_past is None:
        return AccessLevel.NO_ACCESS
    return _moved_out(now - last_past)


def _moved_out(months_away: int) -> AccessLevel:
    """`months_away` is 1 in the first month off the list."""
    if months_away <= 1:
        return AccessLevel.MOVED_OUT_UNDER_1_MONTH
    if months_away <= 3:
        return AccessLevel.MOVED_OUT_1_TO_3_MONTHS
    if months_away <= 12:
        return AccessLevel.MOVED_OUT_3_TO_12_MONTHS
    return AccessLevel.MOVED_OUT_OVER_A_YEAR


def _work_out(resident_ids: Iterable[int] | None) -> dict[int, tuple[str, AccessLevel]]:
    """Each account's (stored level, level by the rule now): all of them, or those of `resident_ids`.
    Four queries however many."""
    year, month = active_period()
    now = _month_index(year, month)
    today = current_date()
    accounts = Resident.objects.all()
    residencies = Residency.objects.all()
    roles = RoleAssignment.objects.filter(year=year, month=month)
    if resident_ids is not None:
        ids = list(resident_ids)
        accounts = accounts.filter(pk__in=ids)
        residencies = residencies.filter(resident_id__in=ids)
        roles = roles.filter(resident_id__in=ids)
    index = F("year") * 12 + F("month") - 1
    # Per resident: the last month on a list in effect by now, and the last month on any list.
    months: dict[int, tuple[int | None, int | None]] = {
        row["resident_id"]: (row["last_past"], row["last"])
        for row in residencies.values("resident_id").annotate(
            last_past=Max(
                Case(
                    When(year__lt=year, then=index),
                    When(year=year, month__lte=month, then=index),
                    output_field=IntegerField(),
                )
            ),
            last=Max(index),
        )
    }
    with_roles = set(roles.values_list("resident_id", flat=True))
    out: dict[int, tuple[str, AccessLevel]] = {}
    for pk, stored, kitchen, superuser, override, until in accounts.values_list(
        "pk",
        "access_level",
        "is_kitchen",
        "is_superuser",
        "access_level_override",
        "access_level_override_until",
    ):
        if kitchen:
            level = AccessLevel.KITCHEN
        elif superuser or pk in with_roles:
            level = AccessLevel.RESIDENT
        elif override and (until is None or until >= today):
            level = AccessLevel(override)
        else:
            level = from_months(now, *months.get(pk, (None, None)))
        out[pk] = (stored, level)
    return out


def refresh_access_levels(resident_ids: Iterable[int] | None = None) -> dict[int, AccessLevel]:
    """Work out and store the level of every account, or of those of `resident_ids`. Returns the ones
    that changed, with their new level."""
    changed = {pk: level for pk, (stored, level) in _work_out(resident_ids).items() if stored != level}
    by_level: dict[AccessLevel, list[int]] = {}
    for pk, level in changed.items():
        by_level.setdefault(level, []).append(pk)
    for level, pks in by_level.items():
        Resident.objects.filter(pk__in=pks).update(access_level=level)
    if changed and resident_ids is None:
        logger.info(
            "Access levels changed for %d account(s): %s",
            len(changed),
            ", ".join(f"{len(pks)} to {level.value}" for level, pks in by_level.items()),
        )
    return changed


def forget_expired_overrides() -> int:
    """Clear the overrides whose last day has passed, so the edit page does not show them as set.
    Nobody's act, but a change to what the Indstilling set: one audit event per account."""
    expired = list(Resident.objects.filter(access_level_override_until__lt=current_date()))
    for resident in expired:
        with trail.event(
            "residents.resident.access_level_override_expired", subject=resident, actor=None
        ) as event:
            event.change(resident, "access_level_override", resident.access_level_override, "")
            event.change(resident, "access_level_override_until", resident.access_level_override_until, None)
            Resident.objects.filter(pk=resident.pk).update(
                access_level_override="", access_level_override_until=None
            )
    return len(expired)


# ---- working it out once for many rows ---------------------------------------------------------------


@dataclass
class _Batch:
    ids: set[int] = field(default_factory=set)
    everyone: bool = False


_batch: ContextVar[_Batch | None] = ContextVar("access_level_batch", default=None)


def refresh_soon(resident_ids: Iterable[int] | None) -> dict[int, AccessLevel]:
    """Work out these accounts' levels (None: every account's) now, or at the end of the
    `refreshed_once()` around this. What the signals call."""
    batch = _batch.get()
    if batch is None:
        return refresh_access_levels(resident_ids)
    if resident_ids is None:
        batch.everyone = True
    else:
        batch.ids.update(resident_ids)
    return {}


@contextmanager
def refreshed_once() -> Iterator[None]:
    """Collect the levels the signals would work out inside this, and work them out once when it
    ends without an error. Also a decorator. For a page that writes a whole month's list."""
    if _batch.get() is not None:  # an outer one will do it
        yield
        return
    batch = _Batch()
    token = _batch.set(batch)
    try:
        yield
    finally:
        _batch.reset(token)
    if batch.everyone:
        refresh_access_levels()
    elif batch.ids:
        refresh_access_levels(batch.ids)


# ---- reading it -----------------------------------------------------------------------------------


def real_level(user: Resident) -> AccessLevel:
    """The account's own stored level, never the preview."""
    return AccessLevel(user.access_level)


def levels_of(resident_ids: Iterable[int]) -> dict[int, AccessLevel]:
    """The stored level of each of these accounts, in one query."""
    return {
        pk: AccessLevel(level)
        for pk, level in Resident.objects.filter(pk__in=list(resident_ids)).values_list("pk", "access_level")
    }


def request_level(request: HttpRequest) -> AccessLevel:
    """The level this request is treated as. An administrator previewing a level ("Se side som") gets
    that level; previewing the kitchen gets the kitchen's."""
    user = request.user
    if not isinstance(user, Resident):
        return AccessLevel.NO_ACCESS
    previewed = request.session.get(PREVIEW_LEVEL_SESSION_KEY)
    if previewed in _LEVEL_VALUES and can_preview(user):
        return AccessLevel(previewed)
    if is_kitchen(request):
        return AccessLevel.KITCHEN
    if PREVIEW_SESSION_KEY in request.session and can_preview(user):
        return AccessLevel.RESIDENT  # previewing a role: a resident holding it
    return real_level(user)


def has_feature(request: HttpRequest, feature: Feature, method: str = "GET") -> bool:
    """Whether this request's level may use `feature` (a logged-in check is the caller's)."""
    return allows(request_level(request), feature, method)
