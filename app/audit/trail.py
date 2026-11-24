"""Recording the audit trail.

An operation worth auditing runs inside `event()`:

    with trail.event("cms.page.published", subject=page) as event:
        page.published = True
        page.save()

and everything it writes to a registered model is captured on its own: which rows were created,
changed or deleted, and every field before and after. The event names the meaning ("published"),
who did it (the request's resident unless `actor=` says otherwise), and the subject; signals only
supply the field values. `event.involve(obj, role)` adds a thing that took part without being changed,
`event.details` holds what the event says beyond fields, and `event.change(...)` records what a
signal cannot see (a queryset `.update()`, a `bulk_create`). The rows are written when the block
ends, in the same transaction as the work, so the trail and the data commit or roll back together.

`record()` is the one-line form for an event with no writes of its own (a login, a failed login).

A registered model written OUTSIDE an event is still recorded, as an "unclaimed" event named after
the write ("residents.resident.updated"), so nothing goes unseen — a shell session, the Django admin, a
service someone forgot to wrap. During a request or a worker job that is a bug, and the test settings
(AUDIT_STRICT) turn it into an error so the suite finds every such place. Writes to a model that is
neither registered nor exempted fail the coverage test.

Each app registers its models and names its actions in its own `auditing.py`.
"""

import contextlib
import datetime
import decimal
import ipaddress
import logging
import re
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, cast

from django.conf import settings
from django.db import models, transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.http import HttpRequest
from django.utils import timezone
from django.utils.crypto import salted_hmac

logger = logging.getLogger(__name__)


class UnauditedWrite(Exception):
    """A registered model was written during a request or a job with no audit event open."""


# --- registration ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Registration:
    model: type[models.Model]
    kind: str
    label: str
    name: Callable[[Any], str]
    # Fields whose changes are not worth a line: timestamps, counters, a worker's progress. A save
    # that changes nothing else records nothing.
    ignore: frozenset[str]
    # Fields recorded as "changed" without their values: passwords, tokens.
    redact: frozenset[str]
    # What the audit page calls a field the model has no Danish name for.
    labels: dict[str, str]

    def fields(self) -> list[models.Field]:
        return [
            f
            for f in self.model._meta.concrete_fields
            if not f.primary_key and f.name not in self.ignore and not isinstance(f, models.BinaryField)
        ]

    def m2m_fields(self) -> list[models.ManyToManyField]:
        return [f for f in self.model._meta.many_to_many if f.name not in self.ignore]

    def describe(self, obj: models.Model) -> str:
        try:
            return str(self.name(obj))[:255]
        except Exception:
            return f"#{obj.pk}"


_registry: dict[type[models.Model], Registration] = {}
_by_kind: dict[str, Registration] = {}
_exempt: dict[type[models.Model], str] = {}
_actions: dict[str, str] = {}
_kind_labels: dict[str, str] = {}


def register(
    model: type[models.Model],
    kind: str,
    *,
    name: Callable[[Any], str] = str,
    label: str = "",
    ignore: Iterable[str] = (),
    redact: Iterable[str] = (),
    labels: dict[str, str] | None = None,
) -> None:
    """Audit every write to `model`. `kind` is the stable name its history is kept under."""
    if model in _registry or kind in _by_kind:
        raise ValueError(f"{model.__name__} / {kind} is registered twice")
    registration = Registration(
        model=model,
        kind=kind,
        label=label or str(model._meta.verbose_name),
        name=name,
        ignore=frozenset(ignore),
        redact=frozenset(redact),
        labels=dict(labels or {}),
    )
    _registry[model] = registration
    _by_kind[kind] = registration
    uid = f"audit:{kind}"
    pre_save.connect(_before_save, sender=model, weak=False, dispatch_uid=uid)
    post_save.connect(_after_save, sender=model, weak=False, dispatch_uid=uid)
    post_delete.connect(_after_delete, sender=model, weak=False, dispatch_uid=uid)
    for m2m in registration.m2m_fields():
        through = m2m.remote_field.through
        m2m_changed.connect(_m2m, sender=through, weak=False, dispatch_uid=f"{uid}:{m2m.name}")


def exempt(model: type[models.Model], reason: str) -> None:
    """Leave `model` out of the trail on purpose. The reason is for the reader of auditing.py."""
    _exempt[model] = reason


def actions(labels: dict[str, str]) -> None:
    """Name actions for people: {"cms.page.published": "Udgav siden"}."""
    _actions.update(labels)


