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

from core.rollout import Gate
from residents.models import Role

ACCESS_ROLES: tuple[str, ...] | None = (Role.KOKKENGRUPPE,)

_GATE = Gate(lambda: ACCESS_ROLES)

is_limited = _GATE.is_limited
roles_allowed = _GATE.roles_allowed
request_allowed = _GATE.request_allowed
access_required = _GATE.required
allowed_subscribers = _GATE.allowed_subscribers
