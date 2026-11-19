"""Reading the audit trail back: events as people read them, for the audit page and for the pages of
features that show their own part of it (the Hjemmeside's history, Drive's activity).

Values are shown as stored, turned into words where the model still knows how: a field's name, a
choice's label, the name of a related row (or "#id" once it is gone). The trail itself never
depends on that — it only makes it easier to read.
"""

import datetime
import json
from dataclasses import dataclass, field
from typing import Any

from django.db import models
from django.db.models import Prefetch, Q, QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.formats import date_format
from django.utils.text import capfirst

from . import trail
from .models import AuditChange, AuditEntity, AuditEvent

PAGE_SIZE = 50
LONG_TEXT = 160

# The first part of an action, as the filter names it.
AREAS = {
    "auth": "Login og adgang",
    "residents": "Beboere og roller",
    "rooms": "Værelser",
    "regnskab": "Regnskab",
    "oelkaelder": "Ølkælderen",
    "cms": "Hjemmeside",
    "gahkflix": "GAHKFlix",
    "photo_album": "Fotoalbum",
    "drive": "Drev",
    "den_hurtige": "Den Hurtige",
    "opslagstavle": "Opslagstavlen",
    "events": "Begivenheder",
    "notifications": "Notifikationer",
    "admissions": "Optagelse",
    "ak": "AK",
    "reparationer": "Reparationer",
    "core": "Fælles",
    "stats": "Statistik",
    "scheduler": "Planlagte opgaver",
}


# What the audit page calls the keys events put in their details.
DETAIL_LABELS = {
    "reason": "Årsag",
    "email": "E-mail",
    "accounts": "Konti",
    "path": "Side",
    "roles": "Roller",
    "role": "Rolle",
    "period": "Måned",
    "account": "Konto",
    "account_name": "Navn i e-conomic",
}


@dataclass(frozen=True)
class Filters:
    kind: str = ""
    entity_id: int | None = None
    kind_prefix: str = ""  # a feature's own page: "cms."
    actor: int | None = None
    area: str = ""
    source: str = ""
    q: str = ""
    unclaimed: bool = False
    since: datetime.date | None = None
    until: datetime.date | None = None

    @classmethod
    def from_query(cls, query: Any) -> "Filters":  # noqa: ANN401 - a QueryDict
        def number(name: str) -> int | None:
            raw = query.get(name, "")
            return int(raw) if raw.isdecimal() else None

        return cls(
            kind=query.get("kind", "")[:60],
            entity_id=number("id"),
            actor=number("actor"),
            area=query.get("area", "")[:40],
            source=query.get("source", "") if query.get("source", "") in AuditEvent.Source.values else "",
            q=query.get("q", "").strip()[:100],
            unclaimed=query.get("unclaimed") == "1",
            since=parse_date(query.get("since", "")) if query.get("since") else None,
            until=parse_date(query.get("until", "")) if query.get("until") else None,
        )

    @property
    def any(self) -> bool:
        return bool(
            self.kind
            or self.actor
            or self.area
            or self.source
            or self.q
            or self.unclaimed
            or self.since
            or self.until
        )

    def queryset(self) -> QuerySet[AuditEvent]:
        events = AuditEvent.objects.all()
        if self.kind:
            entities = AuditEntity.objects.filter(kind=self.kind)
            if self.entity_id is not None:
                entities = entities.filter(entity_id=self.entity_id)
            events = events.filter(pk__in=entities.values("event_id"))
        if self.kind_prefix:
            events = events.filter(
                pk__in=AuditEntity.objects.filter(kind__startswith=self.kind_prefix).values("event_id")
            )
        if self.actor is not None:
            events = events.filter(actor_id=self.actor)
        if self.area:
            events = events.filter(action__startswith=f"{self.area}.")
        if self.source:
            events = events.filter(source=self.source)
        if self.unclaimed:
            events = events.filter(unclaimed=True)
        if self.since:
            events = events.filter(at__date__gte=self.since)
        if self.until:
            events = events.filter(at__date__lte=self.until)
        if self.q:
            named = AuditEntity.objects.filter(name__icontains=self.q).values("event_id")
            events = events.filter(
                Q(actor_name__icontains=self.q) | Q(action__icontains=self.q) | Q(pk__in=named)
            )
        return events


