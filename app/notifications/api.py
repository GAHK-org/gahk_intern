"""Notifications' public interface — the only module other domains may import from `notifications`
(enforced by Tach; see docs/domain-boundaries.md).

Notifications is PLATFORM, not a feature: it is how every feature reaches people, in-app and by
push, the way core.push is how it reaches a device. Features sit above it and call down into this
module; it never imports a feature. The split is the one services' module docstring describes: a
feature decides the audience and the wording, this app decides everything after that from each
resident's own settings.

  * COMMANDS: `notify` (a fan-out) and `notify_grouped` (one resident, folded into their existing
    row: "Anders og Bente svarede").
  * QUERIES: `everyone()`, the house as an audience for a feature to narrow with its rollout gate;
    `push_turned_off`, for a feature that shows a resident's own setting next to the thing.
  * CATEGORIES: the keys a feature notifies under, and `Importance`. See categories' docstring
    before adding one.
  * EXTENSION POINT: `register_categories`, for a feature whose categories follow from its own data
    (Den Hurtige registers one per room from den_hurtige.handlers). Before it existed, this app
    imported den_hurtige.channels to build them, which a platform module may not do.

What stays private: the models — a Notification row belongs to this app, so `notify_grouped` says
whether anyone was told rather than handing the row over — and the views, the settings page and
the delivery machinery.
"""

import datetime
from collections.abc import Iterable

from residents.api import Resident

from . import categories, services
from .categories import (
    CHAT_LIKES,
    CHAT_REPLIES,
    EVENTS_CHANGES,
    EVENTS_COMMENTS,
    EVENTS_NEW,
    EVENTS_PERSONAL,
    GENERAL,
    POSTS_LIKES,
    POSTS_NEW,
    POSTS_REPLIES,
    REPAIRS,
    SYSTEM,
    Category,
    Importance,
)
from .models import NotificationPreference
from .services import Recipients, everyone, notify

__all__ = [
    "CHAT_LIKES",
    "CHAT_REPLIES",
    "EVENTS_CHANGES",
    "EVENTS_COMMENTS",
    "EVENTS_NEW",
    "EVENTS_PERSONAL",
    "GENERAL",
    "POSTS_LIKES",
    "POSTS_NEW",
    "POSTS_REPLIES",
    "REPAIRS",
    "SYSTEM",
    "Category",
    "Importance",
    "Recipients",
    "everyone",
    "notify",
    "notify_grouped",
    "push_turned_off",
    "register_categories",
]


# --- Commands -------------------------------------------------------------------------------------


def notify_grouped(
    recipient_id: int,
    *,
    category: str,
    group_key: str,
    actor: Resident,
    verb: str,
    url: str,
    body: str = "",
    importance: Importance | None = None,
    expires_at: datetime.datetime | None = None,
    background: bool = True,
) -> bool:
    """Tell ONE resident, folding into their row for `group_key` (services.notify_grouped). Returns
    whether they were told at all — in-app, by push or both — not the row itself."""
    row = services.notify_grouped(
        recipient_id,
        category=category,
        group_key=group_key,
        actor=actor,
        verb=verb,
        url=url,
        body=body,
        importance=importance,
        expires_at=expires_at,
        background=background,
    )
    return row is not None


# --- Queries --------------------------------------------------------------------------------------


def push_turned_off(resident_id: int, category: str) -> bool:
    """Whether this resident turned push off for `category` on the settings page. Preferences are
    stored as exceptions (see models), so no row is the default: on."""
    return NotificationPreference.objects.filter(
        resident_id=resident_id, category=category, push=False
    ).exists()


# --- Extension point: a feature's own categories --------------------------------------------------


def register_categories(added: Iterable[Category]) -> None:
    """Add categories generated from a feature's own data, each with its settings toggle. Call it
    from the feature's AppConfig.ready() — its handlers.py. They list first within their group.

    The keys are stored on rows and preferences like any other, so renaming one is a data migration.
    A key already taken, or a group the settings page does not draw (categories.GROUPS), fails at
    startup.
    """
    categories.register(added)
