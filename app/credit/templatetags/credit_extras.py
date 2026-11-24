import uuid

from django import template

register = template.Library()


@register.filter
def kr(ore: int | None) -> str:
    """Whole øre as Danish kroner: -123456 → '-1.234,56 kr'. None reads as 0."""
    value = int(ore or 0)
    sign = "-" if value < 0 else ""
    whole, cents = divmod(abs(value), 100)
    return f"{sign}{whole:,}".replace(",", ".") + f",{cents:02d} kr"


@register.filter
def hexdigest(value: bytes | memoryview | None) -> str:
    return bytes(value or b"").hex()


@register.simple_tag
def credit_token() -> str:
    """A fresh identity for a send made by a button (credit.ledger's dedupe token)."""
    return str(uuid.uuid4())
