"""Markdown rendering for resident-authored content (opslagstavlen).

Two independent defences, on purpose:

  1. markdown-it runs with `html=False`, so `<script>` in the source is *escaped to text* and never
     becomes a node — a reader sees the literal characters. This is the parser refusing to build
     dangerous output in the first place.
  2. nh3 then cleans the generated HTML against the allowlist below. This layer exists for the day
     someone enables an extension, or flips `html=True`, without thinking it through.

Either alone would probably do. Both means a single mistake is not a stored-XSS hole on a page every
resident loads.

**Rendered on read, never stored.** The markdown source is the only thing in the database. That is a
deliberate trade: rendering costs a few milliseconds per page (pure-Python markdown-it plus Rust
nh3, on a page nobody polls), and in exchange a change to the allowlist below — tightening it after
a review, say — takes effect for every post that already exists, the moment the deploy lands. Store
the HTML instead and a `rerender` management command becomes *mandatory*, with cron's failure mode:
silence. Nothing on a page load could even detect HTML produced under an older allowlist without
re-rendering it anyway.

If profiling ever disagrees, the escape hatch is `cache.get_or_set` keyed on
`(pk, edited_at or created_at)` — five lines, no schema change. Deliberately not built now.

The allowlist is much tighter than `cms/sanitize.py`. That one is generous to preserve the
formatting of migrated GAHK pages written by trusted editors; this one is what any of ~100 residents
can put on a page.
"""

import re
from urllib.parse import unquote

import nh3
from django.conf import settings
from django.utils.safestring import SafeString, mark_safe
from markdown_it import MarkdownIt
from markdown_it.token import Token

from .links import shorten, title_for

# The "default" preset — NOT "commonmark" or "gfm-like", both of which set html=True to be
# spec-compliant. `default` gives tables and strikethrough, which the værelsesrunde results post
# needs, and leaves raw HTML off. The explicit dict is belt-and-braces: it survives someone
# changing the preset name without re-reading this comment.
#
# LINKIFY IS ON, and it is the reason `markdown-it-py[linkify]` is pinned with its extra: without
# linkify-it-py installed, `linkify: True` raises at import. It turns a bare pasted URL into a
# link, which is how people actually write one — `[tekst](url)` is a thing you have to know, and a
# board where the obvious way to share something produces dead text is a board people work around.
# It changes nothing about safety: linkify only ever produces an `a` with an `href`, both of which
# were already in the allowlist below, and nh3 still cleans the result.
#
# typographer stays off. It is the half of the same option pair that rewrites quotes and dashes,
# and a post is not the place to be second-guessing what somebody typed.
_MD = MarkdownIt("default", {"html": False, "linkify": True, "typographer": False})

ALLOWED_TAGS = {
    "p",
    "br",
    "hr",
    "strong",
    "em",
    "b",
    "i",
    "s",
    "del",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    # No h1: the page owns its single <h1> (the post's title), and a second one is both an
    # accessibility problem and a way to out-shout every other post in the list.
    "h2",
    "h3",
    "h4",
    "a",
    "img",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}

# No "*" bucket at all, unlike cms/sanitize.py: that means no style, no class, no id, and no event
# handlers. `style` and `class` are the CSS-injection and break-the-layout surface, and `.prose-gahk`
# already styles everything a post can contain.
ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "th": {"colspan", "rowspan", "scope"},
    "td": {"colspan", "rowspan"},
}

ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}

# Where an <img> may point. The compose toolbar always uploads to MEDIA_URL, so a remote src can
# only arrive from hand-typed markdown — and a remote image on an internal page is a tracking pixel
# that hands a third party the IP and read-time of every resident who opens the post. nh3's
# url_schemes constrains the scheme but not the host, so this is the only thing that stops it.
_LOCAL_IMAGE_PREFIXES = (settings.MEDIA_URL, settings.STATIC_URL)

# An <img> whose src was dropped by the filter above renders as a broken-image icon. Removing the
# tag outright is friendlier, and safe to do with a regex *because* nh3 has already normalised the
# markup by this point — this never sees author input.
_SRCLESS_IMG = re.compile(r"<img(?![^>]*\ssrc=)[^>]*>")

# Every <img>, for the images-less excerpt mode of render_markdown. Same reasoning as above: it runs
# on nh3's output, never on author input.
_ANY_IMG = re.compile(r"<img[^>]*>")

# `# Overskrift` is the obvious way to write a heading, so it has to do something sensible — but h1
# is not in the allowlist (the page owns its single h1), and nh3 would strip the tag and leave the
# text bare and unstyled, which looks like a bug. Demote it to h2 instead. Applied to markdown-it's
# output, before nh3: with html=False the author cannot have written an <h1> themselves, so every
# match here is one markdown-it generated from a leading '#'.
_H1_OPEN = re.compile(r"<h1(\s[^>]*)?>")
_H1_CLOSE = re.compile(r"</h1>")


def _shorten_autolinks(state: object) -> None:
    """markdown-it core rule: show a bare URL shortened, and keep the whole of it in `title`.

    Applies ONLY to links the author did not write the text of — `markup` is "linkify" for a bare
    URL and "autolink" for `<https://…>`, and is empty for `[tekst](url)`. That distinction is the
    whole rule: rewriting the text of a link somebody chose the words for would be vandalism, while
    a bare URL has no words to lose. See core.links.shorten for what is cut and why.

    A rule on the token stream rather than a regex over the rendered HTML, because the tokens
    already know which links were written and which were found — recovering that from `<a href=…>`
    afterwards would mean comparing the text against the href and guessing.
    """
    for token in state.tokens:  # type: ignore[attr-defined]
        if token.type != "inline":
            continue
        children: list[Token] = token.children or []
        for index, child in enumerate(children):
            if child.type != "link_open" or child.markup not in ("linkify", "autolink"):
                continue
            href = child.attrGet("href")
            if not isinstance(href, str):
                continue
            # A tooltip only where one recovers something — see core.links.title_for.
            title = title_for(href)
            if title:
                child.attrSet("title", title)
            # linkify and autolink both emit exactly link_open, one text token, link_close.
            following = children[index + 1] if index + 1 < len(children) else None
            if following is not None and following.type == "text":
                following.content = shorten(href)


