"""What the rest of the site uses of access. Permissions are asked the standard Django way:

    request.user.has_perm("access.oelkaelder_manage")   in a view, honouring "Se side som"
    account.has_perm("access.den_hurtige_use")          for an account outside a request
    @permission_required("access.ak_manage")             on a view (this module's: it also records a refusal)
    {% if perms.access.oelkaelder_manage %}             in a template
    holders("access.photo_album_manage")                the accounts that may, as a queryset

The gate (access.middleware) has already checked the page's feature by the time a view runs: view
for a request that changes nothing, use for one that may. A view only asks for more than that.
"""

from collections.abc import Callable, Iterable
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.http.response import HttpResponseBase

from .features import HOME_FEATURES, Feature, Grade, feature_of, feature_of_url, in_feature
from .permissions import all_codenames, name
from .policy import ADMINISTRATOR, GROUPS, KITCHEN, RESIDENT, ROLE_LABELS, STAGES
from .roles import (
    NO_STAGE,
    PREVIEW_SESSION_KEY,
    ROLE_CODES,
    can_preview,
    holders,
    holding,
    previewed_roles,
    real_roles,
    request_roles,
    roles_of_many,
    sync,
    sync_soon,
    syncing_once,
)
from .stages import STAGE_LABELS, Stage

__all__ = [
    "ADMINISTRATOR",
    "GROUPS",
    "KITCHEN",
    "NO_STAGE",
    "PREVIEW_SESSION_KEY",
    "RESIDENT",
    "ROLE_CODES",
    "ROLE_LABELS",
    "STAGES",
    "STAGE_LABELS",
    "Feature",
    "Grade",
    "Stage",
    "can_preview",
    "check",
    "feature_of",
    "feature_of_url",
    "fragment",
    "holders",
    "holding",
    "in_feature",
    "may_open",
    "may_open_many",
    "own_perm",
    "permission_required",
    "previewed_roles",
    "real_roles",
    "request_roles",
    "roles_of_many",
    "sync",
    "sync_soon",
    "syncing_once",
]

View = Callable[..., HttpResponseBase]


def own_perm(user: object, permission: str) -> bool:
    """Whether the account itself holds `permission`, whatever "Se side som" shows: Django's own
    answer from its groups (and the superuser flag), never the preview's."""
    from django.contrib.auth.backends import ModelBackend

    if not getattr(user, "is_active", False) or not getattr(user, "is_authenticated", False):
        return False
    return bool(getattr(user, "is_superuser", False)) or ModelBackend().has_perm(user, permission)  # type: ignore[arg-type]


def _opening(feature: Feature) -> list[str]:
    """The permissions any one of which opens a link to a page of `feature`. The front page does for
    anyone with a home to be sent on to (access.middleware)."""
    if feature == Feature.DASHBOARD:
        return [name(f, "view") for f in (Feature.DASHBOARD, *HOME_FEATURES)]
    return [name(feature, "view")]


def may_open(user: object, url: str) -> bool:
    """Whether a GET of the page at `url` gets through the gate for this account. A link that is no
    page of ours refuses nobody."""
    feature = feature_of_url(url)
    has_perm = getattr(user, "has_perm", lambda _p: False)
    return feature is None or any(has_perm(p) for p in _opening(feature))


def may_open_many(account_ids: Iterable[int], url: str) -> list[int]:
    """Those of `account_ids` whose own groups open the page at `url`, in their order. Nobody is told
    about a page they cannot open: what a notification's recipients are narrowed by."""
    ids = list(dict.fromkeys(account_ids))
    feature = feature_of_url(url)
    if feature is None or not ids:
        return ids
    allowed: set[int] = set()
    for permission in _opening(feature):
        allowed |= set(holders(permission).filter(pk__in=ids).values_list("pk", flat=True))
    return [pk for pk in ids if pk in allowed]


KNOWN = frozenset(f"access.{codename}" for codename, _ in all_codenames())


def check(permission: str) -> str:
    """Fail where a permission is named, not on the first request, when access has no such one."""
    if permission.startswith("access.") and permission not in KNOWN:
        raise ValueError(f"no such permission: {permission}")
    return permission


FRAGMENT_ATTRIBUTE = "access_fragment"


def fragment(view: View) -> View:
    """A part of a page that a script fetches (a poll, a live re-draw). When the gate refuses it, it
    answers 204, "nothing to change", rather than a login page or a 403 body that would be swapped
    into the page or read as an empty answer. Outermost, so the URLconf sees it."""
    setattr(view, FRAGMENT_ATTRIBUTE, True)
    return view


def permission_required(permission: str) -> Callable[[View], View]:
    """Django's permission check on a view, with a 403 for a logged-in account (never the login page
    again) recorded in the audit trail."""
    check(permission)

    def decorator(view: View) -> View:
        @wraps(view)
        def wrapped(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponseBase:
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not request.user.has_perm(permission):
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
