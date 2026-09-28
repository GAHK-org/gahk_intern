"""Who may reach køkkenvagter, once there is anything to reach.

P1 (this phase) ships no views — see the module docstring in `koekken/models.py` and the design
doc's "Phasing" table. This module exists anyway, ahead of the views it will gate, because P2's
views must not be the first thing that has to remember to wire up the rollout gate, and because the
design doc's own "Access" section says it ships behind `core.rollout.Gate`.

    TO OPEN IT TO THE WHOLE HOUSE: set ACCESS_ROLES to None.

Gated to Køkkengruppen for now — they run allocation, adjudicate flags and own overrides (design
doc "Access"); Regnskab's read-only balance access and "administrator implies everything" are
policy for the views that don't exist yet, not for this gate.

Read through a lambda, never by value — see core.rollout's docstring for why this is load-bearing
rather than a style choice: it is what lets tests monkeypatch this module's ACCESS_ROLES and have a
Gate built at import time still see the new value.
"""

from django.http import HttpRequest

from core.rollout import Gate
from residents.models import Role
from residents.permissions import request_has_role

ACCESS_ROLES: tuple[str, ...] | None = (Role.KOKKENGRUPPE,)

_GATE = Gate(lambda: ACCESS_ROLES)

is_limited = _GATE.is_limited
roles_allowed = _GATE.roles_allowed
request_allowed = _GATE.request_allowed
access_required = _GATE.required
allowed_subscribers = _GATE.allowed_subscribers

# P2 design doc §9: per-surface roles, ON TOP OF the rollout gate above -- `access_required` (or
# `roles_allowed`, for the sidebar/nav) is the "is køkkenvagter open to you AT ALL" question every
# view still asks first; the predicates below are the SECOND, narrower question "of the people the
# rollout gate lets in, are you also the right role for THIS surface". Module constants, per the
# design doc's exact wording ("Role tuples as documented module constants in `views.py`"), and
# checked with BOTH a template gate (so a button/link is never rendered for someone who may not use
# it) AND a view re-check (so a replayed POST gets the same refusal) -- `reparationer`'s pattern
# (`reparationer.views.MANAGE_ROLES`/`MOVE_ROLES`), not a new one invented here.
#
# `administrator` needs no explicit listing beyond appearing in each tuple below for readability:
# `residents.permissions.real_roles` already expands a real ADMINISTRATOR to every role that exists
# (see that module's `_load_real_roles`), so "administrator implies everything" (design doc §9) holds
# structurally, not because these tuples enumerate every surface by hand.
MANAGE_ROLES: tuple[str, ...] = (Role.KOKKENGRUPPE, Role.ADMINISTRATOR)
BALANCE_EXPORT_ROLES: tuple[str, ...] = (Role.REGNSKAB, Role.ADMINISTRATOR)


def can_manage(request: HttpRequest) -> bool:
    """Køkkengruppen (or administrator): allocation run/preview, overrides, flag adjudication,
    the "ikke rapporteret" sweep -- P2 design doc §7/§9."""
    return request_has_role(request, *MANAGE_ROLES)


def can_view_balance_export(request: HttpRequest) -> bool:
    """Regnskab (or administrator): the read-only balance export for the move-out penalty -- P2
    design doc §7/§9. A deliberately narrower surface than `can_manage`: Køkkengruppen sees balances
    in context while running allocation/overrides, but the formal export is Regnskab's own job."""
    return request_has_role(request, *BALANCE_EXPORT_ROLES)
