import uuid

from django import template

register = template.Library()


@register.filter
def kr(ore: int | None) -> str:
    """Format integer øre as Danish kroner, e.g. 1250 -> '12,50 kr'."""
    if ore is None:
        return ""
    try:
        ore = int(ore)
    except (TypeError, ValueError):
        return ""
    return f"{ore / 100:.2f} kr".replace(".", ",")


@register.filter
def kr_value(ore: int | None) -> str:
    """Bare kroner value for a number input (dot decimal), e.g. 1250 -> '12.50'; '' when unset."""
    if ore is None:
        return ""
    try:
        return f"{int(ore) / 100:.2f}"
    except (TypeError, ValueError):
        return ""


@register.simple_tag
def new_token() -> str:
    """A fresh send token for a form that moves money: the same form posted twice is one send."""
    return str(uuid.uuid4())
