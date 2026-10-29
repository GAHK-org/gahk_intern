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
"""

import re

# The kinds a body can embed, and the icon a plain-text rendering (a notification) puts before the
# name. A new kind of widget is a name here and a renderer in opslagstavle.embeds.
KINDS: dict[str, str] = {
    "album": "📷",
    "gahkflix": "🎬",
}

# A key of at most 18 digits: anything longer is no row's key, and Postgres refuses to compare it.
_REFERENCE = re.compile(r"([a-z]+):([1-9][0-9]{0,17})")

# Backslashed in the name, so "[Revy] 2009" stays the text of one embed.
_PUNCTUATION = re.compile(r"([\\\[\]])")


def parse(source: str | None) -> tuple[str, int] | None:
    """(kind, key) for an embed's source, or None for anything else (an ordinary image)."""
    match = _REFERENCE.fullmatch(source or "")
    if match is None or match.group(1) not in KINDS:
        return None
    return match.group(1), int(match.group(2))


def reference(kind: str, key: int) -> str:
    """`album:12`: what an embed's source holds, and what renderers key their widgets by."""
    return f"{kind}:{key}"


def markdown(kind: str, key: int, name: str) -> str:
    """The Markdown that embeds it: `![Julefrokost 2026](album:12)`."""
    escaped = _PUNCTUATION.sub(r"\\\1", name)
    return f"![{escaped}]({reference(kind, key)})"