@dataclass
class ChangeLine:
    field: str
    label: str
    before: str
    after: str
    redacted: bool
    before_long: bool
    after_long: bool


@dataclass
class EntityLine:
    kind: str
    kind_label: str
    entity_id: int | None
    name: str
    operation: str
    operation_label: str
    role: str
    changes: list[ChangeLine] = field(default_factory=list)


@dataclass
class EventLine:
    id: int
    at: datetime.datetime
    action: str
    label: str
    actor_id: int | None
    actor_name: str
    source: str
    source_label: str
    ip: str | None
    unclaimed: bool
    details: list[tuple[str, str]]
    subject: EntityLine | None
    entities: list[EntityLine]

    @property
    def shown_subject(self) -> EntityLine | None:
        """The subject, unless it is the actor themself ("Anna loggede ind", not "… ind Anna")."""
        subject = self.subject
        if subject and subject.kind == "residents.resident" and subject.entity_id == self.actor_id:
            return None
        return subject

    @property
    def has_more(self) -> bool:
        """Whether unfolding shows anything beyond the line: details, an address, a change, or
        something other than the subject itself."""
        return bool(
            self.details
            or self.ip
            or any(e.changes or e.operation != "involved" or e is not self.subject for e in self.entities)
        )


def page(
    filters: Filters, *, before: int | None = None, size: int = PAGE_SIZE
) -> tuple[list[EventLine], int | None]:
    """One page of events, newest first, and the id to pass as `before` for the next (None: no more).
    By id rather than offset: new events arriving while somebody pages back do not shift the pages."""
    events = filters.queryset().order_by("-id")
    if before is not None:
        events = events.filter(id__lt=before)
    rows = list(
        events.prefetch_related(
            Prefetch(
                "entities",
                queryset=AuditEntity.objects.prefetch_related(
                    Prefetch("changes", queryset=AuditChange.objects.order_by("id"))
                ).order_by("id"),
            )
        )[: size + 1]
    )
    more = len(rows) > size
    rows = rows[:size]
    names = _RelatedNames()
    return [_event_line(row, names) for row in rows], (rows[-1].id if more and rows else None)


def latest_name(kind: str, entity_id: int | None) -> str:
    """What a thing was last called in the trail: the heading of its history."""
    entity = AuditEntity.objects.filter(kind=kind, entity_id=entity_id).order_by("-id").first()
    return entity.name if entity else f"#{entity_id}"


def areas() -> list[tuple[str, str]]:
    seen = {action.split(".", 1)[0] for action in trail.known_actions()}
    seen |= {kind.split(".", 1)[0] for kind in trail.kinds()}
    return sorted(((a, AREAS.get(a, a)) for a in seen), key=lambda pair: pair[1])


# --- turning rows into lines -----------------------------------------------------------------------


def _event_line(row: AuditEvent, names: "_RelatedNames") -> EventLine:
    entities = [_entity_line(entity, names) for entity in row.entities.all()]
    subject = next((e for e in entities if e.role == "subject"), entities[0] if len(entities) == 1 else None)
    return EventLine(
        id=row.pk,
        at=row.at,
        action=row.action,
        label=trail.action_label(row.action),
        actor_id=row.actor_id,
        actor_name=row.actor_name,
        source=row.source,
        source_label=row.get_source_display(),
        ip=row.ip,
        unclaimed=row.unclaimed,
        details=[
            (DETAIL_LABELS.get(str(key), str(key)), _plain(value))
            for key, value in (row.details or {}).items()
        ],
        subject=subject,
        entities=entities,
    )


def _entity_line(entity: AuditEntity, names: "_RelatedNames") -> EntityLine:
    return EntityLine(
        kind=entity.kind,
        kind_label=trail.kind_label(entity.kind),
        entity_id=entity.entity_id,
        name=entity.name,
        operation=entity.operation,
        operation_label=entity.get_operation_display(),
        role=entity.role,
        changes=[_change_line(entity.kind, change, names) for change in entity.changes.all()],
    )


