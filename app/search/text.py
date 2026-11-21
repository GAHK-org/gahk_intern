"""Text helpers for the site-wide search: matching names people type loosely, and marking matches."""

import html
import re
import unicodedata

from django.utils.safestring import SafeString, mark_safe

# Spelled out the way people type on a keyboard without them: "oelkaelder", "vaerelse", "Aarhus".
_DANISH = str.maketrans({"æ": "ae", "ø": "oe", "å": "aa"})


def fold(text: str) -> str:
    """`text` lowercased, æøå spelled out and other accents dropped, so "Ølkælder", "ølkælder" and
    "oelkaelder" are the same word, and "Café" is "cafe"."""
    spelled = text.casefold().translate(_DANISH)
    return "".join(c for c in unicodedata.normalize("NFKD", spelled) if not unicodedata.combining(c))


def _pattern(marks: list[str]) -> re.Pattern[str] | None:
    # Longest first, so "fødselsdage" is marked before its stem inside it.
    ordered = sorted({m for m in marks if m}, key=len, reverse=True)
    return re.compile("|".join(re.escape(m) for m in ordered), re.IGNORECASE) if ordered else None


def mark(text: str, marks: list[str]) -> SafeString:
    """`text`, escaped, with each match of `marks` in <mark class="search-hit">."""
    pattern = _pattern(marks)
    if pattern is None:
        return mark_safe(html.escape(text))  # noqa: S308  # nosec - escaped on the line it is built
    parts: list[str] = []
    at = 0
    for match in pattern.finditer(text):
        parts.append(html.escape(text[at : match.start()]))
        parts.append(f'<mark class="search-hit">{html.escape(match.group())}</mark>')
        at = match.end()
    parts.append(html.escape(text[at:]))
    return mark_safe("".join(parts))  # noqa: S308  # nosec - every piece of `text` is escaped above


def excerpt(text: str, marks: list[str], words: int = 26) -> str:
    """About `words` words of plain `text`: around its first match, or from the start without one."""
    tokens = text.split()
    if len(tokens) <= words:
        return " ".join(tokens)
    pattern = _pattern(marks)
    hit = next((i for i, token in enumerate(tokens) if pattern and pattern.search(token)), 0)
    start = max(0, min(hit - words // 3, len(tokens) - words))
    end = start + words
    return f"{'… ' if start else ''}{' '.join(tokens[start:end])}{' …' if end < len(tokens) else ''}"