def kind_labels(labels: dict[str, str]) -> None:
    """Name kinds that are no single model, recorded with Event.change_kind ("cms.menu": "Menu")."""
    _kind_labels.update(labels)


def registration(model: type[models.Model]) -> Registration | None:
    return _registry.get(model)


def registration_for_kind(kind: str) -> Registration | None:
    return _by_kind.get(kind)


def is_covered(model: type[models.Model]) -> bool:
    return model in _registry or model in _exempt


def action_label(action: str) -> str:
    """What an action is called on the audit page: its own name, or one made from the kind's."""
    if action in _actions:
        return _actions[action]
    kind, _, verb = action.rpartition(".")
    generic = {"created": "Oprettet", "updated": "Ændret", "deleted": "Slettet"}
    reg = _by_kind.get(kind)
    if reg is not None and verb in generic:
        return f"{generic[verb]}: {reg.label}"
    return action


def kind_label(kind: str) -> str:
    reg = _by_kind.get(kind)
    return reg.label if reg else _kind_labels.get(kind, kind)


def known_actions() -> dict[str, str]:
    return dict(_actions)


def actions_called(text: str) -> list[str]:
    """The actions whose name on the audit page contains `text`: "gav rolle" finds the code
    "residents.role.added". Named actions, and the created/changed/deleted of every registered kind."""
    wanted = text.casefold()
    codes = [
        *_actions,
        *(f"{kind}.{verb}" for kind in _by_kind for verb in ("created", "updated", "deleted")),
    ]
    return [code for code in codes if wanted in action_label(code).casefold()]


# --- addresses typed into the login and reset forms ------------------------------------------------

_DOMAIN = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")


def typed_address(typed: str) -> dict[str, str]:
    """What the trail keeps of an address typed into the login or the reset form that matched no
    account: its domain, when it looks like one, and a keyed hash. Never the text itself, since people
    type their password into that box. The hash still ties repeated tries of one address together,
    and searching the audit page for the address finds them (`address_hash`)."""
    address = typed.strip().casefold()
    _, at, domain = address.rpartition("@")
    kept = {"email_hash": address_hash(address)}
    if at and _DOMAIN.fullmatch(domain):
        kept["email_domain"] = domain
    return kept


def address_hash(address: str) -> str:
    """Keyed with SECRET_KEY: a plain hash of an address is undone by hashing guesses."""
    normal = address.strip().casefold()
    return salted_hmac("audit.typed_address", normal, algorithm="sha256").hexdigest()[:16]


def kinds() -> list[str]:
    return list(_by_kind)


# --- context: where a write comes from -------------------------------------------------------------


@dataclass
class Context:
    source: str
    correlation_id: str
    request: HttpRequest | None = None
    path: str = ""


_context: ContextVar[Context | None] = ContextVar("audit_context", default=None)


@contextlib.contextmanager
def context(source: str, *, correlation_id: str = "", request: HttpRequest | None = None) -> Iterator[None]:
    """Mark everything inside as coming from one request or one job (audit.middleware, audit.apps)."""
    token = _context.set(
        Context(
            source=source,
            correlation_id=correlation_id or uuid.uuid4().hex,
            request=request,
            path=request.path if request is not None else "",
        )
    )
    try:
        yield
    finally:
        _context.reset(token)


