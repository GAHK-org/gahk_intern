"""The notification category registry — what a resident can be notified about, and how loudly.

Categories are **constants in code, not database rows**, for the reason den_hurtige.channels gives
for its channels: they change about once a year, every one of them needs code that produces it, and
a registry in a tuple costs no query on the settings page or on every fan-out.

Most are written out below. A feature whose categories follow from its own data registers them
instead (`register`, through notifications.api.register_categories): Den Hurtige has one per room,
generated from its channel registry, so adding a channel gives it a settings toggle without touching
this file. This app is platform and never imports a feature (tach.toml), which is why the rooms come
to it rather than it going to den_hurtige.channels for them.

A category is a *leaf*: the unit a resident toggles on the settings page. Leaves are grouped
(`GROUPS`) only for display. The keys are stored — on Notification rows and on preference rows — so
renaming one is a data migration, not an edit.

Two things are decided here rather than at each call site, because they must not drift between the
features that share a category:

  * **Delivery capability.** Some notifications are push only: a new message in a Den Hurtige room
    is the chat itself, already in front of you the moment you open the app, so an in-app copy of
    every message would bury the notifications that are actually about *you*. `in_app=False` on a
    category takes the in-app toggle off the settings page and the row out of the dropdown.
  * **Default importance.** A caller may raise it for one notification (an event that was cancelled
    matters more than one that was created), but the category says what is normal.

**Before adding a category**, check that a resident would actually want to switch it off on its own.
The settings page is a table every resident scrolls; every leaf is another row of toggles. A new
category earns its place only when it is *recurring and plentiful enough to be noise to somebody* —
new posts, reactions, chat rooms. Anything else goes under an existing one:

  * **`general` is the catch-all.** Everything that has no category of its own: one-off messages about
    your own affairs ("din ansøgning er slettet"), and
    announcements to the whole kollegium. It cannot be turned off in-app, which is right for exactly
    these: rare, and about you. Raise `importance` at the call site if it matters more than Normal.
  * **`system` is about the site itself** — downtime, new features, something wrong with the app.
    Not about a resident's own case; that is `general`.

So a feature does *not* get its own group just because it is a feature: a notice that an application
was deleted is `general`, not a new "Søg værelse" group.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum


class Importance(IntEnum):
    """Four levels. Stored as the integer, so the order is the meaning: higher sorts first."""

    LAV = 1  # "Anders reagerede på din besked"
    NORMAL = 2  # new posts, replies, comments
    HOEJ = 3  # something you planned around changed: an event moved or was cancelled
    KRITISK = 4  # overrules the resident's settings — see services.notify

    @property
    def label(self) -> str:
        return IMPORTANCE_LABELS[self]


IMPORTANCE_LABELS = {
    Importance.LAV: "Lav",
    Importance.NORMAL: "Normal",
    Importance.HOEJ: "Høj",
    Importance.KRITISK: "Kritisk",
}

IMPORTANCE_CHOICES = [(int(level), label) for level, label in IMPORTANCE_LABELS.items()]


@dataclass(frozen=True)
class Category:
    key: str
    name: str
    group: str
    description: str = ""
    importance: Importance = Importance.NORMAL
    # Whether this category can appear in the dropdown / on the notifications page at all. False is
    # "push only" — see the module docstring.
    in_app: bool = True
    # Whether the resident may turn the in-app row off. False for what the house itself says (system
    # and general): those are few, and the list is where somebody who has push off still sees them.
    in_app_optional: bool = True
    # The id of this category's row on the settings page, for a feature that links straight to it
    # (Den Hurtige's room bell goes to #rum-<slug>). Empty: the row has no id.
    anchor: str = ""


# Display order. System and general last: they are rare and cannot be turned off in-app, so the
# toggles people actually come to the settings page for are at the top.
GROUPS: tuple[tuple[str, str], ...] = (
    ("chat", "Den Hurtige"),
    ("posts", "Opslagstavlen"),
    ("events", "Begivenheder"),
    ("repairs", "Reparationer"),
    ("system", "System"),
    ("general", "Generelt"),
)

# The base.html sprite icon each group's notifications show with.
GROUP_ICONS: dict[str, str] = {
    "system": "gear",
    "general": "inbox",
    "chat": "flash",
    "posts": "board",
    "events": "calendar",
    "repairs": "wrench",
}

# The two catch-alls — see "Before adding a category" in the module docstring. Most features that
# notify rarely should use GENERAL rather than a category of their own.
SYSTEM = "system"
GENERAL = "general"
CHAT_REPLIES = "chat.replies"
CHAT_LIKES = "chat.likes"
POSTS_NEW = "posts.new"
POSTS_REPLIES = "posts.replies"
POSTS_LIKES = "posts.likes"
EVENTS_NEW = "events.new"
EVENTS_CHANGES = "events.changes"
EVENTS_PERSONAL = "events.personal"
EVENTS_COMMENTS = "events.comments"
REPAIRS = "repairs"


def _build() -> tuple[Category, ...]:
    return (
        Category(CHAT_REPLIES, "Svar på dine beskeder", "chat", "Når nogen svarer på din besked."),
        Category(
            CHAT_LIKES,
            "Reaktioner på dine beskeder",
            "chat",
            "Når nogen reagerer på din besked.",
            importance=Importance.LAV,
        ),
        Category(POSTS_NEW, "Nye opslag", "posts", "Når nogen slår op på tavlen."),
        Category(POSTS_REPLIES, "Kommentarer på dine opslag", "posts", "Når nogen kommenterer dit opslag."),
        Category(
            POSTS_LIKES,
            "Reaktioner på dine opslag",
            "posts",
            "Når nogen reagerer på dit opslag.",
            importance=Importance.LAV,
        ),
        Category(EVENTS_NEW, "Nye begivenheder", "events", "Når der oprettes en begivenhed, du kan se."),
        Category(
            EVENTS_CHANGES,
            "Ændringer og aflysninger",
            "events",
            "Når en begivenhed, du har sagt ja til, flyttes eller aflyses.",
            importance=Importance.HOEJ,
        ),
        Category(
            EVENTS_PERSONAL,
            "Påmindelser og venteliste",
            "events",
            "Svarfrister og når du rykker op fra ventelisten.",
            importance=Importance.HOEJ,
        ),
        Category(
            EVENTS_COMMENTS,
            "Kommentarer på dine begivenheder",
            "events",
            "Når nogen kommenterer en begivenhed, du er vært for.",
        ),
        Category(REPAIRS, "Reparationer", "repairs", "Nye og overdragede reparationer for din gruppe."),
        Category(
            SYSTEM,
            "Systembeskeder",
            "system",
            "Om selve siden, fx nedetid og nye funktioner.",
            in_app_optional=False,
        ),
        Category(
            GENERAL,
            "Generelle beskeder",
            "general",
            "Alt, der ikke har sin egen kategori — fx besked om din ansøgning eller meddelelser til hele kollegiet.",
            in_app_optional=False,
        ),
    )


_BUILT_IN: tuple[Category, ...] = _build()

# What features registered, in registration order (INSTALLED_APPS order, from their ready()).
_registered: list[Category] = []

_by_key: dict[str, Category] = {c.key: c for c in _BUILT_IN}


def register(added: Iterable[Category]) -> None:
    """Add a feature's own categories. See notifications.api.register_categories.

    Registering the same category again is a no-op, since ready() can run twice. A different one
    under a key that is taken, or one in a group the settings page does not draw, is a programming
    error and fails at startup rather than notifying under a toggle nobody can find.
    """
    for category in added:
        existing = _by_key.get(category.key)
        if existing == category:
            continue
        if existing is not None:
            raise ValueError(f"Notification category {category.key!r} is already registered.")
        if category.group not in {key for key, _name in GROUPS}:
            raise ValueError(
                f"Notification category {category.key!r} is in unknown group {category.group!r}."
            )
        _registered.append(category)
        _by_key[category.key] = category


def all_categories() -> tuple[Category, ...]:
    """Every category. Registered ones come first, so within a group a feature's generated toggles
    (Den Hurtige's rooms) sit above the fixed ones (replies, reactions)."""
    return (*_registered, *_BUILT_IN)


def get(key: str) -> Category:
    """The category for `key`. Raises KeyError for an unknown key: a typo at a call site must fail
    loudly rather than notify under a category no settings toggle controls."""
    return _by_key[key]


def lookup(key: str) -> Category | None:
    """The category for `key`, or None — for reading stored rows, whose category may since have
    been retired (they outlive code by up to their TTL plus the retention)."""
    return _by_key.get(key)


def grouped() -> list[tuple[str, str, list[Category]]]:
    """(group key, group name, leaves) in display order — what the settings page renders."""
    every = all_categories()
    return [(key, name, [c for c in every if c.group == key]) for key, name in GROUPS]
