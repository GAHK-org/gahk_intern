"""Embeds: another feature's thing, referenced from resident-authored Markdown.

`![Julefrokost 2026](album:12)` is an image whose source is a reference, `kind:key`, rather than an
address. core.markdown draws the feature's own widget in its place, given the widgets by whoever
renders (opslagstavle.embeds); without them, the text in the brackets stands in.

WHY THE BODY AND NOT A COLUMN PER KIND. The reference lives where the author put it, so a post can
hold any number of embeds, of any kind, wherever they belong in the text, and another kind of widget
is an entry in KINDS and a renderer, not a schema change.

WHY A REFERENCE AND NOT A LINK. The body stores what is embedded, never where its page is. Every
link in a widget is reversed when it is drawn, so posts keep pointing at the right pages however the
features change their addresses.

WHY IMAGE SYNTAX. `!` is what Markdown already says for "embed this here", so nothing new has to be
parsed: markdown-it reads an image, and core.markdown takes it from there. The text in the brackets
is the thing's name when it was embedded: what the author sees in the source, and what a reader sees
where no widget can be drawn (a notification, or a reader the feature keeps out).

WHAT IT SHOWS OF ITSELF. A kind in FEATURING can also name, after a slash, the things within it its
widget leads with, in order: `![Julefrokost 2026](album:12/88,91,95)` is the album with those three
pictures in its mosaic, the first one large. Without them the widget picks for itself (an album, its
newest). Each is a key the feature looks up like the embed's own, so a body still holds no addresses.
What is not one, one picked twice, or more than the widget has room for is passed over: the author
still meant the album, so it is drawn with what can be read of the rest.

A ROW'S NUMBER, OR A UUID. An album or a film is named by its number. Drive names its files and
folders only by a random uuid, never by a number that counts up (spec/features/Drive.md), so its
kind is keyed by that: `![Budget.xlsx](drive:3f2c9a1e-…)`. Each kind takes one sort of key, and a
reference with the other sort is no embed.

THE SOURCE IS THE REFERENCE. Renderers key their widgets by the source exactly as written, not by
`reference` written anew, so the composer finds an embed in the body by it however it was typed.
"""

import re
from typing import NamedTuple
from uuid import UUID

# The kinds a body can embed, and the icon a plain-text rendering (a notification) puts before the
# name. A new kind of widget is a name here and a renderer in opslagstavle.embeds.
KINDS: dict[str, str] = {
    "album": "📷",
    "gahkflix": "🎬",
    "drive": "📎",
}

# The kinds keyed by a uuid rather than a row's number: Drive's files and folders (one kind for both,
# since a uuid names one thing).
UUID_KEYED: frozenset[str] = frozenset({"drive"})

# The kinds whose embed can name what within it to show, and at most how many: an album, the
# pictures of its mosaic, which has room for seven.
FEATURING: dict[str, int] = {
    "album": 7,
}

# A key of at most 18 digits: anything longer is no row's key, and Postgres refuses to compare it.
_KEY = r"[1-9][0-9]{0,17}"
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_REFERENCE = re.compile(rf"([a-z]+):({_KEY}|{_UUID})(?:/(.*))?", re.DOTALL)
_FEATURED_KEY = re.compile(_KEY)

# Backslashed in the name, so "[Revy] 2009" stays the text of one embed.
_PUNCTUATION = re.compile(r"([\\\[\]])")


class Embed(NamedTuple):
    kind: str
    # A row's number, or a uuid for the kinds in UUID_KEYED.
    key: int | UUID
    # What within it the widget leads with, in order (FEATURING); () lets the widget pick.
    featured: tuple[int, ...] = ()


def parse(source: str | None) -> Embed | None:
    """The embed an image's source names, or None for anything else (an ordinary image).

    Of the featured keys, those that are keys, each once, as many as the kind's widget shows (none
    for a kind without them); the rest are passed over, never the embed.
    """
    match = _REFERENCE.fullmatch(source or "")
    if match is None or match.group(1) not in KINDS:
        return None
    kind, key, listed = match.groups()
    if (kind in UUID_KEYED) != (re.fullmatch(_UUID, key) is not None):
        return None
    keys = (int(each) for each in (listed or "").split(",") if _FEATURED_KEY.fullmatch(each.strip()))
    featured = tuple(dict.fromkeys(keys))[: FEATURING.get(kind, 0)]
    return Embed(kind, UUID(key) if kind in UUID_KEYED else int(key), featured)


def reference(kind: str, key: int | UUID, featured: tuple[int, ...] = ()) -> str:
    """`album:12`, or `album:12/88,91`: what an embed's source holds when the composer writes it."""
    return f"{kind}:{key}" + (f"/{','.join(map(str, featured))}" if featured else "")


def markdown(kind: str, key: int | UUID, name: str, featured: tuple[int, ...] = ()) -> str:
    """The Markdown that embeds it: `![Julefrokost 2026](album:12)`."""
    escaped = _PUNCTUATION.sub(r"\\\1", name)
    return f"![{escaped}]({reference(kind, key, featured)})"
