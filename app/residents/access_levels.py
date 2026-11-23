"""Who may use which part of the intranet: a person's access level, and what each level gets.

Roles say what someone may DO (moderate, edit gahk.dk, the embedsgrupper's tools). The access level
says which parts of the site exist for them at all, and it follows where they live:

  * RESIDENT: on the alumneliste in effect (residents.models.active_period), or holding a role this
    month, or a superuser. Everything.
  * MOVING_IN: on a coming month's alumneliste but not the one in effect.
  * MOVED_OUT_*: was on the alumneliste but is not any more, graded by how long ago, in whole
    months: last on the list in the month before the one in effect is "under a month".
  * KITCHEN: the kitchen staff's shared login (Resident.is_kitchen).
  * NO_ACCESS: an account that has never been on any list and is none of the above.

THE TABLE (POLICY) is the whole rule: each level's access to each Feature, NONE, READ or FULL. READ
lets through only requests that change nothing (GET, HEAD, OPTIONS). A feature is a part of the site
that the URLconf marks with `in_feature`; a view that belongs to no feature is for residents only,
so a new page is closed to everyone else until somebody puts it in a feature on purpose.

The gate (core.middleware.AccessLevelGate) applies the table to every request; the sidebar, the
topbar, notifications, live updates and each feature's rollout gate (core.rollout) ask it the same
question, so a level never sees a link, a notification or a live signal for a page it cannot open.
"""

from collections.abc import Callable, Iterable
from enum import IntEnum, StrEnum
from typing import Any, cast
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Case, F, IntegerField, Max, Q, When
from django.http import HttpRequest
from django.urls import Resolver404, URLPattern, URLResolver, resolve

from .models import Residency, Resident, RoleAssignment, active_period
from .permissions import PREVIEW_SESSION_KEY, can_preview, is_kitchen, real_roles


class AccessLevel(StrEnum):
    RESIDENT = "resident"
    MOVING_IN = "moving_in"
    MOVED_OUT_UNDER_1_MONTH = "moved_out_under_1_month"
    MOVED_OUT_1_TO_3_MONTHS = "moved_out_1_to_3_months"
    MOVED_OUT_3_TO_12_MONTHS = "moved_out_3_to_12_months"
    MOVED_OUT_OVER_A_YEAR = "moved_out_over_a_year"
    KITCHEN = "kitchen"
    NO_ACCESS = "no_access"

    @property
    def label(self) -> str:
        return LEVEL_LABELS[self]


LEVEL_LABELS = {
    AccessLevel.RESIDENT: "Beboer",
    AccessLevel.MOVING_IN: "Flytter ind",
    AccessLevel.MOVED_OUT_UNDER_1_MONTH: "Fraflyttet for under 1 måned siden",
    AccessLevel.MOVED_OUT_1_TO_3_MONTHS: "Fraflyttet for 1–3 måneder siden",
    AccessLevel.MOVED_OUT_3_TO_12_MONTHS: "Fraflyttet for 3–12 måneder siden",
    AccessLevel.MOVED_OUT_OVER_A_YEAR: "Fraflyttet for over et år siden",
    AccessLevel.KITCHEN: "Køkkenet",
    AccessLevel.NO_ACCESS: "Ingen adgang",
}


class Feature(StrEnum):
    """A part of the site, as the URLconf marks it (in_feature)."""

    # The account pages: logging in and out, the password pages, ending an admin's preview.
    ACCOUNT = "account"
    # No login involved: the service worker, /install, calendar feeds (the token is the credential).
    PUBLIC = "public"
    # Uploaded files (/media/), which carry their own checks.
    FILES = "files"
    DASHBOARD = "dashboard"
    DIRECTORY = "directory"  # Alumneliste
    PROFILES = "profiles"
    MENTIONS = "mentions"  # the @-picker's lookup, used by every composer
    STAMTREE = "stamtree"
    STATS = "stats"
    NOTIFICATIONS = "notifications"
    DEN_HURTIGE = "den_hurtige"
    OPSLAGSTAVLE = "opslagstavle"
    EVENTS = "events"  # Begivenheder
    REPARATIONER = "reparationer"
    ARKIV = "arkiv"
    PHOTO_ALBUM = "photo_album"
    GAHKFLIX = "gahkflix"
    AK = "ak"
    OELKAELDER = "oelkaelder"
    VAERELSESTJEK = "vaerelsestjek"
    SOEGVAERELSE = "soegvaerelse"
    # The wiki, a site of its own whose accounts follow the intranet's. No view here: the sidebar
    # links to it by this, and the wiki's account sync is to ask the same.
    WIKI = "wiki"


