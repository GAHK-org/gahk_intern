"""The photo album's piece of the dashboard (residents/dashboard.html)."""

from typing import Any

from django import template

from core.clock import current_date
from photo_album import access
from photo_album.featured import featured_for, on_this_day
from residents.api import effective_roles

register = template.Library()


@register.inclusion_tag("photo_album/_dashboard_strip.html", takes_context=True)
def photo_album_strip(context: template.Context) -> dict[str, Any]:
    """Behind the same rollout gate as the sidebar's Fotoalbum entry (core.context_processors), so
    re-gating the album hides its photos from the dashboard too rather than only the link to them."""
    if not access.roles_allowed(effective_roles(context["request"])):
        return {"featured": [], "on_this_day": []}
    day = current_date()
    return {"featured": featured_for(day), "on_this_day": on_this_day(day)}
