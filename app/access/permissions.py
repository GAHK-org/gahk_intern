"""The permissions: one Django permission per feature and action, `access.<feature>_<action>`.

`access.oelkaelder_manage`, `access.reparationer_move`. They are ordinary Django permissions (rows of
auth.Permission, on access.models.AccessRule), so everything asks them the standard way:
`request.user.has_perm("access.oelkaelder_manage")`, `@permission_required(...)`, and
`{% if perms.access.oelkaelder_manage %}` in a template.

What each role's group holds is worked out here from the table (access.policy), with the grades
spelled out: manage includes use and view, so a group that manages Ølkælder holds all three
permissions, and asking for one never has to know the order of grades.
"""

from collections.abc import Iterable
from functools import cache

from .features import EXTRAS, GRADE_ACTIONS, Feature, Grade
from .policy import POLICY

APP_LABEL = "access"


def codename(feature: Feature, action: str) -> str:
    return f"{feature.value}_{action}"


def name(feature: Feature, action: str) -> str:
    """The permission's full name, as has_perm takes it."""
    return f"{APP_LABEL}.{codename(feature, action)}"


def all_codenames() -> tuple[tuple[str, str], ...]:
    """Every permission, as (codename, description) for AccessRule's Meta."""
    out: list[tuple[str, str]] = []
    for feature in Feature:
        for action in [*GRADE_ACTIONS, *sorted(EXTRAS.get(feature, ()))]:
            out.append((codename(feature, action), f"{feature.value}: {action}"))
    return tuple(out)


def _actions_for(feature: Feature, value: Grade | str) -> set[str]:
    """The actions a role's entry gives for one feature, grades spelled out."""
    if isinstance(value, str):  # an extra, which opens its feature for use
        return {value, "view", "use"}
    actions = {action for action, grade in GRADE_ACTIONS.items() if grade <= value}
    if value >= Grade.MANAGE:
        actions |= EXTRAS.get(feature, frozenset())
    return actions


@cache
def _codenames(roles: frozenset[str]) -> frozenset[str]:
    out: set[str] = set()
    for role in roles:
        for feature, value in POLICY.get(role, {}).items():
            out |= {codename(feature, action) for action in _actions_for(feature, value)}
    return frozenset(out)


def codenames_of(roles: Iterable[str]) -> frozenset[str]:
    """The codenames a set of roles holds. Cached: there are few distinct sets."""
    return _codenames(frozenset(roles))


def names_of(roles: Iterable[str]) -> frozenset[str]:
    """The full permission names ("access.oelkaelder_manage") a set of roles holds."""
    return frozenset(f"{APP_LABEL}.{c}" for c in codenames_of(roles))


def forget() -> None:
    """Drop the cache, after the table changed (a test patching it)."""
    _codenames.cache_clear()
