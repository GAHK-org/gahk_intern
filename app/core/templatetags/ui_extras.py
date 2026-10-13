"""Shared presentational template filters. Colors themselves live in the global CSS (.amount-pos /
.amount-neg); this only maps a signed value to the right semantic class."""

from django import template

register = template.Library()


@register.filter
def sign_class(value: int | float | None) -> str:
    """Semantic CSS class for a signed amount: negative → red, otherwise green (see styles.css)."""
    if value is None:
        return ""
    try:
        return "amount-neg" if int(value) < 0 else "amount-pos"
    except (TypeError, ValueError):
        return ""


@register.filter
def dict_get(mapping: dict[object, object] | None, key: object) -> object:
    """Look up `key` in a dict from the template (`{{ mapping|dict_get:key }}`) — dotted-path lookup
    can't take a variable key."""
    return (mapping or {}).get(key)
