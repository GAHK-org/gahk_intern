"""Live editing in a Document Server that speaks OnlyOffice's API (spec/features/Drive.md, Preview
and live editing): Euro-Office, the OnlyOffice fork, which is what DEPLOY.md §4h runs. OnlyOffice's
own Document Server works the same way; the settings are named after the API, ONLYOFFICE_*.

SWITCHED OFF while ONLYOFFICE_URL is empty, and whenever the Document Server does not answer its
health check: office files are then downloaded, and nothing offers to open them. Everything else in
Drive works the same either way.

How the pieces meet:

  * The resident's browser loads the editor from ONLYOFFICE_URL, with a configuration this app signs
    with ONLYOFFICE_JWT_SECRET (`editor_config`). It names the resident by id and name, never more.
  * The Document Server fetches the document from this app (`fetch_url`): a signed link to one
    version, good for FETCH_HOURS, that needs no login - the Document Server never has a resident's
    session, and reaches only the documents Drive opened in it.
  * When the last editor leaves, it posts the result to `callback_url` (signed per file, and its body
    is a JWT signed with the same secret). The edited document becomes the file's next version,
    listing everyone who edited in it. Should the file have been locked or binned meanwhile, the work
    is kept as a new file beside it rather than lost.

Everyone who opens the same version shares one document key, which is what puts them in one session
together, seeing each other. A new version is a new key.

docx, xlsx and pptx are edited; the older formats (doc, xls, ppt, odt, ods, odp, rtf) are opened for
viewing, and `convert` makes a docx, xlsx or pptx copy beside the original to edit.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.request
from datetime import timedelta
from pathlib import Path, PurePosixPath
from tempfile import SpooledTemporaryFile
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files import File as DjangoFile
from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from residents.models import Resident

from . import history, services
from .access import Viewer
from .models import Event, File, FileVersion, Folder

logger = logging.getLogger(__name__)

EDITABLE = {"docx": "word", "xlsx": "cell", "pptx": "slide"}
VIEW_ONLY = {
    "doc": "word",
    "odt": "word",
    "rtf": "word",
    "xls": "cell",
    "ods": "cell",
    "ppt": "slide",
    "odp": "slide",
}
# What an older format is copied to for editing, by document type.
EDIT_FORMAT = {"word": "docx", "cell": "xlsx", "slide": "pptx"}
CONTENT_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
# `Nyt dokument`, `Nyt regneark`, `Ny præsentation`: kind -> (name, extension).
NEW = {
    "document": ("Nyt dokument", "docx"),
    "spreadsheet": ("Nyt regneark", "xlsx"),
    "presentation": ("Ny præsentation", "pptx"),
}
BLANK_DIR = Path(__file__).parent / "blank"

FETCH_HOURS = 24
HEALTH_CACHE_KEY = "drive-onlyoffice-healthy"
HEALTH_SECONDS = 60
TIMEOUT = 30


# --- On or off -------------------------------------------------------------------------------------


def configured() -> bool:
    return bool(settings.ONLYOFFICE_URL and settings.ONLYOFFICE_JWT_SECRET)


def enabled() -> bool:
    """Configured, and answering: a Document Server that is down switches live editing off until it
    is back, checked at most once a minute."""
    if not configured():
        return False
    healthy = cache.get(HEALTH_CACHE_KEY)
    if healthy is None:
        healthy = _healthcheck()
        cache.set(HEALTH_CACHE_KEY, healthy, HEALTH_SECONDS)
    return bool(healthy)


def _healthcheck() -> bool:
    try:
        with urllib.request.urlopen(f"{settings.ONLYOFFICE_INTERNAL_URL}/healthcheck", timeout=3) as response:  # noqa: S310  # nosec B310 - our own setting
            return response.read().strip() == b"true"
    except (OSError, ValueError) as exc:
        logger.warning("drive: the Document Server does not answer its health check: %s", exc)
        return False


def _extension(name: str) -> str:
    return PurePosixPath(name).suffix.lower().lstrip(".")


def document_type(name: str) -> str | None:
    """`word`, `cell` or `slide` for what the Document Server opens, else None."""
    extension = _extension(name)
    return EDITABLE.get(extension) or VIEW_ONLY.get(extension)


def editable(name: str) -> bool:
    return _extension(name) in EDITABLE


# --- Signed tokens (HS256 JWT, what the Document Server speaks) ------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(signing_input: str) -> bytes:
    return hmac.new(settings.ONLYOFFICE_JWT_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()


def sign(payload: dict[str, Any]) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{header}.{body}.{_b64(_mac(f'{header}.{body}'))}"


class BadToken(Exception):
    pass


def verify(token: str) -> dict[str, Any]:
    try:
        header, body, signature = token.split(".")
        if json.loads(_unb64(header)).get("alg") != "HS256":
            raise BadToken("not HS256")
        if not hmac.compare_digest(_unb64(signature), _mac(f"{header}.{body}")):
            raise BadToken("wrong signature")
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError) as exc:
        raise BadToken(str(exc)) from exc
    if not isinstance(payload, dict):
        raise BadToken("not an object")
    if "exp" in payload and float(payload["exp"]) < time.time():
        raise BadToken("expired")
    return payload


# --- Links for the Document Server -------------------------------------------------------------------

_FETCH_SALT = "drive.office.fetch"
_CALLBACK_SALT = "drive.office.callback"


def _app_url(name: str, token: str) -> str:
    return f"{settings.ONLYOFFICE_APP_URL}{reverse(name, args=[token])}"


def fetch_url(version: FileVersion) -> str:
    return _app_url("drive:office_fetch", signing.dumps({"v": version.pk}, salt=_FETCH_SALT))


def fetched_version(token: str) -> FileVersion | None:
    try:
        data = signing.loads(token, salt=_FETCH_SALT, max_age=timedelta(hours=FETCH_HOURS))
    except signing.BadSignature:
        return None
    return FileVersion.objects.select_related("file").filter(pk=data.get("v")).first()


def callback_url(file: File) -> str:
    return _app_url("drive:office_callback", signing.dumps({"f": file.pk}, salt=_CALLBACK_SALT))


def called_back_file(token: str) -> int | None:
    try:
        return int(signing.loads(token, salt=_CALLBACK_SALT)["f"])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None


def document_key(file: File, version: FileVersion) -> str:
    """The same for everyone who opens this version, so they edit it together; new with each version."""
    return f"drive-{file.pk}-{version.number}-{version.key.rsplit('/', 1)[-1][:8]}"


def editor_config(
    file: File, version: FileVersion, resident: Resident, *, edit: bool, back_url: str, phone: bool = False
) -> dict[str, Any]:
    kind = document_type(file.name)
    config: dict[str, Any] = {
        # The phone editor, for reading on a phone. It only reads in the free editions (Euro-Office and
        # OnlyOffice alike), so a phone that edits gets the computer layout: cramped, but it edits.
        "type": "mobile" if phone else "desktop",
        "width": "100%",
        "height": "100%",
        "documentType": kind,
        "document": {
            "fileType": _extension(file.name),
            "key": document_key(file, version),
            "title": file.name,
            "url": fetch_url(version),
            "permissions": {"edit": edit, "comment": edit, "review": edit, "download": True, "print": True},
        },
        "editorConfig": {
            "mode": "edit" if edit else "view",
            "lang": "da",
            "region": "da-DK",
            "callbackUrl": callback_url(file),
            "user": {"id": str(resident.pk), "name": resident.full_name},
            "customization": {"goback": {"url": back_url, "text": "Tilbage til Drive"}, "forcesave": False},
        },
    }
    config["token"] = sign(config)
    return config


# --- Back from the Document Server --------------------------------------------------------------------


def callback(file_id: int, data: dict[str, Any]) -> bool:
    """One of the Document Server's status posts about a file's editing session. False when it should
    be told the save failed (it then keeps the document and tries again)."""
    file = File.objects.filter(pk=file_id).first()
    key = str(data.get("key", ""))
    if file is None or not key.startswith(f"drive-{file_id}-"):
        logger.warning(
            "drive: the Document Server called back about %s for file %s, which is not it", key, file_id
        )
        return False
    status = int(data.get("status", 0))
    if status == 1:  # somebody joined or left: being edited
        File.objects.filter(pk=file.pk).update(editing_key=key, editing_at=timezone.now())
        return True
    if status in (2, 6):  # ready to save: 2 when the last one left, 6 a save while still open
        saved = _save(file, data)
        if status == 2:
            File.objects.filter(pk=file.pk, editing_key=key).update(editing_key="", editing_at=None)
        return saved
    if status in (3, 4, 7):  # closed with nothing to save, or it could not save
        if status != 4:
            logger.error("drive: the Document Server could not save %s (status %s)", key, status)
        File.objects.filter(pk=file.pk, editing_key=key).update(editing_key="", editing_at=None)
    return True


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme, parts.netloc


def _from_document_server(url: str) -> str | None:
    """`url` if the Document Server serves it - reached the way this app reaches the server - else
    None: the callback names the URL, and this app must not be made to fetch anything else."""
    internal = _origin(settings.ONLYOFFICE_INTERNAL_URL)
    if _origin(url) == internal:
        return url
    if _origin(url) == _origin(settings.ONLYOFFICE_URL):
        parts = urlsplit(url)
        return parts._replace(scheme=internal[0], netloc=internal[1]).geturl()
    return None


def _download(url: str) -> tuple[str, int]:
    """The document at `url`, stored under a new key: (key, size)."""
    limit = services.max_upload_bytes()
    with (
        urllib.request.urlopen(url, timeout=TIMEOUT) as response,  # noqa: S310  # nosec B310 - _from_document_server checked it
        SpooledTemporaryFile(16 * 1024 * 1024) as copy,
    ):
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValidationError("too big")
            copy.write(chunk)
        copy.seek(0)
        key = services.new_key()
        services.store_local(key, DjangoFile(copy, name="edited"))
    return key, size


def _locked_above(folder: Folder) -> bool:
    ids = [int(part) for part in folder.path.strip("/").split("/") if part]
    now = timezone.now()
    return any(f.lock_active(now) for f in Folder.objects.filter(pk__in=ids, locked_at__isnull=False))


def _editor_ids(data: dict[str, Any]) -> list[int]:
    """Everyone who changed something, in the order they first did: the session's history has each
    change's author, where `users` names only the last one to edit."""
    history_ = data.get("history")
    changes = history_.get("changes", []) if isinstance(history_, dict) else []
    found = [str((change.get("user") or {}).get("id", "")) for change in changes if isinstance(change, dict)]
    found += [str(user) for user in data.get("users", [])]
    return list(dict.fromkeys(int(user) for user in found if user.isdigit()))


