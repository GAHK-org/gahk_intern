"""The general audit trail: what happened, which things it involved, and what changed in them.

Three tables, append-only. A row is never changed or deleted once written: the database refuses an
UPDATE or DELETE on them (the trigger in migration 0001), and the models refuse it before it gets
there. Nothing points at them with a cascade, and they point at nothing with a constraint: deleting a
resident, a page or a video leaves its history exactly as it was, readable through the names
snapshotted when it was written.

Rows are written by audit.trail only — never by hand.
"""

from typing import Any, NoReturn

from django.conf import settings
from django.db import models
from django.utils import timezone


class AppendOnlyError(Exception):
    """An audit row was about to be changed or deleted."""


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs: Any) -> NoReturn:  # noqa: ANN401
        raise AppendOnlyError("Audit rows are never changed.")

    def delete(self) -> NoReturn:
        raise AppendOnlyError("Audit rows are never deleted.")


class AppendOnly(models.Model):
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        if not self._state.adding:
            raise AppendOnlyError("Audit rows are never changed.")
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:  # noqa: ANN401
        raise AppendOnlyError("Audit rows are never deleted.")


class AuditEvent(AppendOnly):
    """One thing that happened: who did it, when, from where, and what it was."""

    class Source(models.TextChoices):
        WEB = "web", "Web"
        WORKER = "worker", "Worker"
        SYSTEM = "system", "System"  # a management command, the shell, a data migration
        LEGACY = "legacy", "Gammelt system"  # carried over from a trail kept before this one

    at = models.DateTimeField(default=timezone.now, db_index=True)
    # A dotted name, "<app>.<thing>.<what happened>": "cms.page.updated", "auth.login.failed".
    action = models.CharField(max_length=100, db_index=True)
    # No constraint and no cascade: a resident deleted later leaves the id and `actor_name` behind.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="+",
    )
    actor_name = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices)
    # Every event of one request or one worker job shares it.
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    # What the event says beyond field changes: the address a failed login used, a reason, a count.
    details = models.JSONField(default=dict, blank=True)
    # Written by the fallback for a change no audited operation claimed (see audit.trail).
    unclaimed = models.BooleanField(default=False)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["actor", "-at"], name="audit_event_actor_at")]

    def __str__(self) -> str:
        return f"{self.at:%Y-%m-%d %H:%M} {self.actor_name or 'System'}: {self.action}"


class AuditEntity(AppendOnly):
    """One thing an event involved, as it was named then."""

    class Operation(models.TextChoices):
        CREATED = "created", "Oprettet"
        UPDATED = "updated", "Ændret"
        DELETED = "deleted", "Slettet"
        INVOLVED = "involved", "Berørt"  # part of the event without being changed

    event = models.ForeignKey(AuditEvent, on_delete=models.DO_NOTHING, related_name="entities")
    # A stable name chosen at registration ("cms.page"), never derived from the class: a model renamed
    # or removed later keeps its history readable.
    kind = models.CharField(max_length=60)
    entity_id = models.BigIntegerField(null=True, blank=True)
    name = models.CharField(max_length=255, blank=True)
    operation = models.CharField(max_length=10, choices=Operation.choices)
    # How it took part: "subject" for what the event is about, otherwise free ("recipient", "folder").
    role = models.CharField(max_length=30, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["kind", "entity_id"], name="audit_entity_kind_id")]

    def __str__(self) -> str:
        return f"{self.kind}#{self.entity_id} {self.name}"


class AuditChange(AppendOnly):
    """One field of one entity, before and after. A redacted field records that it changed, not what
    it held (a password, a token)."""

    entity = models.ForeignKey(AuditEntity, on_delete=models.DO_NOTHING, related_name="changes")
    field = models.CharField(max_length=100)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    redacted = models.BooleanField(default=False)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.field}: {self.before!r} → {self.after!r}"
