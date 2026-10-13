"""The photo album's piece of the dashboard (residents/dashboard.html)."""

from typing import Any

from django import template

from core.clock import current_date
from photo_album.featured import featured_for, on_this_day

register = template.Library()


@register.inclusion_tag("photo_album/_dashboard_strip.html")
def photo_album_strip() -> dict[str, Any]:
    day = current_date()
    return {"featured": featured_for(day), "on_this_day": on_this_day(day)}