_MD.core.ruler.push("shorten_autolinks", _shorten_autolinks)


def _local_images_only(tag: str, attr: str, value: str) -> str | None:
    """nh3 attribute filter: drop an image source that is not one of ours."""
    if tag == "img" and attr == "src" and not value.startswith(_LOCAL_IMAGE_PREFIXES):
        return None
    return value


def _demote_h1(html: str) -> str:
    return _H1_CLOSE.sub("</h2>", _H1_OPEN.sub("<h2>", html))


def _clean(html: str) -> str:
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        url_schemes=ALLOWED_URL_SCHEMES,
        # nofollow as well as noopener/noreferrer: these links are resident-authored, so the board
        # should not pass the kollegium's search-engine standing to whatever anyone links to.
        link_rel="noopener noreferrer nofollow",
        attribute_filter=_local_images_only,
    )


def render_markdown(source: str | None, *, with_images: bool = True) -> SafeString:
    """Render `source` to sanitized HTML, safe to output unescaped.

    The only function in the project that marks resident-authored content safe. Nothing else may —
    and no board template should ever use `|safe`, because the next refactor that hands it raw
    source would turn that into stored XSS.

    `with_images=False` drops every <img> from the result. It exists for a feed excerpt: a post with
    several pictures would otherwise fill the whole viewport, so opslagstavlen renders the text
    without them and shows one thumbnail instead. Stripping after the sanitiser is safe for the same
    reason `_SRCLESS_IMG` is — by then nh3 has normalised the markup, so this never sees author
    input, only tags nh3 itself emitted.
    """
    if not source:
        return mark_safe("")  # nosec — a literal empty string
    # ruff S308 and bandit B308/B703 both flag mark_safe, and both are right to: it is the one
    # dangerous call in this feature, and it is deliberate and reviewed here. What is being marked
    # has just come out of `_clean` — nh3 with the allowlist above — on top of markdown-it running
    # with html=False. This is the single chokepoint every resident-authored body passes through, and
    # concentrating the risk in one reviewed line (and nowhere else) is the design.
    cleaned = _SRCLESS_IMG.sub("", _clean(_demote_h1(_MD.render(source))))
    if not with_images:
        cleaned = _ANY_IMG.sub("", cleaned)
    return mark_safe(cleaned)  # noqa: S308  # nosec


def plain_text(source: str | None) -> str:
    """`source` with all markup removed — for a push body or a meta description.

    Rendered and then stripped rather than regexed, so a lock screen shows readable prose instead of
    `**bold**` markers and `[link](url)` noise.
    """
    if not source:
        return ""
    return nh3.clean(_MD.render(source), tags=set()).strip()


def _image_srcs(source: str) -> list[str]:
    """Every image `src` in `source`, in document order.

    Walks markdown-it's token stream rather than regexing the text, which is what makes it *exact*:
    a URL inside a fenced code block is a code sample, not a reference, and treating it as one is how
    a body-scanning sweep ends up either leaking files forever or deleting a live image.
    """
    srcs: list[str] = []
    for token in _MD.parse(source or ""):
        # Inline content is a nested token stream; images only ever live in there.
        for child in token.children or ():
            if child.type != "image":
                continue
            src = child.attrGet("src")
            if isinstance(src, str):
                srcs.append(src)
    return srcs


def image_sources(source: str) -> list[str]:
    """The images `source` will actually render, in document order.

    Filtered to our own origins, because that is what survives the sanitiser (see
    `_local_images_only`) — so a caller counting these counts what a reader will see, not what the
    author typed. Used by opslagstavlen to decide whether a post has enough pictures to be worth
    collapsing in the feed.
    """
    return [src for src in _image_srcs(source) if src.startswith(_LOCAL_IMAGE_PREFIXES)]


def extract_image_names(source: str) -> set[str]:
    """The storage names of every *uploaded* image `source` embeds.

    Narrower than `image_sources` on purpose: only MEDIA_URL images have a NoticeImage row to claim,
    while a /static/ one is shipped with the app and owned by nobody.

    Returns FileField `name` values (the MEDIA_URL prefix stripped), so callers can match with an
    indexed `file__in=` lookup instead of a `LIKE '%…%'` scan.

    UNQUOTED, because a `name` is what Django stores and a URL is percent-encoded. Storage.url()
    runs the name through `filepath_to_uri`, and `get_valid_filename` — which is what decides the
    stored name — only replaces spaces and strips punctuation: `æøå` are Unicode word characters, so
    they survive into the name and are then encoded in the URL. On a Danish kollegium that is not an
    edge case, it is the default screenshot filename ("Skærmbillede 2026-09-04.png" is stored as
    `Skærmbillede_2026-09-04.png` and serves as `Sk%C3%A6rmbillede_2026-09-04.png`).

    Without the unquote the returned name matches no row, so sync_images never claims the image, the
    row keeps `notice_id IS NULL`, and purge_notices deletes the file a day later while the post that
    embeds it is still on the board — a broken image the author cannot fix by re-uploading, because
    the replacement is orphaned the same way. See tests/test_markdown.py for the round trip.
    """
    prefix = settings.MEDIA_URL
    return {unquote(src[len(prefix) :]) for src in _image_srcs(source) if src.startswith(prefix)}
