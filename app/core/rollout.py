"""The staged-rollout gate, shared by the features that ship behind one.

    TO OPEN A FEATURE TO EVERY RESIDENT: set its own ACCESS_ROLES to None.

That one edit widens the feature's views, its sidebar entry, its "Under test" chip and its
notification audience together, because all four ask this module the same question.

Extracted at the third caller, on the schedule opslagstavle/access.py set for it. Its comment said
the gate was copy-pasted from den_hurtige deliberately, because both copies were meant to be deleted
and "a core/rollout.py both apps imported would outlive the reason it existed. If a third feature
ever wants one, that is the point to extract it." That reasoning was right at n=2 and expired at
n=3: the copies have outlived "temporary", they have already drifted apart in wording, and a third
copy would be the first one written by someone reading two prior copies rather than the original
argument. That is how copies stop agreeing.

What moved here is only the MECHANISM. What did not move is every feature's policy — ACCESS_ROLES
itself, and the per-object predicates (can_edit, can_moderate, visible_to) that differ per feature
and are the whole reason each app still has an access.py.

THE GATE TAKES A CALLABLE, NOT A VALUE, and that is load-bearing rather than fussy. Each app keeps
ACCESS_ROLES as a module global that tests reach in and rebind
(`monkeypatch.setattr(access, "ACCESS_ROLES", None)`), and Django imports view modules lazily on the
first request. A Gate constructed with the *value* would bind whatever the constant happened to be
at import time and never see a change again — which passes every test that patches before the first
import, and freezes in production. Reading through a callable on every call is what keeps the
existing rollout tests meaningful.
"""

from collections.abc import Callable, Collection
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db.models import Q, QuerySet
from django.http import HttpRequest
from django.http.response import HttpResponseBase

from residents.models import RoleAssignment, active_period
from residents.permissions import View, effective_roles


class Gate:
    """One feature's rollout gate, reading its ACCESS_ROLES through `roles`.

    `roles` returns None when the feature is open to every logged-in resident, or a tuple of role
    names when it is still limited to them.
    """

    def __init__(self, roles: Callable[[], tuple[str, ...] | None]) -> None:
        self._roles = roles

    def is_limited(self) -> bool:
        """Whether the gate is still on — what drives the "Under test" chip."""
        return self._roles() is not None

    def roles_allowed(self, roles: Collection[str]) -> bool:
        """Whether a role set may use the feature.

        Takes roles rather than a request so the sidebar, which only has the effective role set to
        hand, can ask exactly the same question as the views.
        """
        access_roles = self._roles()
        return access_roles is None or not set(roles).isdisjoint(access_roles)

    def request_allowed(self, request: HttpRequest) -> bool:
        """Same question for a request. Uses *effective* roles, so an administrator previewing as a
        plain beboer is locked out too — which is exactly the rollout being simulated."""
        return request.user.is_authenticated and self.roles_allowed(effective_roles(request))

    def required(self, view: View) -> View:
        """@login_required plus the gate. Every view gets it, not just the page: a partial that
        answers 200 to someone the page 403s hands the feature out through the back door."""

        @wraps(view)
        def wrapped(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponseBase:
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not self.roles_allowed(effective_roles(request)):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    def allowed_subscribers(self, qs: QuerySet) -> QuerySet:
        """Narrow a push audience to devices whose owner can actually open the feature.

        Without this, gating the page would still notify every resident who had opted in before the
        gate went on — and tapping that notification lands on a 403. A notification you are not
        allowed to read is worse than no feature at all, so the audience is narrowed rather than the
        page alone.

        Filtered in SQL rather than by calling real_roles() per subscription: this runs on every
        post, and the role set lives in one RoleAssignment row per resident per month. Superusers
        are matched separately because they hold every role without holding any assignment.
        """
        access_roles = self._roles()
        if access_roles is None:
            return qs
        year, month = active_period()
        allowed = RoleAssignment.objects.filter(role__in=access_roles, year=year, month=month).values(
            "resident_id"
        )
        return qs.filter(Q(user_id__in=allowed) | Q(user__is_superuser=True))
