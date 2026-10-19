"""Links in resident-authored text: found, shortened, and made clickable.

ONE SET OF RULES FOR ALL FIVE SURFACES. Den Hurtige's messages and its thread replies, the
opslagstavle and begivenheder comment threads, and the Markdown bodies of a notice or an event
description. Three of those are plain text and two are Markdown, so the rules reach them by two
different routes — `linkify` below, and the core rule in core/markdown.py — but both routes call
the same `shorten`, and both use the same scanner, so a URL cannot be found in a comment and missed
one line above it in the post it answers.

WHY NOT A REGEX OF OUR OWN. Finding a URL in prose is one of those problems that looks like twenty
minutes and is not: a trailing full stop belongs to the sentence, a trailing bracket belongs to the
URL if an opening one was inside it, `dr.dk` is a link and `bl.a.` is not, an email is neither, and
a Danish kollegium types `æ` into paths. linkify-it-py is markdown-it's own scanner, ships with the
parser this project already uses, and is the SAME code that finds the links in a Markdown body —
which is the property that actually matters here, because anything else would mean a comment and a
post disagreeing about what counts as a link.

WHY SHORTEN. A pasted link is routinely 120 characters of tracking parameters, and a chat bubble is
about 40 wide. Unshortened it wraps to five lines and buries the message it was sent with, which is
why the messenger apps this feature is modelled on all show the host and a little path. The full
URL never goes anywhere: it stays in `href`, and where the cut actually hid something it stays in
`title` too (see `title_for`). The shortening is purely what the reader is shown.

NOTHING HERE DECIDES WHAT IS SAFE. The Markdown route hands its output to nh3 as it always did, and
the plain-text route escapes every character of the author's text before a single tag is added. See
`linkify` on why that ordering is the whole security argument for this module.
"""

import re

from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe
from linkify_it import LinkifyIt

# Characters of link TEXT a reader is shown. Roughly a chat bubble's width at the message font, so
# a shortened link is one line rather than the four an unshortened one wraps to.
MAX_LINK_TEXT = 42

# The scanner. `mailto:` addresses are wanted (someone pastes a mail address and it should be
# clickable), and the default fuzzy matching is what catches a bare `gahk.dk/intern` written without
# a scheme — which is how people actually type an internal link to each other.
_LINKIFY = LinkifyIt()

_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_WWW = re.compile(r"^www\.", re.IGNORECASE)


def _display(url: str, limit: int) -> tuple[str, bool]:
    """(what the reader sees, whether anything was cut to fit).

    Scheme and `www.` go first, because they are the part of a URL that carries no information at
    all — every reader knows a link is a link — and dropping them buys back a third of the budget
    before anything has to be cut. A trailing slash goes for the same reason. None of that counts
    as CUT: the address is still entirely legible, so the second half of the tuple stays False and
    the link gets no tooltip.

    What is cut is cut from the END, not the middle. The host is the part that answers "should I
    click this", so it is the part that must survive whole; a middle ellipsis protects the tail
    instead, which on a modern URL is a tracking parameter.
    """
    if url.lower().startswith("mailto:"):
        return url[len("mailto:") :] or url, False
    text = _WWW.sub("", _SCHEME.sub("", url)).rstrip("/")
    if not text:  # a URL that was nothing but a scheme; show it as typed rather than as nothing
        return url, False
    if len(text) <= limit:
        return text, False
    return text[: limit - 1].rstrip() + "…", True


def shorten(url: str, limit: int = MAX_LINK_TEXT) -> str:
    """What the reader sees in place of `url`. See `_display`."""
    return _display(url, limit)[0]


def title_for(url: str, limit: int = MAX_LINK_TEXT) -> str:
    """The `title` a link to `url` should carry, or "" for none.

    ONLY WHEN SOMETHING WAS CUT. A tooltip that repeats what is already on screen is noise, and it
    is worse than noise on a phone, where there is no hover to dismiss it but a long-press menu that
    now has a redundant line in it. `mailto:anton@gahk.dk` showing its own scheme back was the case
    that prompted this. When a URL genuinely did not fit, the tooltip is the only way back to the
    rest of it, and then it earns its place.
    """
    return url if _display(url, limit)[1] else ""


def linkify(text: str | None) -> SafeString:
    """Plain author text as HTML, with every URL in it turned into a shortened link.

    THE ORDER OF THE TWO OPERATIONS IS THE SECURITY ARGUMENT, and it is the only thing in this
    function worth being careful about. Every character between the links is `escape`d before it is
    concatenated, and each link's own pieces are escaped individually — the href, the title and the
    visible text — so the only unescaped characters in the result are the tags this function writes
    itself. Author input therefore cannot reach the output as markup by any path, which is what
    makes the mark_safe at the end true rather than hopeful.

    Note what is NOT trusted: the href comes from linkify-it's `url` field, not from the author's
    keystrokes, and it is checked against a scheme allowlist below. A scanner that can be talked
    into returning `javascript:...` would otherwise walk straight through the escaping, because a
    scheme is not a character that escaping changes.

    This replaces Django's `|urlize`, which the two comment threads used. urlize does not shorten,
    marks up nothing else this needs, and — the reason it had to go rather than be lived with —
    is a different scanner from the one the Markdown bodies use, so a link could be live in a
    comment and dead in the post above it.
    """
    if not text:
        return mark_safe("")  # nosec — a literal empty string
    matches = _LINKIFY.match(text)
    if not matches:
        return mark_safe(escape(text))  # noqa: S308  # nosec — escape() output only

    out: list[str] = []
    cursor = 0
    for match in matches:
        url = match.url
        # A scheme allowlist, matching core.markdown's ALLOWED_URL_SCHEMES. linkify-it does not
        # emit `javascript:` today; this is here so that it still could not matter if it ever did.
        if url.split(":", 1)[0].lower() not in ("http", "https", "mailto"):
            continue
        title = title_for(url)
        out.append(escape(text[cursor : match.index]))
        out.append(
            f'<a href="{escape(url)}"'
            + (f' title="{escape(title)}"' if title else "")
            + f' rel="noopener noreferrer nofollow">{escape(shorten(url))}</a>'
        )
        cursor = match.last_index
    out.append(escape(text[cursor:]))
    # ruff S308 and bandit flag mark_safe, and they are right to. What is being marked is a list of
    # escape() results and tags built above from escaped pieces; see the docstring.
    return mark_safe("".join(out))  # noqa: S308  # nosec
