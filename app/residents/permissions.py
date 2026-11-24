"""Authorization helpers built on the **monthly** role model (F-010).

A resident's privileges come from their RoleAssignments in the *active* period (the newest published
month). Superusers bypass. Replaces the legacy scattered/inverted per-controller session checks
(01-infrastructure.md A5).

Role preview ("view site as role"): a real superuser / administrator may set a session override so the
whole internal site renders as if they held a chosen role (or none). The distinction between *real*
roles (DB/superuser, never the session) and *effective* roles (what the current request is treated as)
is the security boundary — only `real_roles`/`can_preview` decide who may preview, and they never read
the session, so a forged/stale override on a non-admin is silently ignored.
"""

from collections.abc import Callable
from enum import StrEnum
from functools import wraps

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.http.response import HttpResponseBase

from .models import Resident, Role, active_period

# A Django view: called with a request (plus captured URL kwargs) and returns a response.
#
# HttpResponseBase rather than HttpResponse, because FileResponse and StreamingHttpResponse are NOT
# HttpResponse subclasses - they descend from HttpResponseBase directly. Without this, any view that
# hands back a file (arkiv's download, and anything like core.media.serve_media that ever wants
# gating) cannot be decorated with role_required or a rollout Gate, which is the one place the
# decorator is most worth having. Widening a return type is safe for every existing caller.
View = Callable[..., HttpResponseBase]
# What request.user is statically typed as: django-stubs can't know it is always a Resident here.
AnyUser = AbstractBaseUser | AnonymousUser

ALL_ROLES = frozenset(Role.values)
PREVIEW_SESSION_KEY = "preview_roles"

# Pseudo-role of the kitchen-staff account (Resident.is_kitchen). Not a Role choice, so it grants
# nothing anywhere; it only lets the preview tool show the site as the kitchen sees it.
KITCHEN = "koekken"

# Attribute the resolved role set is memoised under, on the request's user instance.
_REAL_ROLES_MEMO = "_gahk_real_roles"

# Roles allowed to edit the website gahk.dk (the Hjemmeside editor, cms.services). Single source of
# truth for its views and the navigation. administrator has full access (and alone may delete a
# page); indstilling, inspektion and pr (the PR group) maintain the content.
CMS_EDITOR_ROLES = (Role.ADMINISTRATOR, Role.INDSTILLING, Role.INSPEKTION, Role.PR)

# Roles that may remove other residents' content and pin it (Den Hurtige messages, opslagstavlen
# posts and comments). Inspektionen keep the kollegium's house rules, so they moderate resident
# content the same way they do everything else; administrator is listed for readability even though
# it already implies every role (see real_roles). Lives here rather than in either feature app
# because this module is the single source of truth for role groupings.
MODERATION_ROLES = (Role.ADMINISTRATOR, Role.INSPEKTION)


def current_resident(request: HttpRequest) -> Resident:
    """The logged-in Resident for a request behind @login_required / @role_required.

    Centralizes the "an authenticated user is a Resident" invariant that django-stubs can't express.
    Raises PermissionDenied if the request user is not an authenticated resident - which only happens
    if a view forgot its access decorator (a programming error), never in the normal flow.
    """
    user = request.user
    if not isinstance(user, Resident):
        raise PermissionDenied
    return user


def _load_real_roles(user: Resident) -> frozenset[str]:
    """Read the role set from the DB. Superuser and `administrator` => every role (all-access)."""
    if user.is_superuser:
        return ALL_ROLES
    if user.is_kitchen:
        return frozenset({KITCHEN})
    year, month = active_period()
    codes = set(user.role_assignments.filter(year=year, month=month).values_list("role", flat=True))
    if Role.ADMINISTRATOR in codes:
        return ALL_ROLES
    return frozenset(codes)


def real_roles(user: AnyUser) -> set[str]:
    """The user's ACTUAL role codes for the active period. Never affected by preview.

    Anonymous / non-resident => none. This is the only authorization reader of the DB / superuser
    flag.

    Memoised on the user instance, which lives exactly one request (AuthenticationMiddleware builds
    a fresh one each time), so the snapshot can never outlive the request that took it. Rendering an
    internal page asks for roles at least three times — the access decorator, `effective_roles` for
    the sidebar, and `can_preview` — and every miss cost an `active_period()` lookup plus a
    RoleAssignment query. On Den Hurtige's 20-second poll that was six queries, every twenty seconds,
    per open tab.

    Resolving once per request is also the semantics you want from an authorization check: a request
    cannot end up half-authorized because someone edited roles while it was being handled. Nothing
    changes the current user's roles mid-request today (the role editor redirects, and the monthly
    sync writes *next* month), but if that ever changes, call `forget_real_roles` after the write.
    """
    if not isinstance(user, Resident):
        return set()
    cached = getattr(user, _REAL_ROLES_MEMO, None)
    if cached is None:
        cached = _load_real_roles(user)
        setattr(user, _REAL_ROLES_MEMO, cached)
    return set(cached)  # a copy: a caller mutating the result must not poison the memo


