"""The audit page: everything in the trail, for administrators (/admin/audit)."""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from . import presentation, trail
from .models import AuditEvent


def audit_trail(request: HttpRequest) -> HttpResponse:
    filters = presentation.Filters.from_query(request.GET)
    raw_before = request.GET.get("before", "")
    events, older = presentation.page(filters, before=int(raw_before) if raw_before.isdecimal() else None)
    heading = None
    if filters.kind:
        heading = {
            "kind": trail.kind_label(filters.kind),
            "name": presentation.latest_name(filters.kind, filters.entity_id) if filters.entity_id else "",
        }
    actor_name = ""
    if filters.actor is not None:
        actor_name = (
            AuditEvent.objects.filter(actor_id=filters.actor)
            .exclude(actor_name="")
            .values_list("actor_name", flat=True)
            .first()
            or f"#{filters.actor}"
        )
    return render(
        request,
        "audit/audit_trail.html",
        {
            "events": events,
            "older": older,
            "filters": filters,
            "areas": presentation.areas(),
            "sources": AuditEvent.Source.choices,
            "heading": heading,
            "actor_name": actor_name,
            "link_entities": True,
        },
    )
