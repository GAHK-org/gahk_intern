"""What Drive shows of a file in the browser (spec/features/Drive.md, Preview and live editing).

A file's KIND is read off its name, never off the type the browser sent with the upload: that type
is the uploader's to choose. The kind decides the icon in the list and what the file's page shows:

  * an image: its viewer copy, with next and previous through the folder. The copy and the list's
    thumbnail are made in the background (ImagePreview) - a HEIC from an iPhone is then shown in
    every browser, and a 20 MB photo is not what a phone loads to look at it;
  * a PDF, a video, a sound: the browser's own viewer or player, on the stored bytes, served inline
    under a type chosen here;
  * text: the first TEXT_BYTES, a CSV as a table and markdown rendered;
  * anything else - office files until OnlyOffice comes - is only downloaded.
"""

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from uuid import uuid4

from django.conf import settings
from django.core.files import File as DjangoFile
from django.db import transaction
from django.utils import timezone
from django.utils.safestring import SafeString

from core import disk_cache
from core.markdown import render_markdown
from photo_album.services import image_variants

from .models import File, FileVersion, ImagePreview

logger = logging.getLogger(__name__)

PREVIEW_PREFIX = "drive/previews/"
# How much of a text file is shown, and how many rows of a CSV.
TEXT_BYTES = 1024 * 1024
CSV_ROWS = 2000
# A build that failed this often is given up on: the image gets the icon, and is downloaded.
MAX_ATTEMPTS = 3


class Kind(StrEnum):
    IMAGE = "image"
    PDF = "pdf"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"
    CSV = "csv"
    MARKDOWN = "markdown"
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    ARCHIVE = "archive"
    OTHER = "other"


_KINDS = {
    **dict.fromkeys(("jpg", "jpeg", "png", "gif", "webp", "heic", "heif"), Kind.IMAGE),
    "pdf": Kind.PDF,
    **dict.fromkeys(("mp4", "webm", "mov"), Kind.VIDEO),
    **dict.fromkeys(("mp3", "m4a"), Kind.AUDIO),
    **dict.fromkeys(("txt", "json", "log", "xml", "yaml", "yml", "ini"), Kind.TEXT),
    **dict.fromkeys(("csv", "tsv"), Kind.CSV),
    **dict.fromkeys(("md", "markdown"), Kind.MARKDOWN),
    **dict.fromkeys(("docx", "doc", "odt", "rtf"), Kind.DOCUMENT),
    **dict.fromkeys(("xlsx", "xls", "ods"), Kind.SPREADSHEET),
    **dict.fromkeys(("pptx", "ppt", "odp"), Kind.PRESENTATION),
    **dict.fromkeys(("zip", "7z", "rar", "tar", "gz"), Kind.ARCHIVE),
}

_ICONS = {
    Kind.IMAGE: "photo",
    Kind.VIDEO: "film",
    Kind.AUDIO: "music",
    Kind.CSV: "sheet",
    Kind.SPREADSHEET: "sheet",
    Kind.PRESENTATION: "slides",
    Kind.ARCHIVE: "archive",
    Kind.OTHER: "file",
}

# What the browser shows itself, and the type it is told the bytes are.
INLINE_TYPES = {
    "pdf": "application/pdf",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
}


def _extension(name: str) -> str:
    return PurePosixPath(name).suffix.lower().lstrip(".")


def kind_of(name: str) -> Kind:
    return _KINDS.get(_extension(name), Kind.OTHER)


def icon(name: str) -> str:
    """The sprite symbol for the file's kind: `i-<icon>` in base.html."""
    return _ICONS.get(kind_of(name), "doc")


def inline_type(name: str) -> str | None:
    return INLINE_TYPES.get(_extension(name))


# --- Image previews --------------------------------------------------------------------------------


def _storage():  # noqa: ANN202 - a Storage
    from .services import storage  # services imports this module

    return storage()


def _max_image_bytes() -> int:
    return int(settings.DRIVE_PREVIEW_IMAGE_MAX_MB) * 1024 * 1024


def ensure(version: FileVersion, name: str) -> ImagePreview | None:
    """The preview of an image version, queued for building when it has none yet. None for what is
    not an image, or too big to decode: that is downloaded to be seen."""
    if kind_of(name) != Kind.IMAGE or version.size > _max_image_bytes():
        return None
    row, created = ImagePreview.objects.get_or_create(key=version.key)
    if created:
        _queue(row.key)
    return row


def _queue(key: str) -> None:
    from .tasks import build_image_preview  # tasks imports this module

    transaction.on_commit(lambda: build_image_preview.apply_async((key,), queue="thumbnails"))


def build(key: str) -> bool:
    """Make the viewer copy and the thumbnail of the bytes at `key`. True when they were made."""
    row = ImagePreview.objects.filter(key=key, state=ImagePreview.State.PENDING).first()
    if row is None:
        return False
    ImagePreview.objects.filter(pk=row.pk).update(attempts=row.attempts + 1)

    store = _storage()
    try:
        with store.open(key, "rb") as source:
            variants = image_variants(DjangoFile(source, name=key), "preview.jpg")
        if variants is None:
            ImagePreview.objects.filter(pk=row.pk).update(state=ImagePreview.State.FAILED)
            return False
        viewer_key = _save(store, variants.high_definition)
        thumbnail_key = _save(store, variants.thumbnail)
    except Exception:
        logger.exception("drive: no preview of %s (attempt %d)", key, row.attempts + 1)
        if row.attempts + 1 >= MAX_ATTEMPTS:
            ImagePreview.objects.filter(pk=row.pk).update(state=ImagePreview.State.FAILED)
        return False
    done = ImagePreview.objects.filter(pk=row.pk, state=ImagePreview.State.PENDING).update(
        state=ImagePreview.State.READY, viewer_key=viewer_key, thumbnail_key=thumbnail_key
    )
    if not done:  # another worker got there first, or the bytes went meanwhile
        store.delete(viewer_key)
        store.delete(thumbnail_key)
    return bool(done)