def _save(file: File, data: dict[str, Any]) -> bool:
    url = _from_document_server(str(data.get("url", "")))
    if url is None:
        logger.error("drive: the Document Server named %r to save %s from; refused", data.get("url"), file.pk)
        return False
    ids = _editor_ids(data)
    by_id = Resident.objects.in_bulk(ids)
    editors = [by_id[pk] for pk in ids if pk in by_id]
    try:
        key, size = _download(url)
    except (OSError, ValidationError):
        logger.exception("drive: could not fetch the edited %s from the Document Server", file.pk)
        return False
    content_type = CONTENT_TYPES.get(_extension(file.name), file.content_type)
    with transaction.atomic():
        file = File.objects.select_for_update().select_related("folder").get(pk=file.pk)
        if file.deleted_at is None and not file.lock_active() and not _locked_above(file.folder):
            services.add_edited_version(file, key=key, size=size, content_type=content_type, editors=editors)
            return True
        folder = file.folder
        if folder.deleted_at is None and not _locked_above(folder):
            # Locked or binned while it was open: the work is kept beside it.
            _new_file(folder, file.name, key, size, content_type, editors)
            return True
    logger.error("drive: the edited %s had nowhere to go; dropped", file.pk)
    services.storage().delete(key)
    return False


def _new_file(
    folder: Folder, name: str, key: str, size: int, content_type: str, editors: list[Resident]
) -> File:
    actor = editors[0] if editors else None
    now = timezone.now()
    file = File.objects.create(
        folder=folder,
        name=services.free_name(folder, name),
        size=size,
        content_type=content_type,
        created_by=actor,
        changed_by=actor,
        changed_at=now,
    )
    version = FileVersion.objects.create(
        file=file, number=1, key=key, size=size, content_type=content_type, created_by=actor
    )
    version.editors.set(editors)
    history.record(Event.Action.CREATED, actor=actor, file=file, name=file.name)
    return file


