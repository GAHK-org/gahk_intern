from django import template

from drive import preview

register = template.Library()


@register.filter
def drive_icon(name: str) -> str:
    """The sprite symbol for a file's kind (drive/preview.py): `#i-{{ file.name|drive_icon }}`."""
    return preview.icon(name)
