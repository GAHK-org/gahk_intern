"""New files from the folder's `Ny fil`, and the text editor (spec/features/Drive.md, Preview and live
editing).

`Ny fil` makes an empty text, markdown or CSV file - or, while live editing is on, a blank document,
spreadsheet or presentation (drive/office.py) - and opens it. The text editor is a plain text box over
the whole file: what it saves becomes the file's next version, like an upload. Two residents saving
the same file at once are told rather than overwritten: a save names the version it started from,
and is refused when another came after it.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction

from residents.models import Resident

from . import office, preview, services
from .access import Viewer
from .models import File, FileVersion, Folder

# What `Ny fil` offers: kind -> (label, name without a name given, extension). Office kinds last.
TEXT_KINDS = {
    "text": ("Tekstfil", "Ny tekstfil", "txt"),
    "markdown": ("Markdown", "Ny note", "md"),
    "csv": ("CSV-tabel", "Ny tabel", "csv"),
}
OFFICE_LABELS = {"document": "Dokument", "spreadsheet": "Regneark", "presentation": "Præsentation"}
CONTENT_TYPES = {
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
}
# Files up to this size open in the editor: what the preview shows of a text file.
EDIT_BYTES = preview.TEXT_BYTES
EDITABLE_KINDS = (preview.Kind.TEXT, preview.Kind.CSV, preview.Kind.MARKDOWN)


CONFLICT = "Filen er gemt af en anden, mens du skrev."


class Conflict(ValidationError):
    """Somebody saved a newer version after the editor was opened."""


def new_file_kinds() -> list[dict[str, object]]:
    """`Ny fil`'s choices, each marked with whether it needs live editing."""
    kinds: list[dict[str, object]] = [
        {"kind": kind, "label": label, "extension": extension, "office": False}
        for kind, (label, _, extension) in TEXT_KINDS.items()
    ]
    kinds += [
        {"kind": kind, "label": OFFICE_LABELS[kind], "extension": extension, "office": True}
        for kind, (_, extension) in office.NEW.items()
    ]
    for item in kinds:
        item["icon"] = preview.icon(f"new.{item['extension']}")
    return kinds


def new_file(viewer: Viewer, folder: Folder, kind: str, name: str, resident: Resident) -> File:
    """An empty text file, numbered if the name is taken."""
    if kind not in TEXT_KINDS:
        raise ValidationError("Den slags fil findes ikke.")
    if not viewer.can_upload_to(folder):
        raise PermissionDenied
    _, default, extension = TEXT_KINDS[kind]
    key = services.new_key()
    services.store_local(key, ContentFile(b"", name=f"blank.{extension}"))
    return services.add_upload(
        viewer,
        folder,
        name=services.new_file_name(name, default, extension),
        key=key,
        size=0,
        content_type=CONTENT_TYPES[extension],
        on_conflict=services.KEEP_BOTH,
        resident=resident,
    ).file


def editable(file: File) -> bool:
    """A text, markdown or CSV file small enough to open whole in the editor."""
    return preview.kind_of(file.name) in EDITABLE_KINDS and file.size <= EDIT_BYTES


def _stored(version: FileVersion) -> bytes:
    with services.storage().open(version.key, "rb") as source:
        return source.read(EDIT_BYTES + 1)


def _decoded(data: bytes) -> str:
    return preview.decode(data).replace("\r\n", "\n")


def read(version: FileVersion) -> str:
    """The text as the editor shows it: decoded like the preview, with plain newlines."""
    return _decoded(_stored(version))


def _encode(content: str, name: str, *, windows: bool) -> bytes:
    """UTF-8 - with the mark that tells Excel so, for a CSV - and the line endings the file had."""
    content = content.replace("\r\n", "\n")
    if windows:
        content = content.replace("\n", "\r\n")
    encoding = "utf-8-sig" if preview.kind_of(name) == preview.Kind.CSV else "utf-8"
    return content.encode(encoding)


def save(viewer: Viewer, file: File, content: str, *, base: int, resident: Resident) -> FileVersion | None:
    """`content` as the file's next version, or None if nothing changed. Refused with Conflict when
    the latest version is no longer the one numbered `base`."""
    if file.deleted_at is not None or not viewer.can_version(file) or not editable(file):
        raise PermissionDenied
    latest = file.latest_version()
    if latest is None or latest.number != base:
        raise Conflict(CONFLICT)
    old = _stored(latest)
    if content.replace("\r\n", "\n") == _decoded(old):
        return None
    data = _encode(content, file.name, windows=b"\r\n" in old)
    if len(data) > EDIT_BYTES:
        raise ValidationError(f"Teksten er for lang til at gemme her (over {EDIT_BYTES // 1024 // 1024} MB).")
    extension = file.name.rsplit(".", 1)[-1].lower()
    key = services.new_key()
    services.store_local(key, ContentFile(data, name=file.name))
    try:
        with transaction.atomic():
            locked = File.objects.select_for_update().get(pk=file.pk)
            if locked.latest_version().number != base:
                raise Conflict(CONFLICT)
            services.add_version(
                viewer,
                locked,
                key=key,
                size=len(data),
                content_type=CONTENT_TYPES.get(extension, "text/plain"),
                resident=resident,
            )
    except Exception:
        services.storage().delete(key)  # nothing points at it: the version was never made
        raise
    return locked.latest_version()
