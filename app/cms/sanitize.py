"""HTML sanitization for the website's content (F-006 hardening).

Bodies are published to gahk.dk as they are stored (gahk.dk has no sanitizer of its own and renders
them as trusted), so editor input must be scrubbed of scripts/handlers before it is stored. nh3 (ammonia) always strips <script>, event handlers and dangerous URL schemes, and sanitizes
the CSS inside `style`. The allowlist below is intentionally generous — it keeps the formatting the
migrated GAHK pages rely on (tables, inline styles, images, links) while removing anything executable.
"""

import html
import re

import nh3

ALLOWED_TAGS = {
    "p",
    "br",
    "hr",
    "div",
    "span",
    "a",
    "img",
    "figure",
    "figcaption",
    "ul",
    "ol",
    "li",
    "dl",
    "dt",
    "dd",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "td",
    "th",
    "caption",
    "colgroup",
    "col",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "strong",
    "em",
    "b",
    "i",
    "u",
    "s",
    "small",
    "sub",
    "sup",
    "blockquote",
    "pre",
    "code",
    "abbr",
    "cite",
    "mark",
}

ALLOWED_ATTRS = {
    "*": {"style", "class", "id", "title", "dir", "lang"},
    "a": {"href", "target"},  # `rel` is managed by link_rel below
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "col": {"span"},
    "colgroup": {"span"},
}


def clean_html(html: str | None) -> str | None:
    """Return `html` with scripts/handlers/unsafe URLs removed; safe to render with |safe."""
    if not html:
        return html
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},  # relative URLs (/static/…, #…) are allowed too
        link_rel="noopener noreferrer",
    )


_TAG_RE = re.compile(r"<[^>]*>")


def plain_text_from_html(value: str | None) -> str:
    """The text a legacy title showed as: tags dropped, entities decoded, whitespace collapsed.

    For the legacy page headers and news titles, which were rendered `|safe`; they are plain text
    now and escaped wherever they are shown.
    """
    return " ".join(html.unescape(_TAG_RE.sub(" ", value or "")).split())
