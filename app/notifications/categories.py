"""The notification category registry — what a resident can be notified about, and how loudly.

Categories are **constants in code, not database rows**, for the reason den_hurtige.channels gives
for its channels: they change about once a year, every one of them needs code that produces it, and
a registry in a tuple costs no query on the settings page or on every fan-out.

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

from dataclasses import dataclass
from enum import IntEnum

# A leaf module (it imports nothing of ours), so den_hurtige.services importing notifications is not
# a cycle.
from den_hurtige import channels


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


# Display order. System and general last: they are rare and cannot be turned off in-app, so the
# toggles people actually come to the settings page for are at the top.
GROUPS: tuple[tuple[str, str], ...] = (
    ("chat", "Den Hurtige"),
    ("posts", "Opslagstavlen"),
    ("events", "Begivenheder"),
    ("repairs", "Reparationer"),
    ("gahkflix", "GAHKFlix"),
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
    "gahkflix": "film",
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
GAHKFLIX_NEW = "gahkflix.new"
GAHKFLIX_CAST = "gahkflix.cast"
GAHKFLIX_UPLOADS = "gahkflix.uploads"
GAHKFLIX_APPROVALS = "gahkflix.approvals"
GAHKFLIX_DRAFTS = "gahkflix.drafts"

# The prefix of a Den Hurtige room's category. One category per room, generated from the channel
# registry below, so adding a channel gives it a settings toggle without touching this file.
CHAT_ROOM_PREFIX = "chat.room."


def chat_room_key(slug: str) -> str:
    return f"{CHAT_ROOM_PREFIX}{slug}"


def _chat_rooms() -> tuple[Category, ...]:
    return tuple(
        Category(
            chat_room_key(channel.slug),
            channel.name,
            group="chat",
            description=f"Nye beskeder i {channel.name}.",
            in_app=False,
        )
        for channel in channels.CHANNELS
    )


def _build() -> tuple[Category, ...]:
    return (
        *_chat_rooms(),
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
        Category(GAHKFLIX_NEW, "Nye videoer", "gahkflix", "Når en ny video udgives på GAHKFlix."),
        Category(GAHKFLIX_CAST, "Når du medvirker", "gahkflix", "Når du bliver krediteret i en video."),
        Category(
            GAHKFLIX_UPLOADS,
            "Dine uploads og anmodninger",
            "gahkflix",
            "Når din video er behandlet, når en anmodning, du har sendt, er afgjort, og om kladder, du ikke har udgivet.",
            in_app_optional=False,
        ),
        Category(
            GAHKFLIX_APPROVALS,
            "Godkendelser",
            "gahkflix",
            "Ændringer og sletninger, som en administrator skal godkende.",
            importance=Importance.HOEJ,
            in_app_optional=False,
        ),
        Category(
            GAHKFLIX_DRAFTS,
            "Kladder, der ikke er udgivet",
            "gahkflix",
            "Ugentligt overblik over upublicerede kladder, og besked dagen før en slettes. Til administratorer.",
            in_app_optional=False,
        ),
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


CATEGORIES: tuple[Category, ...] = _build()

BY_KEY: dict[str, Category] = {c.key: c for c in CATEGORIES}

GROUP_NAMES: dict[str, str] = dict(GROUPS)


def get(key: str) -> Category:
    """The category for `key`. Raises KeyError for an unknown key: a typo at a call site must fail
    loudly rather than notify under a category no settings toggle controls."""
    return BY_KEY[key]


def grouped() -> list[tuple[str, str, list[Category]]]:
    """(group key, group name, leaves) in display order — what the settings page renders."""
    return [(key, name, [c for c in CATEGORIES if c.group == key]) for key, name in GROUPS]
