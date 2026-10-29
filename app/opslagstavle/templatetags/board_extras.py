"""Template filters for opslagstavlen, and the tag other features use to link into it."""

from collections.abc import Mapping
from typing import Any

from django import template
from django.db.models import Model
from django.utils.safestring import SafeString

from core.markdown import render_markdown
from opslagstavle.access import request_allowed
from opslagstavle.embeds import share_url

register = template.Library()


@register.filter(name="markdown")
def markdown_filter(value: str | None, embeds: Mapping[str, str] | None = None) -> SafeString:
    """Render a notice body. Returns a SafeString, so templates must NOT add `|safe` — see
    core.markdown: that function is the only place resident-authored content is marked safe, and a
    stray `|safe` elsewhere is how the next refactor turns raw source into stored XSS.

    `embeds` are the widgets for what the body embeds (opslagstavle.embeds, `notice.embeds`), each
    drawn where it stands. Without them an embed shows its name."""
    return render_markdown(value, embeds=embeds)


@register.filter(name="markdown_excerpt")
def markdown_excerpt_filter(value: str | None, embeds: Mapping[str, str] | None = None) -> SafeString:
    """Render a notice body for the feed: without the embeds it has widgets for, which the card shows
    under the excerpt, where truncating cannot cut one in half."""
    return render_markdown(value, embeds=embeds, place_embeds=False)


@register.filter(name="markdown_text_only")
def markdown_text_only_filter(value: str | None, embeds: Mapping[str, str] | None = None) -> SafeString:
    """Render a notice body with the images removed, for a feed excerpt.

    A post with several pictures would otherwise fill the whole viewport in the list, pushing every
    other post below the fold. The card shows this plus one thumbnail instead; the detail page still
    renders everything. Its embeds are left out as in `markdown_excerpt`.
    """
    return render_markdown(value, with_images=False, embeds=embeds, place_embeds=False)


@register.simple_tag(takes_context=True)
def board_share_url(context: dict[str, Any], obj: Model) -> str:
    """ "Del på opslagstavlen" for an album or a film: the composer, with it embedded.

    "" for a reader the board's rollout gate keeps out, so the album's and the film's pages never
    offer a button that answers 403.
    """
    request = context.get("request")
    if request is None or not request_allowed(request):
        return ""
    return share_url(obj)