# --- New documents, and editable copies ------------------------------------------------------------------


def new_document(viewer: Viewer, folder: Folder, kind: str, resident: Resident) -> File:
    if kind not in NEW:
        raise ValidationError("Den slags dokument findes ikke.")
    if not viewer.can_upload_to(folder):
        raise PermissionDenied
    name, extension = NEW[kind]
    content = (BLANK_DIR / f"blank.{extension}").read_bytes()
    key = services.new_key()
    services.store_local(key, ContentFile(content, name=f"blank.{extension}"))
    return services.add_upload(
        viewer,
        folder,
        name=f"{name}.{extension}",
        key=key,
        size=len(content),
        content_type=CONTENT_TYPES[extension],
        on_conflict=services.KEEP_BOTH,
        resident=resident,
    ).file


def convert(viewer: Viewer, file: File, resident: Resident) -> File:
    """A docx, xlsx or pptx copy of an older format, beside it, made by the Document Server."""
    kind = document_type(file.name)
    if kind is None or editable(file.name):
        raise ValidationError("Den fil kan redigeres, som den er.")
    if not viewer.can_upload_to(file.folder):
        raise PermissionDenied
    target = EDIT_FORMAT[kind]
    version = file.latest_version()
    payload = {
        "async": False,
        "filetype": _extension(file.name),
        "outputtype": target,
        "key": f"drive-convert-{version.pk}-{uuid4().hex[:12]}",
        "title": file.name,
        "url": fetch_url(version),
    }
    request = urllib.request.Request(  # noqa: S310 - our own setting
        f"{settings.ONLYOFFICE_INTERNAL_URL}/ConvertService.ashx",
        data=json.dumps({**payload, "token": sign(payload)}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {sign({'payload': payload})}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310  # nosec B310 - our own setting
            answer = json.load(response)
        if answer.get("error") or not answer.get("endConvert"):
            raise ValidationError(f"Document Server error {answer.get('error')}")
        url = _from_document_server(str(answer.get("fileUrl", "")))
        if url is None:
            raise ValidationError("the Document Server named a file elsewhere")
        key, size = _download(url)
    except (OSError, ValueError, ValidationError) as exc:
        logger.exception("drive: the Document Server could not convert %s", file.pk)
        raise ValidationError("Der kunne ikke laves en kopi, der kan redigeres. Prøv igen senere.") from exc
    return services.add_upload(
        viewer,
        file.folder,
        name=f"{PurePosixPath(file.name).stem}.{target}",
        key=key,
        size=size,
        content_type=CONTENT_TYPES[target],
        on_conflict=services.KEEP_BOTH,
        resident=resident,
    ).file