def forget_real_roles(user: AnyUser) -> None:
    """Drop the memo so the next `real_roles` re-reads the DB. Needed only by code that changes the
    *current* user's roles and must observe the change within the same request."""
    if hasattr(user, _REAL_ROLES_MEMO):
        delattr(user, _REAL_ROLES_MEMO)


def can_preview(user: AnyUser) -> bool:
    """Only a real administrator (or superuser, who holds all roles) may use the preview tool."""
    return Role.ADMINISTRATOR in real_roles(user)


def effective_roles(request: HttpRequest) -> set[str]:
    """The role set this request is treated as: the preview override (only if the real user may
    preview), otherwise the real roles. Re-verifies `can_preview` against real roles every call."""
    if PREVIEW_SESSION_KEY in request.session and can_preview(request.user):
        raw = request.session.get(PREVIEW_SESSION_KEY) or []
        previewed = {c for c in raw if c in Role.values or c == KITCHEN}
        if Role.ADMINISTRATOR in previewed:
            return set(ALL_ROLES)
        return previewed  # may be empty => plain resident
    return real_roles(request.user)


def is_kitchen(request: HttpRequest) -> bool:
    """Whether this request is the kitchen account, or an admin previewing as it. Runs on every
    request (core.middleware), so it only resolves roles when a preview is set."""
    user = request.user
    if not isinstance(user, Resident):
        return False
    if user.is_kitchen:
        return True
    return PREVIEW_SESSION_KEY in request.session and KITCHEN in effective_roles(request)


class AccountKind(StrEnum):
    """What kind of account a request is treated as, which decides which pages exist for it at all.

    Roles add powers on top (moderate, edit gahk.dk); the kind decides the pages. A resident reaches
    every page; any other kind reaches only the views marked `open_to` it, and gets 403 elsewhere
    (core.middleware.AccountKindGate). So a page nobody thought about is closed to them, not open.
    """

    RESIDENT = "resident"
    KITCHEN = "kitchen"  # the kitchen staff's shared account (Resident.is_kitchen)


# The attribute `open_to` sets on a view, read by core.middleware.AccountKindGate.
OPEN_TO_ATTRIBUTE = "open_to_account_kinds"


def account_kind(request: HttpRequest) -> AccountKind:
    """The kind this request is treated as. Honors the preview, so "Se side som → Køkkenet" shows an
    administrator what the kitchen account gets."""
    return AccountKind.KITCHEN if is_kitchen(request) else AccountKind.RESIDENT


def open_to[V: Callable[..., object]](*kinds: AccountKind) -> Callable[[V], V]:
    """Mark a view as open to these kinds of account besides residents.

    Only marks, never wraps: the view's own checks (login, roles, the feature's rules) still run, so
    opening a view to a kind means "this page exists for them", not "they may do anything on it".
    Put it above every other decorator, so the mark sits on what the URLconf calls.
    """

    def decorator(view: V) -> V:
        setattr(view, OPEN_TO_ATTRIBUTE, frozenset(kinds))
        return view

    return decorator


def open_to_every_kind[V: Callable[..., object]](view: V) -> V:
    """For pages that are the same for every kind of account: logging in and out, the password
    pages, uploaded files (served with their own checks), the service worker and the public pages."""
    return open_to(*AccountKind)(view)


def view_open_to(view: Callable[..., object], kind: AccountKind) -> bool:
    """Whether `kind` may reach `view`. Residents reach every view."""
    return kind == AccountKind.RESIDENT or kind in getattr(view, OPEN_TO_ATTRIBUTE, ())


def has_active_role(user: AnyUser, *roles: str) -> bool:
    """Backward-compatible, REAL-roles-only check (does not honor preview). Prefer `request_has_role`
    in views so preview is respected."""
    return not real_roles(user).isdisjoint(roles)


def request_has_role(request: HttpRequest, *roles: str) -> bool:
    """Request-aware role check that honors the preview override."""
    return not effective_roles(request).isdisjoint(roles)


def role_required(*roles: str) -> Callable[[View], View]:
    """View decorator: require one of `roles` in the *effective* role set (honors preview)."""

    def decorator(view: View) -> View:
        @wraps(view)
        def wrapped(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponseBase:
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if effective_roles(request).isdisjoint(roles):
                _audit_denied(request, roles)
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def _audit_denied(request: HttpRequest, roles: tuple[str, ...]) -> None:
    """A resident sent away from a page their roles do not open. Not while previewing: an
    administrator looking at the site as a plain resident is refused on purpose."""
    if PREVIEW_SESSION_KEY in request.session and can_preview(request.user):
        return
    from audit import trail

    trail.record("auth.access.denied", details={"path": request.path, "roles": list(roles)})


def require_can_preview(view: View) -> View:
    """Gate the preview switcher on the REAL admin role, so previewing 'beboer' cannot lock an admin
    out of ending the preview."""

    @wraps(view)
    def wrapped(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponseBase:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not can_preview(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped
