"""Template filters for the room lottery."""

from django import template

from rooms.kvotient import month_label

register = template.Library()


@register.filter
def maaned(index: int | None) -> str:
    """Render an absolute month index (year*12 + month-1) as 'Måned ÅÅÅÅ'; '' when unset."""
    if index is None:
        return ""
    return month_label(index)
