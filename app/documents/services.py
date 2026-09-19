import base64
import hashlib
import hmac
import json
import re
import secrets
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from io import BytesIO
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone

from residents.models import Resident

from .models import (
    Document,
    DocumentCallbackReceipt,
    DocumentEditingSession,
    DocumentSaveType,
    DocumentSessionState,
    DocumentType,
    DocumentVersion,
)
from .storage import get_store

SAFE_TITLE = re.compile(r"[^\w .()\-]+", re.UNICODE)


def _b64json(value: Mapping[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=").decode()


def sign_jwt(payload: Mapping[str, object]) -> str:
    header = _b64json({"alg": "HS256", "typ": "JWT"})
    body = _b64json(payload)
    signature = hmac.new(
        settings.ONLYOFFICE_JWT_SECRET.encode(), f"{header}.{body}".encode(), hashlib.sha256
    ).digest()
    return f"{header}.{body}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def verify_jwt(token: str) -> dict[str, object] | None:
    try:
        header, body, encoded_signature = token.split(".")
        expected = hmac.new(
            settings.ONLYOFFICE_JWT_SECRET.encode(), f"{header}.{body}".encode(), hashlib.sha256
        ).digest()
        actual = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        if not hmac.compare_digest(expected, actual):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if not isinstance(payload, dict) or payload.get("exp", float("inf")) < datetime.now(UTC).timestamp():
            return None
        return payload
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def version_key(document_id: object, version_id: object, extension: str) -> str:
    return f"documents/{document_id}/versions/{version_id}.{extension}"


def blank_document(document_type: str) -> BytesIO:
    stream = BytesIO()
    if document_type == DocumentType.SPREADSHEET:
        from openpyxl import Workbook

        Workbook().save(stream)
        stream.seek(0)
        return stream
    contents = {
        "_rels/.rels": "<?xml version='1.0'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='word/document.xml'/></Relationships>",
    }
    if document_type == DocumentType.DOCUMENT:
        contents["[Content_Types].xml"] = (
            "<?xml version='1.0'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'><Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/><Default Extension='xml' ContentType='application/xml'/><Override PartName='/word/document.xml' ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/></Types>"
        )
        contents["word/document.xml"] = (
            "<?xml version='1.0'?><w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p/><w:sectPr/></w:body></w:document>"
        )
    elif document_type == DocumentType.PRESENTATION:
        contents["[Content_Types].xml"] = (
            "<?xml version='1.0'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'><Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/><Default Extension='xml' ContentType='application/xml'/><Override PartName='/ppt/presentation.xml' ContentType='application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml'/></Types>"
        )
        contents["_rels/.rels"] = (
            "<?xml version='1.0'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='ppt/presentation.xml'/></Relationships>"
        )
        contents["ppt/presentation.xml"] = (
            "<?xml version='1.0'?><p:presentation xmlns:p='http://schemas.openxmlformats.org/presentationml/2006/main'><p:sldMasterIdLst/><p:sldIdLst/><p:sldSz cx='9144000' cy='6858000'/><p:notesSz cx='6858000' cy='9144000'/></p:presentation>"
        )
        contents["ppt/_rels/presentation.xml.rels"] = (
            "<?xml version='1.0'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'/>"
        )
    else:
        raise ValueError("Unsupported document type")
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    stream.seek(0)
    return stream


@transaction.atomic
def create_document(*, owner: Resident, title: str, document_type: str) -> Document:
    if document_type not in DocumentType.values:
        raise ValueError("Unsupported document type")
    cleaned_title = SAFE_TITLE.sub("", title).strip()[:200] or "Unavngivet"
    document = Document(
        title=cleaned_title,
        document_type=document_type,
        original_filename=f"{cleaned_title}.{document_type}",
        extension=document_type,
        object_key="",
        owner=owner,
    )
    document.save()
    version = DocumentVersion(
        document=document,
        sequence=1,
        object_key="",
        content_sha256="",
        size=0,
        save_type=DocumentSaveType.INITIAL,
    )
    version.save()
    key = version_key(document.id, version.id, document.extension)
    try:
        size, digest = get_store().save(key, blank_document(document_type))
    except Exception:
        transaction.set_rollback(True)
        raise
    version.object_key, version.size, version.content_sha256 = key, size, digest
    version.save(update_fields=["object_key", "size", "content_sha256"])
    document.object_key, document.current_version = key, version
    document.save()
    return document


@transaction.atomic
def restore_version(*, document: Document, version: DocumentVersion) -> DocumentVersion:
    locked = Document.objects.select_for_update().get(pk=document.pk)
    if locked.editing_sessions.filter(state=DocumentSessionState.ACTIVE).exists():
        raise ValueError("Document has an active collaborative session")
    sequence = (locked.versions.aggregate(maximum=Max("sequence"))["maximum"] or 0) + 1
    restored = DocumentVersion(
        document=locked,
        sequence=sequence,
        object_key="",
        content_sha256=version.content_sha256,
        size=version.size,
        save_type=DocumentSaveType.RESTORE,
    )
    restored.save()
    key = version_key(locked.pk, restored.pk, locked.extension)
    get_store().copy(version.object_key, key)
    restored.object_key = key
    restored.save(update_fields=["object_key"])
    locked.object_key, locked.current_version = key, restored
    locked.save(update_fields=["object_key", "current_version", "updated_at"])
    return restored


@transaction.atomic
def active_session(document_id: UUID | str) -> DocumentEditingSession:
    document = (
        Document.objects.select_for_update()
        .select_related("current_version")
        .get(pk=document_id, deleted_at__isnull=True)
    )
    session = document.editing_sessions.filter(state=DocumentSessionState.ACTIVE).first()
    if session is not None:
        return session
    if document.current_version is None:
        raise RuntimeError("Document has no current version")
    try:
        return DocumentEditingSession.objects.create(
            document=document, source_version=document.current_version, document_key=secrets.token_urlsafe(24)
        )
    except IntegrityError:
        return document.editing_sessions.get(state=DocumentSessionState.ACTIVE)


def editor_config(
    *, document: Document, resident: Resident, can_edit: bool, request: HttpRequest
) -> dict[str, object]:
    session = active_session(document.pk)
    base_url = settings.DOCUMENT_PUBLIC_URL.rstrip("/") or request.build_absolute_uri("/").rstrip("/")
    callback_url = base_url + reverse("documents:callback", args=[document.pk])
    source_url = get_store().signed_download_url(document.object_key, settings.DOCUMENT_DOWNLOAD_URL_TTL)
    user_id = hashlib.sha256(f"resident:{resident.pk}".encode()).hexdigest()[:32]
    config: dict[str, object] = {
        "documentType": {"docx": "word", "xlsx": "cell", "pptx": "slide"}[document.extension],
        "document": {
            "fileType": document.extension,
            "key": session.document_key,
            "title": document.original_filename,
            "url": source_url,
            "permissions": {"edit": can_edit, "download": can_edit, "print": True},
        },
        "editorConfig": {
            "callbackUrl": callback_url,
            "user": {"id": user_id, "name": resident.full_name[:128] or resident.email[:128]},
            "mode": "edit" if can_edit else "view",
            "lang": "da",
            "coEditing": {"mode": "fast", "change": False},
        },
    }
    config["token"] = sign_jwt(config)
    return config


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


def _safe_callback_url(value: str) -> str:
    parsed = urlparse(value)
    trusted = urlparse(settings.ONLYOFFICE_INTERNAL_URL)
    if parsed.scheme not in {"http", "https"} or parsed.netloc != trusted.netloc:
        raise ValueError("Callback file URL is not an ONLYOFFICE URL")
    return value


def download_callback_file(value: str) -> BytesIO:
    url = _safe_callback_url(value)
    response = build_opener(_RejectRedirects()).open(Request(url), timeout=30)  # noqa: S310
    maximum = settings.DOCUMENT_MAX_FILE_SIZE
    output = BytesIO()
    while chunk := response.read(512 * 1024):
        output.write(chunk)
        if output.tell() > maximum:
            raise ValueError("Callback file exceeds configured limit")
    output.seek(0)
    return output


@transaction.atomic
def persist_callback(*, document: Document, payload: Mapping[str, object], raw_payload: bytes) -> bool:
    key = str(payload.get("key", ""))
    session = (
        DocumentEditingSession.objects.select_for_update().filter(document=document, document_key=key).first()
    )
    if session is None:
        raise ValueError("Unknown editing session")
    digest = hashlib.sha256(raw_payload).hexdigest()
    if DocumentCallbackReceipt.objects.filter(session=session, payload_sha256=digest).exists():
        return False
    status = payload.get("status")
    if not isinstance(status, int):
        raise ValueError("Callback status is invalid")
    if status in {1, 4}:
        DocumentCallbackReceipt.objects.create(session=session, payload_sha256=digest, status=status)
        return False
    if status in {3, 7}:
        session.state = DocumentSessionState.FAILED
        session.save(update_fields=["state"])
        raise ValueError("ONLYOFFICE reported a save failure")
    save_url = payload.get("url")
    if status not in {2, 6} or not isinstance(save_url, str):
        raise ValueError("Unsupported callback payload")
    file = download_callback_file(save_url)
    version = DocumentVersion(
        document=document,
        sequence=(document.versions.aggregate(maximum=Max("sequence"))["maximum"] or 0) + 1,
        object_key="",
        content_sha256="",
        size=0,
        save_type=DocumentSaveType.FINAL if status == 2 else DocumentSaveType.FORCE_SAVE,
        editing_session=session,
    )
    version.save()
    object_key = version_key(document.pk, version.pk, document.extension)
    size, content_sha256 = get_store().save(object_key, file)
    if document.current_version is not None and document.current_version.content_sha256 == content_sha256:
        get_store().delete(object_key)
        version.delete()
        DocumentCallbackReceipt.objects.create(session=session, payload_sha256=digest, status=status)
        return False
    version.object_key, version.size, version.content_sha256 = object_key, size, content_sha256
    version.save(update_fields=["object_key", "size", "content_sha256"])
    document.object_key, document.current_version = object_key, version
    document.save(update_fields=["object_key", "current_version", "updated_at"])
    if status == 2:
        session.finish()
        session.final_saved_at = timezone.now()
        session.save(update_fields=["state", "finished_at", "final_saved_at"])
    DocumentCallbackReceipt.objects.create(session=session, payload_sha256=digest, status=status)
    return True
