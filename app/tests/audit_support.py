"""Reading the audit trail in tests."""

from typing import Any

from django.db.models import Model, QuerySet

from audit.models import AuditEntity, AuditEvent


def events(
    prefix: str = "", *, kind: str = "", entity_id: int | None = None, actor: Model | None = None
) -> QuerySet[AuditEvent]:
    """Events whose action starts with `prefix`, oldest first; optionally only those involving one
    thing (`kind`, `entity_id`) or done by one resident."""
    found = AuditEvent.objects.filter(action__startswith=prefix).order_by("id")
    if kind:
        entities = AuditEntity.objects.filter(kind=kind)
        if entity_id is not None:
            entities = entities.filter(entity_id=entity_id)
        found = found.filter(pk__in=entities.values("event_id"))
    if actor is not None:
        found = found.filter(actor_id=actor.pk)
    return found


def actions(prefix: str = "") -> list[str]:
    return list(events(prefix).values_list("action", flat=True))


def changes(event: AuditEvent, kind: str | None = None) -> dict[str, list[Any]]:
    """{field: [before, after]} of the event's subject (or its one entity of `kind`)."""
    entities = event.entities.all()
    entity = entities.get(kind=kind) if kind else entities.get(role="subject")
    return {change.field: [change.before, change.after] for change in entity.changes.all()}
