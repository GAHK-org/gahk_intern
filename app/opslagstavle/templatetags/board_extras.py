"""Template filters for opslagstavlen."""

from django import template
from django.utils.safestring import SafeString

from core.markdown import render_markdown

register = template.Library()


@register.filter(name="markdown")
def markdown_filter(value: str | None) -> SafeString:
    """Render a notice body. Returns a SafeString, so templates must NOT add `|safe` — see
    core.markdown: that function is the only place resident-authored content is marked safe, and a
    stray `|safe` elsewhere is how the next refactor turns raw source into stored XSS."""
    return render_markdown(value)


@register.filter(name="markdown_text_only")
def markdown_text_only_filter(value: str | None) -> SafeString:
    """Render a notice body with the images removed, for a feed excerpt.

    A post with several pictures would otherwise fill the whole viewport in the list, pushing every
    other post below the fold. The card shows this plus one thumbnail instead; the detail page still
    renders everything.
    """
    return render_markdown(value, with_images=False)
