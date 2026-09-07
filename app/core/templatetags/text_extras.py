"""Template filters for resident-authored PLAIN text — the surfaces that are deliberately not
Markdown: Den Hurtige's messages and thread replies, and the opslagstavle and begivenheder comment
threads.

The Markdown surfaces do not need a filter of their own; `|markdown` already routes through
core.markdown, which finds and shortens its links with the same rules. See core.links.
"""

from django import template
from django.utils.safestring import SafeString

from core.links import linkify

register = template.Library()


@register.filter(name="links")
def links_filter(value: str | None) -> SafeString:
    """Escape `value` and turn every URL in it into a shortened link.

    Returns a SafeString, so templates must NOT add `|safe` and must NOT chain another filter that
    escapes again — `|links|linebreaksbr` is fine, `|escape|links` is not. The escaping happens
    inside core.links.linkify, before any tag is added, which is what makes the result safe; that
    docstring is where the argument lives.

    Replaces `|urlize` on the two comment threads. Same idea, three differences that matter: it
    shortens, it uses the same URL scanner as the Markdown bodies these comments sit under, and it
    carries `rel="noopener noreferrer nofollow"` — which urlize does not, so the kollegium was
    passing its search-engine standing to whatever anybody pasted.
    """
    return linkify(value)
