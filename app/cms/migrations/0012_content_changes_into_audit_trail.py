"""Carry the Hjemmeside's own change log (ContentChange) into the general audit trail.

Each row becomes one event, its object the subject, its {field: [before, after]} the changes. The
event remembers the row it came from (details["content_change"]), which makes running this again —
or forwards after a step back — add nothing twice. Backwards copies those rows back into the table
0013 recreates; events written since stay in the trail only (it is append-only).
"""

from django.db import migrations

KINDS = {
    "page": "cms.page",
    "news": "cms.news",
    "event": "cms.event",
    "image": "cms.image",
    "menu": "cms.menu",
    "redirect": "cms.redirect",
    "settings": "cms.settings",
    "site": "cms.site",
}
VERBS = {
    "create": "created",
    "update": "updated",
    "delete": "deleted",
    "restore": "restored",
    "publish": "published",
    "upload": "uploaded",
    "menu": "updated",
}
OPERATIONS = {"create": "created", "delete": "deleted", "upload": "created", "publish": "involved"}
SYSTEM = "System"


def _pair(value: object) -> list[object]:
    if isinstance(value, list):
        return ([*value, None, None])[:2]
    return [None, value]


def forwards(apps, schema_editor) -> None:  # noqa: ANN001
    ContentChange = apps.get_model("cms", "ContentChange")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    AuditEntity = apps.get_model("audit", "AuditEntity")
    AuditChange = apps.get_model("audit", "AuditChange")
    done = set(
        AuditEvent.objects.filter(details__has_key="content_change").values_list(
            "details__content_change", flat=True
        )
    )
    for row in ContentChange.objects.order_by("id").iterator():
        if row.id in done:
            continue
        kind = KINDS.get(row.kind, f"cms.{row.kind}")
        if row.kind == "site":
            action = "cms.site.published" if row.action == "publish" else "cms.site.update_requested"
        else:
            action = f"{kind}.{VERBS.get(row.action, row.action)}"
        system = row.by_id is None
        details = {"content_change": row.id}
        changes = dict(row.changes or {})
        if "version" in changes:
            details["version"] = _pair(changes.pop("version"))[1]
        if system and row.by_name and row.by_name != SYSTEM:
            details["by"] = row.by_name  # which job it was: "System (HTML renset)"
        event = AuditEvent.objects.create(
            at=row.at,
            action=action,
            actor_id=row.by_id,
            actor_name="" if system else row.by_name,
            source="system" if system else "web",
            correlation_id="",
            details=details,
        )
        entity = AuditEntity.objects.create(
            event=event,
            kind=kind,
            entity_id=row.object_id,
            name=row.label or ("gahk.dk" if row.kind == "site" else ""),
            operation=OPERATIONS.get(row.action, "updated"),
            role="subject",
        )
        AuditChange.objects.bulk_create(
            [
                AuditChange(entity=entity, field=name, before=_pair(value)[0], after=_pair(value)[1])
                for name, value in changes.items()
            ]
        )


def backwards(apps, schema_editor) -> None:  # noqa: ANN001
    ContentChange = apps.get_model("cms", "ContentChange")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    kinds = {kind: short for short, kind in KINDS.items()}
    actions = {verb: short for short, verb in VERBS.items() if short != "menu"}
    for event in AuditEvent.objects.filter(details__has_key="content_change").prefetch_related(
        "entities__changes"
    ):
        entity = event.entities.first()
        if entity is None or ContentChange.objects.filter(pk=event.details["content_change"]).exists():
            continue
        kind = kinds.get(entity.kind, entity.kind.removeprefix("cms."))
        verb = event.action.rsplit(".", 1)[1]
        action = "menu" if kind == "menu" else actions.get(verb, "update" if kind == "site" else verb)
        changes = {c.field: [c.before, c.after] for c in entity.changes.all()}
        if "version" in event.details:
            changes["version"] = [None, event.details["version"]]
        ContentChange.objects.create(
            pk=event.details["content_change"],
            at=event.at,
            by_id=event.actor_id,
            by_name=event.actor_name or event.details.get("by", SYSTEM),
            action=action,
            kind=kind,
            object_id=entity.entity_id,
            label="" if kind == "site" and entity.name == "gahk.dk" else entity.name,
            changes=changes,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
        ("cms", "0011_delete_legacy_image_failure"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