def client_ip(request: HttpRequest) -> str | None:
    """The client's address. Behind Coolify/Traefik REMOTE_ADDR is the proxy, so the last hop of
    X-Forwarded-For is the one Traefik appended (as oelkaelder.views reads the till's)."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip = forwarded.split(",")[-1].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def _request_actor(ctx: Context | None) -> Any:  # noqa: ANN401 - a Resident, or None
    if ctx is None or ctx.request is None:
        return None
    user = getattr(ctx.request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return user


# --- the event being recorded ----------------------------------------------------------------------


@dataclass
class _Change:
    before: Any
    after: Any
    redacted: bool


@dataclass
class _Entity:
    kind: str
    entity_id: int | None
    name: str
    operation: str
    role: str
    changes: dict[str, _Change] = field(default_factory=dict)


_UNSET: Any = object()


class Event:
    """An audit event while its block runs. Its rows are written when the block ends."""

    def __init__(self, action: str, actor: Any, details: dict[str, Any]) -> None:  # noqa: ANN401
        self.action = action
        self.actor = actor
        self.details = details
        self.cancelled = False
        self._entities: dict[tuple[str, Any], _Entity] = {}

    # -- what the event involves

    def _entity(self, obj: models.Model, operation: str) -> _Entity | None:
        reg = registration(type(obj))
        if reg is None:
            return None
        key = (reg.kind, obj.pk)
        entity = self._entities.get(key)
        if entity is None:
            entity = _Entity(reg.kind, obj.pk, reg.describe(obj), operation, "")
            self._entities[key] = entity
        else:
            entity.name = reg.describe(obj) or entity.name
            if entity.operation == "involved" or operation == "deleted":
                entity.operation = operation
            elif entity.operation == "deleted" and operation == "created":
                entity.operation = "updated"  # deleted and made again: a replacement
        return entity

    def involve(self, obj: models.Model | None, role: str = "") -> None:
        """Add something that took part without being changed (or name the role of one that was)."""
        if obj is None:
            return
        entity = self._entity(obj, "involved")
        if entity is not None and role:
            entity.role = role

    def involve_kind(self, kind: str, entity_id: int | None, name: str, role: str = "") -> None:
        """Add something that has no registered model, or no row any more (an address, a resident
        already deleted)."""
        key = (kind, entity_id if entity_id is not None else name)
        self._entities.setdefault(key, _Entity(kind, entity_id, name[:255], "involved", role))

    def change(self, obj: models.Model, field_name: str, before: Any, after: Any) -> None:  # noqa: ANN401
        """Record a change no signal saw: a queryset update, a bulk write."""
        reg = registration(type(obj))
        entity = self._entity(obj, "updated")
        if entity is None or reg is None:
            return
        if entity.operation == "involved":
            entity.operation = "updated"
        redacted = field_name in reg.redact
        _merge_change(entity, field_name, _Change(_json(before), _json(after), redacted))

    def change_kind(
        self,
        kind: str,
        entity_id: int | None,
        name: str,
        field_name: str,
        before: Any,  # noqa: ANN401
        after: Any,  # noqa: ANN401
    ) -> None:
        """Record a change of something that is no single row: the whole menu, a list of settings."""
        key = (kind, entity_id if entity_id is not None else name)
        entity = self._entities.get(key)
        if entity is None:
            entity = self._entities[key] = _Entity(kind, entity_id, name[:255], "updated", "")
        _merge_change(entity, field_name, _Change(_json(before), _json(after), redacted=False))

    def cancel(self) -> None:
        """Write nothing for this block (it turned out not to change anything)."""
        self.cancelled = True

    @property
    def has_changes(self) -> bool:
        return any(e.changes or e.operation in ("created", "deleted") for e in self._entities.values())

    # -- writing

    def _write(self, ctx: Context | None, *, unclaimed: bool = False) -> None:
        from .models import AuditChange, AuditEntity, AuditEvent

        actor = self.actor
        source = ctx.source if ctx else AuditEvent.Source.SYSTEM
        row = AuditEvent(
            at=_backdated.get() or timezone.now(),
            action=self.action[:100],
            actor_id=getattr(actor, "pk", None),
            actor_name=_person_name(actor),
            source=source,
            correlation_id=ctx.correlation_id if ctx else "",
            ip=client_ip(ctx.request) if ctx and ctx.request is not None else None,
            details=_json(self.details) or {},
            unclaimed=unclaimed,
        )
        row.save()
        entities = list(self._entities.values())
        saved = AuditEntity.objects.bulk_create(
            [
                AuditEntity(
                    event=row,
                    kind=e.kind,
                    entity_id=e.entity_id if isinstance(e.entity_id, int) else None,
                    name=e.name,
                    operation=e.operation,
                    role=e.role,
                )
                for e in entities
            ]
        )
        AuditChange.objects.bulk_create(
            [
                AuditChange(
                    entity=saved_entity,
                    field=name,
                    before=None if change.redacted else change.before,
                    after=None if change.redacted else change.after,
                    redacted=change.redacted,
                )
                for saved_entity, e in zip(saved, entities, strict=True)
                for name, change in e.changes.items()
            ]
        )


def _merge_change(entity: _Entity, name: str, change: _Change) -> None:
    """Two writes of one field in one event make one change: from the first before to the last after."""
    earlier = entity.changes.get(name)
    if earlier is not None:
        change = _Change(earlier.before, change.after, change.redacted)
    if change.before == change.after and not change.redacted and entity.operation == "updated":
        entity.changes.pop(name, None)
    else:
        entity.changes[name] = change


_open: ContextVar[Event | None] = ContextVar("audit_open_event", default=None)


@contextlib.contextmanager
def event(
    action: str,
    *,
    subject: models.Model | None = None,
    actor: Any = _UNSET,  # noqa: ANN401 - a Resident or None (the system); default: the request's
    details: dict[str, Any] | None = None,
    involve: Iterable[tuple[models.Model | None, str]] = (),
    only_if_changed: bool = False,
) -> Iterator[Event]:
    """Run an audited operation. See the module docstring.

    `only_if_changed` writes nothing when the block created, changed and deleted nothing (a form
    saved as it was)."""
    ctx = _context.get()
    # Opened inside unaudited(): the block runs as usual, and nothing of it is written.
    suppressed = _suppressed.get()
    recorded = Event(action, _request_actor(ctx) if actor is _UNSET else actor, dict(details or {}))
    # A subject made inside the block has no id yet: it is named when the block ends.
    unsaved_subject = subject is not None and subject.pk is None
    if subject is not None and not unsaved_subject:
        recorded.involve(subject, "subject")
    for obj, role in involve:
        recorded.involve(obj, role)
    with transaction.atomic():
        token = _open.set(recorded)
        try:
            yield recorded
        finally:
            _open.reset(token)
        if unsaved_subject and subject is not None and subject.pk is not None:
            recorded.involve(subject, "subject")
        if suppressed:
            return
        if not recorded.cancelled and not (only_if_changed and not recorded.has_changes):
            recorded._write(ctx)


def record(
    action: str,
    *,
    subject: models.Model | None = None,
    actor: Any = _UNSET,  # noqa: ANN401
    details: dict[str, Any] | None = None,
    involve: Iterable[tuple[models.Model | None, str]] = (),
) -> None:
    """An event with nothing to capture: a login, a failed login, a denied request."""
    with event(action, subject=subject, actor=actor, details=details, involve=involve):
        pass


def current() -> Event | None:
    """The event being recorded, for code deep inside one that wants to add details."""
    return _open.get()


@contextlib.contextmanager
def unaudited() -> Iterator[None]:
    """Nothing inside is recorded, neither captured writes nor named events (a service an import or
    the demo seed calls opens its own): loading old data (ETL), seeding a demo database. Never for
    anything a person does."""
    token = _suppressed.set(True)
    try:
        yield
    finally:
        _suppressed.reset(token)


_suppressed: ContextVar[bool] = ContextVar("audit_suppressed", default=False)


@contextlib.contextmanager
def backdated(at: datetime.datetime) -> Iterator[None]:
    """Events written inside are dated `at` rather than now: the made-up history of a demo database
    (audit.demo). Never for anything a person or a job does — those happen when they are recorded."""
    token = _backdated.set(at)
    try:
        yield
    finally:
        _backdated.reset(token)


_backdated: ContextVar[datetime.datetime | None] = ContextVar("audit_backdated", default=None)


# --- capture ---------------------------------------------------------------------------------------


_BEFORE = "_audit_before"


def _active() -> bool:
    """Whether a write now would be recorded somewhere: into an open event, or as an unclaimed one."""
    if _suppressed.get():
        return False
    return _open.get() is not None or _context.get() is not None or getattr(settings, "AUDIT_FALLBACK", True)


def _before_save(
    sender: type[models.Model], instance: models.Model, raw: bool = False, **kwargs: object
) -> None:
    if raw or not _active() or instance._state.adding or instance.pk is None:
        return
    reg = _registry[sender]
    names = [f.attname for f in reg.fields()]
    update_fields = kwargs.get("update_fields")
    if update_fields is not None:
        wanted = set(cast("Iterable[str]", update_fields))
        names = [f.attname for f in reg.fields() if f.name in wanted or f.attname in wanted]
        if not names:
            setattr(instance, _BEFORE, None)
            return
    setattr(instance, _BEFORE, sender._base_manager.filter(pk=instance.pk).values(*names).first())


def _after_save(
    sender: type[models.Model], instance: models.Model, created: bool, raw: bool = False, **kwargs: object
) -> None:
    if raw or not _active():
        return
    reg = _registry[sender]
    if created:
        changes = {
            f.name: _Change(None, _field_json(f, instance), f.name in reg.redact)
            for f in reg.fields()
            if not _empty(f.value_from_object(instance))
        }
        _capture(reg, instance, "created", changes)
        return
    before = instance.__dict__.pop(_BEFORE, None)
    if before is None:
        return  # only ignored fields were saved, or the row was not there to read
    changes = {}
    for f in reg.fields():
        if f.attname not in before:
            continue
        old, new = _json(before[f.attname]), _field_json(f, instance)
        if old != new:
            changes[f.name] = _Change(old, new, f.name in reg.redact)
    if changes:
        _capture(reg, instance, "updated", changes)


def _after_delete(sender: type[models.Model], instance: models.Model, **_: Any) -> None:  # noqa: ANN401
    if not _active():
        return
    reg = _registry[sender]
    changes = {
        f.name: _Change(_field_json(f, instance), None, f.name in reg.redact)
        for f in reg.fields()
        if not _empty(f.value_from_object(instance))
    }
    _capture(reg, instance, "deleted", changes)


def _m2m(
    sender: type[models.Model],
    instance: models.Model,
    action: str,
    reverse: bool,
    model: type[models.Model],
    pk_set: set[Any] | None,
    **_: object,
) -> None:
    if (
        reverse
        or not _active()
        or action not in ("pre_add", "pre_remove", "pre_clear", "post_add", "post_remove", "post_clear")
    ):
        return
    reg = registration(type(instance))
    if reg is None:
        return
    m2m = next((f for f in reg.m2m_fields() if f.remote_field.through is sender), None)
    if m2m is None:
        return
    current_ids = sorted(getattr(instance, m2m.name).values_list("pk", flat=True))
    if action.startswith("pre_"):
        setattr(instance, f"{_BEFORE}_{m2m.name}", current_ids)
        return
    before = instance.__dict__.pop(f"{_BEFORE}_{m2m.name}", None)
    if before is None or before == current_ids:
        return
    _capture(reg, instance, "updated", {m2m.name: _Change(before, current_ids, m2m.name in reg.redact)})


def _capture(reg: Registration, instance: models.Model, operation: str, changes: dict[str, _Change]) -> None:
    open_event = _open.get()
    if open_event is not None:
        entity = open_event._entity(instance, operation)
        if entity is None:
            return
        if entity.operation == "involved":
            entity.operation = operation
        for name, change in changes.items():
            _merge_change(entity, name, change)
        return
    _unclaimed(reg, instance, operation, changes)


def _unclaimed(
    reg: Registration, instance: models.Model, operation: str, changes: dict[str, _Change]
) -> None:
    """A registered write outside any event. Recorded on its own, and an error in tests when a request
    or a job made it: some view or task is missing its `event()`."""
    ctx = _context.get()
    action = f"{reg.kind}.{operation}"
    if ctx is not None and not any(
        ctx.path.startswith(p) for p in getattr(settings, "AUDIT_UNCLAIMED_PATHS", ())
    ):
        where = ctx.path or f"a {ctx.source} job"
        if getattr(settings, "AUDIT_STRICT", False):
            raise UnauditedWrite(f"{action} (#{instance.pk}) written by {where} outside trail.event()")
        logger.warning("Unclaimed audit write: %s #%s by %s", action, instance.pk, where)
    elif ctx is None and not getattr(settings, "AUDIT_FALLBACK", True):
        return
    recorded = Event(action, _request_actor(ctx), {})
    entity = recorded._entity(instance, operation)
    if entity is not None:
        entity.role = "subject"
        entity.changes = changes
    recorded._write(ctx, unclaimed=True)


# --- values ----------------------------------------------------------------------------------------


def _empty(value: Any) -> bool:  # noqa: ANN401
    return value is None or value in ("", [], {})


def _field_json(f: models.Field, instance: models.Model) -> Any:  # noqa: ANN401
    return _json(f.value_from_object(instance))


def _json(value: Any) -> Any:  # noqa: ANN401
    """A value as the trail stores it: JSON, readable without the model that held it."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime.datetime):
        # In UTC: the same moment read back from the database and set by code in Copenhagen time
        # must compare equal, or every save would "change" it.
        return (value.astimezone(datetime.UTC) if timezone.is_aware(value) else value).isoformat()
    if isinstance(value, datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return format(value.normalize(), "f")  # 10 and 10.00 are one amount
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json(v) for v in value]
    if isinstance(value, models.Model):
        return value.pk
    if hasattr(value, "name") and hasattr(value, "storage"):  # a FieldFile: its name, as the column holds it
        return value.name
    if isinstance(value, bytes | memoryview):
        return None
    return str(value)


def _person_name(person: Any) -> str:  # noqa: ANN401
    if person is None:
        return ""
    name = getattr(person, "full_name", "") or str(person)
    return str(name)[:255]
