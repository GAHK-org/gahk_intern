from django import template
from django.utils.safestring import SafeString

from search.text import mark

register = template.Library()


@register.filter
def marked(text: str, marks: list[str]) -> SafeString:
    """`text`, escaped, with the search's words in <mark class="search-hit">."""
    return mark(str(text), marks or [])