class Access(IntEnum):
    NONE = 0
    READ = 1  # only requests that change nothing
    FULL = 2


# Every level reaches these, NO_ACCESS too: it must be able to log out.
_EVERY_LEVEL = frozenset({Feature.ACCOUNT, Feature.PUBLIC})
# What a resident uses. Residents reach every view; this set is what "all but …" is counted from.
_RESIDENT_FEATURES = frozenset(Feature) - _EVERY_LEVEL - {Feature.FILES}


def _full(features: Iterable[Feature]) -> dict[Feature, Access]:
    return dict.fromkeys(features, Access.FULL)


POLICY: dict[AccessLevel, dict[Feature, Access]] = {
    AccessLevel.MOVING_IN: _full(_RESIDENT_FEATURES - {Feature.PHOTO_ALBUM, Feature.GAHKFLIX}),
    AccessLevel.MOVED_OUT_UNDER_1_MONTH: _full(_RESIDENT_FEATURES - {Feature.SOEGVAERELSE}),
    AccessLevel.MOVED_OUT_1_TO_3_MONTHS: {
        **_full(
            _RESIDENT_FEATURES
            - {
                Feature.SOEGVAERELSE,
                Feature.VAERELSESTJEK,
                Feature.REPARATIONER,
                Feature.AK,
                Feature.OELKAELDER,
            }
        ),
        Feature.AK: Access.READ,
        Feature.OELKAELDER: Access.READ,
    },
    AccessLevel.MOVED_OUT_3_TO_12_MONTHS: _full(
        {
            Feature.PHOTO_ALBUM,
            Feature.OPSLAGSTAVLE,
            Feature.DEN_HURTIGE,
            Feature.EVENTS,
            # What those four need: the bell for their notifications, the composers' @-picker.
            Feature.NOTIFICATIONS,
            Feature.MENTIONS,
        }
    ),
    AccessLevel.MOVED_OUT_OVER_A_YEAR: _full({Feature.STAMTREE}),
    # Den Hurtige's Køkkenet channel and its own opslag only: the features narrow it further inside.
    AccessLevel.KITCHEN: _full(
        {Feature.DEN_HURTIGE, Feature.OPSLAGSTAVLE, Feature.REPARATIONER, Feature.NOTIFICATIONS}
    ),
    AccessLevel.NO_ACCESS: {},
}
for _each_level, _its_features in POLICY.items():
    _its_features.update(_full(_EVERY_LEVEL))
    if _each_level != AccessLevel.NO_ACCESS:
        _its_features[Feature.FILES] = Access.FULL

