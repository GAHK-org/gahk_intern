"""What each access level gets: the table, and which feature each page is in.

Roles say what someone may DO (moderate, edit gahk.dk, the embedsgrupper's tools). The access level
says which parts of the site exist for them at all. Whose level is what lives in access.levels; this
module is the rule, and knows nothing about the alumneliste.

THE TABLE (POLICY) is the whole rule: each level's access to each Feature, NONE, READ or FULL. READ
lets through only requests that change nothing (GET, HEAD, OPTIONS). A feature is a part of the site
that the URLconf marks with `in_feature`; a view that belongs to no feature is for residents only,
so a new page is closed to everyone else until somebody puts it in a feature on purpose.

The gate (access.middleware.AccessLevelGate) applies the table to every request; the sidebar, the
topbar, notifications, live updates and each feature's rollout gate (core.rollout) ask it the same
question, so a level never sees a link, a notification or a live signal for a page it cannot open.
"""

from collections.abc import Callable, Iterable
from enum import IntEnum, StrEnum
from typing import Any, cast
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.urls import Resolver404, URLPattern, URLResolver, resolve


class AccessLevel(StrEnum):
    """Who someone is to the intranet. Worked out from the alumneliste (access.levels) and stored on
    the account (Resident.access_level)."""

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
# For the account's fields (Resident.access_level and its override).
LEVEL_CHOICES = [(level.value, LEVEL_LABELS[level]) for level in AccessLevel]
# What the Indstilling may set by hand: any level but the kitchen's, which is the account's flag.
OVERRIDE_CHOICES = [(value, label) for value, label in LEVEL_CHOICES if value != AccessLevel.KITCHEN]


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
