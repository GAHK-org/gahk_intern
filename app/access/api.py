"""What the rest of the site asks of access: may this request (or account) do this?

    can(request, "oelkaelder.manage")        in a view, honouring "Se side som"
    user_can(resident, "den_hurtige.use")   for an account outside a request (a socket, a feed)
    @permission_required("ak.manage")        on a view
    holders("photo_album.manage")            the accounts that may, as a queryset
    {% if access.oelkaelder.manage %}        in a template

The gate (access.middleware) has already checked the page's feature by the time a view runs: view
for a request that changes nothing, use for one that may. A view only asks for more than that.
"""

from collections.abc import Callable
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.http.response import HttpResponseBase

from .features import Feature, Grade, feature_of, feature_of_url, in_feature, parse
from .permissions import Permissions, permissions_of
from .policy import ADMINISTRATOR, KITCHEN, RESIDENT, ROLE_LABELS
from .roles import (
    PREVIEW_SESSION_KEY,
    can_preview,
    forget,
    holders,
    previewed_roles,
    real_roles,
    request_permissions,
    request_roles,
)

__all__ = [
    "ADMINISTRATOR",
    "KITCHEN",
    "PREVIEW_SESSION_KEY",
    "RESIDENT",
    "ROLE_LABELS",
    "Feature",
    "Grade",
    "Permissions",
    "can",
    "can_preview",
    "feature_of",
    "feature_of_url",
    "forget",
    "fragment",
    "holders",
    "in_feature",
    "is_kitchen",
    "may_open",
    "permission_required",
    "permissions_of",
    "previewed_roles",
    "real_roles",
    "request_permissions",
    "request_roles",
    "user_can",
    "user_permissions",
]

View = Callable[..., HttpResponseBase]


def can(request: HttpRequest, permission: str) -> bool:
    """Whether this request may do `permission`. Honours "Se side som": an administrator looking at
    the site as a plain resident is refused what a plain resident would be."""
    return request.user.is_authenticated and request_permissions(request).allows(permission)


def user_permissions(user: object) -> Permissions:
    return permissions_of(real_roles(user))


def user_can(user: object, permission: str) -> bool:
    """Whether the account itself may do `permission`, whatever any preview shows."""
    return user_permissions(user).allows(permission)


def is_kitchen(request: HttpRequest) -> bool:
    """Whether this request is the kitchen staff's, or an administrator looking at the site as it."""
    return request.user.is_authenticated and KITCHEN in request_roles(request)


def may_open(user_or_request: object, url: str) -> bool:
    """Whether a GET of the page at `url` gets through the gate for this account (or request)."""
    permissions = (
        request_permissions(user_or_request)
        if isinstance(user_or_request, HttpRequest)
        else user_permissions(user_or_request)
    )
    return permissions.grade(feature_of_url(url)) >= Grade.VIEW


FRAGMENT_ATTRIBUTE = "access_fragment"


def fragment(view: View) -> View:
    """A part of a page that a script fetches (a poll, a live re-draw). When the gate refuses it, it
    answers 204, "nothing to change", rather than a login page or a 403 body that would be swapped
    into the page or read as an empty answer. Outermost, so the URLconf sees it."""
    setattr(view, FRAGMENT_ATTRIBUTE, True)
    return view


def permission_required(permission: str) -> Callable[[View], View]:
    """View decorator: log in first, then `permission`, or 403 (recorded in the audit trail)."""
    parse(permission)  # a name that does not exist fails at import, not on the first request

    def decorator(view: View) -> View:
        @wraps(view)
        def wrapped(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponseBase:
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not can(request, permission):
                audit_denied(request, permission)
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def audit_denied(request: HttpRequest, permission: str) -> None:
    """Someone sent away from what their roles do not allow. Not while previewing: an administrator
    looking at the site as someone else is refused on purpose."""
    if previewed_roles(request) is not None:
        return
    from audit import trail

    trail.record("auth.access.denied", details={"path": request.path, "permission": permission})