class Values:
    """Field names and values as the audit page shows them, for a feature's own page of the trail."""

    def __init__(self) -> None:
        self._names = _RelatedNames()

    def label(self, kind: str, field_name: str) -> str:
        return _field_label(kind, field_name)

    def text(self, kind: str, field_name: str, value: Any) -> str:  # noqa: ANN401
        return _value(_model_field(trail.registration_for_kind(kind), field_name), value, self._names)


def _field_label(kind: str, name: str) -> str:
    reg = trail.registration_for_kind(kind)
    if reg is None and name == kind.rsplit(".", 1)[-1]:
        return trail.kind_label(kind)  # a kind with no model, changed as a whole: "Menu"
    if reg is not None and name in reg.labels:
        return reg.labels[name]
    model_field = _model_field(reg, name)
    return capfirst(str(model_field.verbose_name)) if model_field is not None else name


def _change_line(kind: str, change: AuditChange, names: "_RelatedNames") -> ChangeLine:
    model_field = _model_field(trail.registration_for_kind(kind), change.field)
    label = _field_label(kind, change.field)
    if change.redacted:
        return ChangeLine(change.field, label, "", "", redacted=True, before_long=False, after_long=False)
    before = _value(model_field, change.before, names)
    after = _value(model_field, change.after, names)
    return ChangeLine(
        change.field,
        label,
        before,
        after,
        redacted=False,
        before_long=len(before) > LONG_TEXT,
        after_long=len(after) > LONG_TEXT,
    )


def _model_field(reg: trail.Registration | None, name: str) -> models.Field | None:
    if reg is None:
        return None
    try:
        found = reg.model._meta.get_field(name)
    except Exception:
        return None
    return found if isinstance(found, models.Field) else None


def _value(model_field: models.Field | None, value: Any, names: "_RelatedNames") -> str:  # noqa: ANN401
    if value is None or value == "":
        return "—"
    if model_field is None:
        return _plain(value)
    if model_field.is_relation and model_field.related_model is not None:
        if isinstance(value, list):
            return ", ".join(names.get(model_field.related_model, pk) for pk in value) or "—"
        return names.get(model_field.related_model, value)
    if model_field.choices:
        return str(dict(model_field.flatchoices).get(value, value))
    if isinstance(model_field, models.BooleanField):
        return "ja" if value else "nej"
    if isinstance(model_field, models.DateTimeField) and isinstance(value, str):
        moment = parse_datetime(value)
        if moment is not None:
            return date_format(timezone.localtime(moment), "j. b Y H:i")
    if isinstance(model_field, models.DateField) and isinstance(value, str):
        day = parse_date(value)
        if day is not None:
            return date_format(day, "j. b Y")
    return _plain(value)


def _plain(value: Any) -> str:  # noqa: ANN401
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "ja" if value else "nej"
    if isinstance(value, list) and all(isinstance(v, str | int) for v in value):
        return ", ".join(str(v) for v in value) or "—"
    if isinstance(value, list) and all(isinstance(v, dict) for v in value):
        # A list of entries (a menu): one per line, its values side by side.
        return "\n".join(" ".join(str(part) for part in entry.values()) for entry in value) or "—"
    return json.dumps(value, ensure_ascii=False)


class _RelatedNames:
    """Names of related rows for one page, looked up once each. A row since deleted is "#id"."""

    def __init__(self) -> None:
        self._cache: dict[tuple[type[models.Model], Any], str] = {}

    def get(self, model: Any, pk: Any) -> str:  # noqa: ANN401
        key = (model, pk)
        if key not in self._cache:
            obj = model._base_manager.filter(pk=pk).first()
            reg = trail.registration(model)
            if obj is None:
                self._cache[key] = f"#{pk}"
            else:
                self._cache[key] = reg.describe(obj) if reg else str(obj)
        return self._cache[key]
