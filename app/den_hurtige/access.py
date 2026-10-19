"""Who may reach Den Hurtige — the single switch for the whole feature.

The staged rollout is over. The feature was trialled with the administrator group and Inspektionen
first, so that a bad notification experience (or a Brave user who cannot subscribe) was found by
three people rather than a hundred; ACCESS_ROLES is now None and every resident is in.

    TO RE-GATE IT: set ACCESS_ROLES to a tuple of roles.

That one edit narrows the views, the sidebar entry and the live-feed poll together. It has never
governed individual channels, in either direction: den_hurtige.channels carries its own per-channel
`roles` which stacks on top of this one, so opening the rollout did NOT open a channel restricted to
a role. This gate is about the feature; that one is about one feed within it.

The module stays rather than being deleted with the trial, as the original plan had it, because two
things here are still load-bearing: MODERATOR_ROLES, and `roles_allowed` — the function
den_hurtige.channels.allowed mirrors.

The gate MECHANISM moved to core.rollout when begivenheder became the third feature to want it; see
that module's docstring for why the copy-it-again argument expired at three. What is left below is
this feature's own policy plus the thin re-exports that keep every caller unchanged.
"""

from collections.abc import Collection

from django.db.models import QuerySet
from django.http import HttpRequest

from core.rollout import Gate
from residents.permissions import MODERATION_ROLES, View, effective_roles

# None = every logged-in resident. A tuple = only those roles (administrator implies every role, so
# administrators and superusers are always in).
#
# Open to the whole kollegium. Two things follow that are worth expecting rather than debugging:
# the "Under test" chip disappears on its own (the feed reads is_limited()), and the channel
# picker's live-post counts stop reading zero — five feeds only look alive with a whole house in
# them, which is the question the trial could not answer.
ACCESS_ROLES: tuple[str, ...] | None = None

# Read through a lambda, never passed by value: this global is what tests rebind and what the edit
# above flips, and a Gate holding the value would freeze at import. See core.rollout.
_GATE = Gate(lambda: ACCESS_ROLES)

# Who may delete somebody else's message. Aliased rather than redefined: the list is shared with
# opslagstavlen and lives in residents.permissions, which owns every other role grouping.
MODERATOR_ROLES = MODERATION_ROLES


def can_moderate(request: HttpRequest) -> bool:
    """Whether this request may delete other people's messages. Uses *effective* roles, so an
    administrator previewing as a beboer correctly loses the delete buttons."""
    return request.user.is_authenticated and not effective_roles(request).isdisjoint(MODERATOR_ROLES)


is_limited = _GATE.is_limited


def roles_allowed(roles: Collection[str]) -> bool:
    """Whether a role set may use Den Hurtige. Takes roles rather than a request so the sidebar,
    which only has the effective role set to hand, can ask the same question as the views.

    Kept as a module-level function rather than an alias because den_hurtige.channels.allowed
    mirrors this signature deliberately, and the two are read side by side.
    """
    return _GATE.roles_allowed(roles)


def request_allowed(request: HttpRequest) -> bool:
    """Same question for a request. Uses *effective* roles, so an administrator previewing as a
    plain beboer is locked out too — which is exactly the rollout being simulated."""
    return _GATE.request_allowed(request)


def access_required(view: View) -> View:
    """@login_required plus the rollout gate."""
    return _GATE.required(view)


def allowed_residents(qs: QuerySet) -> QuerySet:
    """Narrow a notification audience to residents who can actually open Den Hurtige."""
    return _GATE.allowed_residents(qs)