def _save(store, content: DjangoFile) -> str:  # noqa: ANN001 - a Storage
    suffix = PurePosixPath(content.name or "").suffix or ".jpg"
    return store.save(f"{PREVIEW_PREFIX}{uuid4()}{suffix}", content)


def queue_stranded() -> int:
    """Nightly: queue again what is still waiting an hour after it was asked for - a message lost
    while the broker was down, or a worker killed mid-build."""
    stranded = ImagePreview.objects.filter(
        state=ImagePreview.State.PENDING,
        attempts__lt=MAX_ATTEMPTS,
        created_at__lt=timezone.now() - timedelta(hours=1),
    ).values_list("key", flat=True)
    keys = list(stranded)
    for key in keys:
        _queue(key)
    return len(keys)


def drop(keys: set[str]) -> None:
    """The bytes at `keys` are going: so do their previews."""
    rows = list(ImagePreview.objects.filter(key__in=keys))
    if not rows:
        return
    derived = [k for row in rows for k in (row.viewer_key, row.thumbnail_key) if k]
    ImagePreview.objects.filter(pk__in=[row.pk for row in rows]).delete()

    store = _storage()
    directory = _cache_directory()

    def remove() -> None:
        for k in derived:
            store.delete(k)
            if directory is not None:
                disk_cache.discard(directory, k)

    transaction.on_commit(remove)


def _cache_directory() -> Path | None:
    root = getattr(settings, "MEDIA_CACHE_DIR", "")
    return Path(root) / "drive" if root else None


def cached(key: str) -> Path | None:
    """A local copy of a preview (core.disk_cache), so a folder of photos is not a redirect to the
    bucket per thumbnail. None when the cache is off (MEDIA_CACHE_DIR empty, as in the tests)."""
    directory = _cache_directory()
    if directory is None:
        return None

    return disk_cache.cached_copy(directory, key, _storage(), settings.DRIVE_CACHE_MB * 1024 * 1024)


def thumbnails(files: list[File]) -> dict[int, int]:
    """file id -> its latest version's number, for the images in `files` whose thumbnail is ready -
    the number goes in the thumbnail's URL, so a new version is not shown from the browser's cache."""
    images = [file.pk for file in files if kind_of(file.name) == Kind.IMAGE]
    if not images:
        return {}
    latest: dict[int, tuple[int, str]] = {}
    for file_id, number, key in FileVersion.objects.filter(file_id__in=images).values_list(
        "file_id", "number", "key"
    ):
        if file_id not in latest or number > latest[file_id][0]:
            latest[file_id] = (number, key)
    ready = set(
        ImagePreview.objects.filter(
            key__in=[key for _, key in latest.values()], state=ImagePreview.State.READY
        ).values_list("key", flat=True)
    )
    return {file_id: number for file_id, (number, key) in latest.items() if key in ready}


def ready(version: FileVersion) -> ImagePreview | None:
    return ImagePreview.objects.filter(key=version.key, state=ImagePreview.State.READY).first()


# --- Text --------------------------------------------------------------------------------------------


@dataclass
class Text:
    text: str
    truncated: bool
    rows: list[list[str]] = field(default_factory=list)
    more_rows: bool = False
    html: SafeString | None = None


def _decode(data: bytes) -> str:
    """UTF-8, or Windows' Western encoding for what a Danish Excel saved. A UTF-8 character cut in
    half by the TEXT_BYTES limit is left out."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        if exc.start >= len(data) - 3 and exc.reason == "unexpected end of data":
            return data[: exc.start].decode("utf-8-sig")
        return data.decode("cp1252", errors="replace")


def text(version: FileVersion, name: str) -> Text:

    with _storage().open(version.key, "rb") as source:
        data = source.read(TEXT_BYTES + 1)
    truncated = len(data) > TEXT_BYTES
    result = Text(_decode(data[:TEXT_BYTES]), truncated)
    kind = kind_of(name)
    if kind == Kind.CSV:
        result.rows, result.more_rows = _table(
            result.text, truncated=truncated, tabs=name.lower().endswith(".tsv")
        )
    elif kind == Kind.MARKDOWN:
        result.html = render_markdown(result.text, with_images=False)
    return result


def _table(source: str, *, truncated: bool, tabs: bool) -> tuple[list[list[str]], bool]:
    """The rows of a CSV, at most CSV_ROWS; a row cut off by the TEXT_BYTES limit is left out. The
    separator is guessed: a Danish Excel writes `;`."""
    if truncated and "\n" in source:
        source = source[: source.rindex("\n")]
    if tabs:
        dialect: type[csv.Dialect] | csv.Dialect = csv.excel_tab
    else:
        try:
            dialect = csv.Sniffer().sniff(source[:16384], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
    rows: list[list[str]] = []
    for row in csv.reader(io.StringIO(source, newline=""), dialect):
        if len(rows) == CSV_ROWS:
            return rows, True
        rows.append(row)
    return rows, truncated