# Where a level lands when it opens the dashboard without having it: its first feature in this order.
_HOMES = (
    (Feature.DEN_HURTIGE, "den_hurtige:feed"),
    (Feature.OPSLAGSTAVLE, "opslagstavle:board"),
    (Feature.EVENTS, "events:index"),
    (Feature.PHOTO_ALBUM, "photo_album:index"),
    (Feature.STAMTREE, "stamtree"),
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def access(level: AccessLevel, feature: Feature | None) -> Access:
    """`level`'s access to `feature`. A resident has full access everywhere; anyone else has none to
    a view that is in no feature."""
    if level == AccessLevel.RESIDENT:
        return Access.FULL
    if feature is None:
        return Access.NONE
    return POLICY[level].get(feature, Access.NONE)


def allows(level: AccessLevel, feature: Feature | None, method: str = "GET") -> bool:
    granted = access(level, feature)
    return granted == Access.FULL or (granted == Access.READ and method in SAFE_METHODS)


def home_url_name(level: AccessLevel) -> str | None:
    """The page a level without the dashboard lands on, or None when it has no page at all."""
    return next((name for feature, name in _HOMES if allows(level, feature)), None)


# ---- marking the URLconf --------------------------------------------------------------------------

_FEATURE_ATTRIBUTE = "access_feature"


def _mark(view: Callable[..., Any], feature: Feature) -> None:
    current = getattr(view, _FEATURE_ATTRIBUTE, None)
    if current not in (None, feature):
        raise ImproperlyConfigured(f"{view.__qualname__} is in both {current} and {feature}")
    setattr(view, _FEATURE_ATTRIBUTE, feature)


def _mark_patterns(patterns: Iterable[URLPattern | URLResolver], feature: Feature) -> None:
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            _mark_patterns(pattern.url_patterns, feature)
        else:
            _mark(pattern.callback, feature)


def in_feature[T](feature: Feature, target: T) -> T:
    """Put a view, or every view of an `include(...)`, in `feature`. Used in the URLconf:

    path("den-hurtige/", in_feature(Feature.DEN_HURTIGE, include("den_hurtige.urls"))),
    path("stamtree/", in_feature(Feature.STAMTREE, views.stamtree), name="stamtree"),
    """
    if isinstance(target, tuple):  # what include() returns: (urlconf module, app_name, namespace)
        urlconf = target[0]
        patterns = cast("Iterable[URLPattern | URLResolver]", getattr(urlconf, "urlpatterns", urlconf))
        _mark_patterns(patterns, feature)
    else:
        _mark(target, feature)  # type: ignore[arg-type]
    return target


def feature_of(view: Callable[..., Any]) -> Feature | None:
    return getattr(view, _FEATURE_ATTRIBUTE, None)


def feature_of_url(url: str) -> Feature | None:
    """The feature of the page at a site path (a query or fragment is ignored); None for no page."""
    try:
        return feature_of(resolve(urlsplit(url).path).func)
    except Resolver404:
        return None


# ---- whose level is what --------------------------------------------------------------------------

PREVIEW_LEVEL_SESSION_KEY = "preview_access_level"
_LEVEL_VALUES = frozenset(level.value for level in AccessLevel)
_LEVEL_MEMO = "_gahk_access_level"


def _month_index(year: int, month: int) -> int:
    return year * 12 + month - 1


def levels_of(residents: Iterable[Resident], with_roles: set[int] | None = None) -> dict[int, AccessLevel]:
    """Each resident's real access level, in three queries however many there are.

    `with_roles`: those of them known to hold a role in the month in effect, when the caller has
    already looked (real_level has, through real_roles); otherwise this looks.
    """
    residents = list(residents)
    year, month = active_period()
    now = _month_index(year, month)
    ids = [r.pk for r in residents]
    index = F("year") * 12 + F("month") - 1
    # Per resident: the last month on a list in effect by now, and the last month on any list.
    months: dict[int, tuple[int | None, int | None]] = {
        row["resident_id"]: (row["last_past"], row["last"])
        for row in Residency.objects.filter(resident_id__in=ids)
        .values("resident_id")
        .annotate(
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
    if with_roles is None:
        with_roles = set(
            RoleAssignment.objects.filter(resident_id__in=ids, year=year, month=month).values_list(
                "resident_id", flat=True
            )
        )
    out: dict[int, AccessLevel] = {}
    for resident in residents:
        if resident.is_kitchen:
            out[resident.pk] = AccessLevel.KITCHEN
        elif resident.is_superuser or resident.pk in with_roles:
            out[resident.pk] = AccessLevel.RESIDENT
        else:
            out[resident.pk] = _level(now, *months.get(resident.pk, (None, None)))
    return out


def _level(now: int, last_past: int | None, last: int | None) -> AccessLevel:
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


def real_level(user: Resident) -> AccessLevel:
    """The user's own level, never the preview. Memoised on the user instance, which lives one
    request, as residents.permissions.real_roles is."""
    cached = getattr(user, _LEVEL_MEMO, None)
    if cached is None:
        if user.is_kitchen:
            cached = AccessLevel.KITCHEN
        elif real_roles(user):  # a superuser or a role this month: the request has these already
            cached = AccessLevel.RESIDENT
        else:
            cached = _level_without_roles(user)
        setattr(user, _LEVEL_MEMO, cached)
    return cached


def _level_without_roles(user: Resident) -> AccessLevel:
    """levels_of for one person known to hold no role, in ONE query: the month in effect (what
    active_period works out) and their own months, aggregated together. Asked on every request."""
    from core.clock import current_date

    today = current_date()
    index = F("year") * 12 + F("month") - 1
    in_effect = Q(year__lt=today.year) | Q(year=today.year, month__lte=today.month)
    row = Residency.objects.aggregate(
        now=Max(index, filter=in_effect),
        last_past=Max(index, filter=in_effect & Q(resident=user)),
        last=Max(index, filter=Q(resident=user)),
    )
    now = row["now"] if row["now"] is not None else _month_index(today.year, today.month)
    return _level(now, row["last_past"], row["last"])


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


def may_open(level: AccessLevel, url: str) -> bool:
    """Whether `level` may open the page at a site path, as the gate would decide a GET of it. The
    front page counts for the kitchen too, which the gate sends on to its channel from there."""
    try:
        match = resolve(urlsplit(url).path)
    except Resolver404:
        return level == AccessLevel.RESIDENT
    if match.view_name == "dashboard" and level == AccessLevel.KITCHEN:
        return True
    return allows(level, feature_of(match.func))
