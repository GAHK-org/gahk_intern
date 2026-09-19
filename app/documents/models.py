import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class DocumentType(models.TextChoices):
    DOCUMENT = "docx", "Word-dokument"
    SPREADSHEET = "xlsx", "Regneark"
    PRESENTATION = "pptx", "Præsentation"


class DocumentSaveType(models.TextChoices):
    INITIAL = "initial", "Oprettet"
    FORCE_SAVE = "force_save", "Løbende gemning"
    FINAL = "final", "Endelig gemning"
    RESTORE = "restore", "Gendannet"


class DocumentSessionState(models.TextChoices):
    ACTIVE = "active", "Aktiv"
    FINISHED = "finished", "Afsluttet"
    FAILED = "failed", "Fejlet"


class Document(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    document_type = models.CharField(max_length=12, choices=DocumentType.choices)
    original_filename = models.CharField(max_length=255)
    extension = models.CharField(max_length=12)
    object_key = models.CharField(max_length=500)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_documents"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    current_version = models.ForeignKey(
        "DocumentVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["owner", "deleted_at", "-updated_at"])]

    def __str__(self) -> str:
        return self.title

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class DocumentGrant(models.Model):
    class Permission(models.TextChoices):
        EDITOR = "editor", "Redaktør"
        VIEWER = "viewer", "Læser"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="grants")
    resident = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="document_grants"
    )
    permission = models.CharField(max_length=10, choices=Permission.choices)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "resident"], name="unique_document_grant")]


class DocumentVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="versions")
    sequence = models.PositiveIntegerField()
    object_key = models.CharField(max_length=500, unique=True)
    content_sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    save_type = models.CharField(max_length=20, choices=DocumentSaveType.choices)
    editing_session = models.ForeignKey(
        "DocumentEditingSession", null=True, blank=True, on_delete=models.SET_NULL, related_name="versions"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sequence"]
        constraints = [
            models.UniqueConstraint(fields=["document", "sequence"], name="unique_document_version_sequence")
        ]


class DocumentEditingSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="editing_sessions")
    document_key = models.CharField(max_length=128, unique=True)
    source_version = models.ForeignKey(
        DocumentVersion, on_delete=models.PROTECT, related_name="source_sessions"
    )
    state = models.CharField(
        max_length=12, choices=DocumentSessionState.choices, default=DocumentSessionState.ACTIVE
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    final_saved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["document"],
                condition=models.Q(state=DocumentSessionState.ACTIVE),
                name="one_active_document_session",
            )
        ]

    def finish(self) -> None:
        self.state = DocumentSessionState.FINISHED
        self.finished_at = timezone.now()


class DocumentCallbackReceipt(models.Model):
    """Idempotency record for an exact ONLYOFFICE callback payload."""

    session = models.ForeignKey(
        DocumentEditingSession, on_delete=models.CASCADE, related_name="callback_receipts"
    )
    payload_sha256 = models.CharField(max_length=64)
    status = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session", "payload_sha256"], name="unique_document_callback_receipt"
            )
        ]
