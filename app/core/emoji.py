"""Emoji validation shared by every feature that offers one-tap reactions.

Two separate problems live here. Allowing any emoji means arbitrary text reaches the database, so
"LOL" and markup have to be rejected. And an emoji is not one character: 👍 is one code point,
❤️ is two, 👨‍👩‍👧‍👦 is seven, and a flag is two — so a naive length check either rejects real
emoji or lets someone paste 👍🎉🔥 and land three of them in one reaction bubble.

There is no stdlib grapheme splitter, so this parses one emoji cluster and insists nothing is left
over:

    flag          two regional indicators
    keycap        digit / # / * + U+FE0F + U+20E3
    everything    base symbol, optional U+FE0F, optional skin tone,
    else          then any number of ZWJ-joined repeats of the same

Known limit: bare ❤ (U+2764) and ❤️ (U+2764 U+FE0F) are distinct reactions. Every mobile keyboard
emits the U+FE0F form and EMOJI_SHORTLIST matches it, so it does not arise in practice.

The *messages* stay with each feature's form (they are Danish user-facing text); only the grammar
lives here, so a fix — the bare-heart limit above, say — lands in one place.
"""

import unicodedata

# One-tap reactions offered by the pickers. Any emoji is still accepted — this is only the
# shortlist, because in a desktop browser a bare text field gives you a cursor and no help, while
# these are a single click. The heart carries U+FE0F because that is the form every mobile keyboard
# sends, and counts must not split.
EMOJI_SHORTLIST = [
    "👍",
    "❤️",
    "😂",
    "🎉",
    "🙏",
    "👀",
    "🔥",
    "😮",
    "😢",
    "👎",
    "✅",
    "❌",
    "☕",
    "🍺",
    "🍕",
    "🎂",
    "🚲",
    "🔑",
]

ZWJ = 0x200D
VS16 = 0xFE0F
KEYCAP = 0x20E3
SKIN_TONES = (0x1F3FB, 0x1F3FF)
REGIONAL = (0x1F1E6, 0x1F1FF)
KEYCAP_BASES = frozenset("0123456789#*")


def _in(code: int, span: tuple[int, int]) -> bool:
    return span[0] <= code <= span[1]


def _base_at(value: str, i: int) -> int:
    """Consume one base symbol plus its presentation/skin-tone modifiers; return the next index,
    or -1 if there is no base symbol at `i`."""
    if i >= len(value) or unicodedata.category(value[i]) != "So":
        return -1
    i += 1
    if i < len(value) and ord(value[i]) == VS16:
        i += 1
    if i < len(value) and _in(ord(value[i]), SKIN_TONES):
        i += 1
    return i


def _consume_one(value: str) -> int:
    """Length in code points of the first emoji cluster, or -1 if it does not start with one."""
    codes = [ord(c) for c in value]

    # Flag: exactly two regional indicators.
    if len(codes) >= 2 and all(_in(c, REGIONAL) for c in codes[:2]):
        return 2

    # Keycap: 1️⃣ — the only case where a digit is legitimate.
    if len(codes) >= 3 and value[0] in KEYCAP_BASES and codes[1] == VS16 and codes[2] == KEYCAP:
        return 3

    i = _base_at(value, 0)
    if i < 0:
        return -1
    while i < len(value) and ord(value[i]) == ZWJ:
        nxt = _base_at(value, i + 1)
        if nxt < 0:  # a trailing joiner with nothing after it
            return -1
        i = nxt
    return i


def normalize_emoji(value: str) -> str:
    """NFC-normalise and trim. Callers compare against this, so a reaction cannot be stored in one
    normalisation and counted in another."""
    return unicodedata.normalize("NFC", value).strip()


def is_emoji(value: str) -> bool:
    """Whether `value` is exactly one emoji cluster and nothing else. Expects `normalize` output."""
    if not value:
        return False
    consumed = _consume_one(value)
    # `consumed != len(value)` is the "several pasted emoji, or trailing text" case.
    return consumed >= 0 and consumed == len(value)


def is_only_one_emoji(value: str) -> bool:
    """Whether `value` starts with a valid emoji but carries more after it — the case worth its own
    Danish message ("Vælg kun én emoji.") rather than "that is not an emoji"."""
    return _consume_one(value) >= 0
