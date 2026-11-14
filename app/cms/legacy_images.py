"""Moving the old site's images into the image library, by itself.

The legacy site kept its content images as files (/public/image/…), and this project carries them
under static/legacy/image/upload/ (LEGACY_DIRECTORY), in the image like any static file. They are
content, not code: they belong in the image library (CmsImage, in S3) like every other picture, and
gahk.dk gets them from there. This import moves them; the files leave the repository with it, once
production has them all (the cutover's cleanup).

ALL of them are imported, not only the ones the content still uses: other websites, old mails and
search engines link old picture addresses, and gahk.dk redirects each one to its library copy
(legacy_image_map). "All" is every file below upload/. An image an editor deletes from the library
is recorded as deleted (LegacyImageFailure, cms.services.delete_image) and not imported again,
unless content refers to it again.

The content's references are imported first, then the rest of upload/. For every legacy image
reference in the stored content —

    /public/image/<rest>   public/image/<rest>   http(s)://(www.)gahk.dk/public/image/<rest>
    /static/legacy/image/<rest>   (and the same with http(s)://(www.)gahk.dk in front)

— in Page.body/background_image, NewsItem.body, Event.description, PageVersion.body/
background_image and SiteSettings.default_background, the file <rest> (below static/legacy/image/)
is imported into the library once (checked and downscaled like an upload, `legacy_path` = <rest>,
uploaded by nobody), and the reference is rewritten to the library URL. Every import and every
rewrite is recorded as a system ContentChange. A reference to a file outside upload/ (elements/…)
is looked for in the same directory.

It runs by itself: cms.tasks.sync_gahk_web calls `import_pending_legacy_images` every 2 minutes,
before it builds the snapshot. A run that finds nothing left to import does nothing (a few queries
and a directory listing). Otherwise it imports at most AUTO_MAX_FILES files in at most AUTO_SECONDS
(each is downscaled and uploaded to S3) and leaves the rest to the next run, so the outbox pull
behind it is never held up for long. A file that does not exist or is not a usable image is logged,
recorded as a LegacyImageFailure and not asked for again; its references stay as they are (gahk.dk
shows such a picture as broken, such a background as a plain band).

`manage.py import_legacy_images` runs the same import by hand, all of it at once, without the budget
and retrying the recorded failures (not the deleted ones).

Idempotent, and one run at a time (a PostgreSQL advisory lock): a second run finds the images in
the library and the content already rewritten.

TEMPORARY. Once production has every file in the library (or deleted) and no legacy reference left
(the Hjemmeside overview says so), this module, upload/, the command and LegacyImageFailure go;
`legacy_path` stays, because gahk.dk's redirects for old image links are built from it
(legacy_image_map).
"""

import logging
import os
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Model, Q
from PIL import Image

from core.images import DECODE_ERRORS

from .images import EXTENSION_TYPES, ImageRejected, prepare_image
from .models import (
    CmsImage,
    ContentChange,
    Event,
    LegacyImageFailure,
    NewsItem,
    Page,
    PageVersion,
    SiteSettings,
)

logger = logging.getLogger(__name__)

SYSTEM_NAME = "System (gamle billeder flyttet til billedbiblioteket)"
OLD_URL_PREFIX = "/static/legacy/image/"

# The old site's static/legacy/image/: upload/ holds its content images (all of them are imported),
# elements/ and intern/ the intranet's own pictures, which stay.
LEGACY_DIRECTORY = Path(settings.BASE_DIR) / "static" / "legacy" / "image"
LISTED_DIRECTORY = "upload/"
MAX_FILE_BYTES = 25 * 1024 * 1024  # the largest old image is 13 MB

# What one automatic run may spend before it leaves the rest to the next one, 2 minutes later.
AUTO_MAX_FILES = 20
AUTO_SECONDS = 60

# pg advisory lock id for "a legacy image import is running" ("gahkleg" in ASCII). Not cms.tasks'
# publish lock: a publish need not wait for an import, only two imports for each other.
IMPORT_LOCK_ID = 0x6761686B6C6567

# A legacy reference, wherever it sits: an attribute, a CSS url(), or the whole of a background
# field. The look-behind keeps it from matching inside a longer URL (example.com/public/image/…).
# `rest` stops at what cannot be part of a URL in an attribute. In a style attribute it may swallow
# the end of the CSS: the ")" of an unquoted url() and what follows it without a space
# ("…/a.jpg);color:red"), or the "&quot;)" of url("…"), which nh3 writes with entities. _options
# cuts those off again, at a ")" or "&", when the file name does not go on past it.
LEGACY_REF_RE = re.compile(
    r"(?<![\w./:-])"
    r"(?:https?://(?:www\.)?gahk\.dk)?"
    r"(?:/public/image/|public/image/|/static/legacy/image/)"
    r"(?P<rest>[^\"'\s<>?#\\]+)",
    re.IGNORECASE,
)

# (model, fields) that may hold a legacy reference. PageVersion is history, but restoring a version
# must not bring back a reference that no longer resolves.
TEXT_FIELDS: tuple[tuple[type[Model], tuple[str, ...]], ...] = (
    (Page, ("body", "background_image")),
    (NewsItem, ("body",)),
    (Event, ("description",)),
    (SiteSettings, ("default_background",)),
    (PageVersion, ("body", "background_image")),
)

KIND_OF: dict[type[Model], str] = {
    Page: ContentChange.Kind.PAGE,
    NewsItem: ContentChange.Kind.NEWS,
    Event: ContentChange.Kind.EVENT,
    SiteSettings: ContentChange.Kind.SETTINGS,
}

# The longest legacy path that fits CmsImage.legacy_path and LegacyImageFailure.legacy_path. A longer
# one names no file of the old site, and storing it would fail every run at the database.
MAX_PATH_LENGTH = 255


# --------------------------------------------------------------------------------------- files
class LegacyFileMissing(Exception):
    """No such file in LEGACY_DIRECTORY."""


class LegacyFileUnusable(Exception):
    """The file is there, but it is not an image the library takes."""


def legacy_files() -> dict[str, Path]:
    """Every file below LEGACY_DIRECTORY, by its legacy path ("upload/images/gang.jpg").

    The paths are NFC, as the content and CmsImage.legacy_path spell them, whatever form the file
    system keeps the name in. Symbolic links are left out, so nothing outside the directory is read.
    """
    root = LEGACY_DIRECTORY
    files: dict[str, Path] = {}
    for directory, _subdirectories, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            legacy_path = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
            files[legacy_path] = path
    return files


def read_legacy_file(legacy_path: str, files: Mapping[str, Path]) -> bytes:
    """The file's bytes; raises LegacyFileMissing or LegacyFileUnusable."""
    path = files.get(legacy_path)
    if path is None:
        raise LegacyFileMissing(f"not in {LEGACY_DIRECTORY}")
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise LegacyFileUnusable(f"more than {MAX_FILE_BYTES} bytes")
        return path.read_bytes()
    except OSError as exc:
        raise LegacyFileMissing(f"{path}: {exc}") from exc


def _out_of_time(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


# ---------------------------------------------------------------------------------- references
def _normalize(rest: str) -> str | None:
    """The file below static/legacy/image/ that `rest` names, or None if it is not a plain path."""
    # NFC, as legacy_files spells the names: "å" typed as "a" + a combining ring is the same file.
    path = unicodedata.normalize("NFC", unquote(rest)).strip().replace("\\", "/")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    normalized = "/".join(parts)
    # A control character (PostgreSQL text cannot even hold a NUL) or an overlong path is no file of
    # the old site; taken as one, it would break every run when it is recorded.
    if len(normalized) > MAX_PATH_LENGTH or any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized):
        return None
    return normalized


def references(text: str) -> list[list[str]]:
    """The legacy references in `text`, each as its candidate paths below static/legacy/image/.

    Usually one candidate. A reference inside a CSS url(…) may have more — with and without the end
    of the CSS it swallowed (see _options) — because a file name may contain ")" itself; the file
    decides which it is.
    """
    found: list[list[str]] = []
    for match in LEGACY_REF_RE.finditer(text or ""):
        candidates = _candidates(match.group("rest"))
        if candidates and candidates not in found:
            found.append(candidates)
    return found


def _options(rest: str) -> list[str]:
    """`rest` itself, then each shorter piece of it that ends right before a ")" or "&" and still
    looks like a file name (has an extension): the swallowed end of a CSS url(), cut off. Longest
    first, because a file name may contain ")" itself ("Hall(1).png")."""
    options = [rest]
    for cut in range(len(rest) - 1, 0, -1):
        if rest[cut] in ")&" and PurePosixPath(rest[:cut]).suffix:
            options.append(rest[:cut])
    return options


def _candidates(rest: str) -> list[str]:
    """The paths `rest` may name (see _options), normalized; each once."""
    paths: list[str] = []
    for option in _options(rest):
        path = _normalize(option)
        if path and path not in paths:
            paths.append(path)
    return paths


def _image_candidates(candidates: list[str]) -> list[str]:
    """The candidates with a file extension the library takes: only those are worth asking for."""
    return [path for path in candidates if PurePosixPath(path).suffix.lower() in EXTENSION_TYPES]


def _name_of(candidates: list[str]) -> str:
    """The one path a reference is reported and recorded under."""
    images = _image_candidates(candidates)
    return images[-1] if images else candidates[-1]


def rewrite(text: str, url_for: Callable[[str], str | None]) -> str:
    """`text` with each legacy reference that `url_for` knows replaced by the URL it returns."""

    def _replace(match: re.Match[str]) -> str:
        rest = match.group("rest")
        for option in _options(rest):
            path = _normalize(option)
            new_url = url_for(path) if path else None
            if new_url:
                return new_url + rest[len(option) :]
        return match.group(0)

    return LEGACY_REF_RE.sub(_replace, text or "")


Row = tuple[type[Model], tuple[str, ...], Model]


def _rows_with_references() -> list[Row]:
    """Every row that may hold a legacy reference: one query per model, the text fields only."""
    rows: list[Row] = []
    for model, fields in TEXT_FIELDS:
        query = Q()
        for name in fields:
            query |= Q(**{f"{name}__icontains": "public/image/"}) | Q(
                **{f"{name}__icontains": "legacy/image/"}
            )
        rows.extend((model, fields, row) for row in model._default_manager.filter(query).only("pk", *fields))
    return rows


def _wanted(rows: Iterable[Row]) -> list[list[str]]:
    wanted: list[list[str]] = []
    for _model, fields, row in rows:
        for name in fields:
            for candidates in references(getattr(row, name)):
                if candidates not in wanted:
                    wanted.append(candidates)
    return wanted


# -------------------------------------------------------------------------------------- import
# Called after each file a run asks for: (how many so far, of how many, its legacy path, the outcome).
Progress = Callable[[int, int, str, str], None]


@dataclass
class ConversionResult:
    imported: list[str] = field(default_factory=list)  # legacy paths imported this run
    rewritten: int = 0  # rows whose references were rewritten
    missing: list[str] = field(default_factory=list)  # referenced or listed, but no such file (recorded)
    unusable: list[str] = field(default_factory=list)  # a file, but not an image gahk.dk can show
    details: dict[str, str] = field(default_factory=dict)  # why, for each missing or unusable one
    skipped: list[str] = field(default_factory=list)  # failed in an earlier run, not asked again
    deferred: int = 0  # files left for a later run (budget spent, or an error)
    error: str = ""  # an unexpected failure (S3, the database) that stopped the downloads
    busy: bool = False  # another import was running, so this one did nothing

    def summary(self) -> dict[str, Any]:
        return {
            "imported": len(self.imported),
            "rewritten": self.rewritten,
            "missing": len(self.missing),
            "unusable": len(self.unusable),
            "deferred": self.deferred,
            "error": self.error,
            "busy": self.busy,
        }


@contextmanager
def _import_lock() -> Iterator[bool]:
    """Hold the import lock if it is free; yields whether it was taken. Session-level, so it is
    released in `finally` even when the worker process lives on."""
    if connection.vendor != "postgresql":
        yield True
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [IMPORT_LOCK_ID])
        taken = bool(cursor.fetchone()[0])
    try:
        yield taken
    finally:
        if taken:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [IMPORT_LOCK_ID])


def _caption(legacy_path: str) -> str:
    """Alt text from the file name, when it says something (not "10355627_1015275…_o")."""
    stem = Path(legacy_path).stem
    words = " ".join(re.sub(r"\(\d+\)$", "", stem).replace("_", " ").replace("-", " ").split())
    return words if re.search(r"[^\W\d_]{3,}", words) else ""


def _decodes(data: bytes) -> bool:
    """Whether Pillow reads `data` as an intact image. Checked on its own, because prepare_image
    keeps an animated GIF as it is, without decoding it."""
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
    except DECODE_ERRORS:
        return False
    return True


def _record_failure(
    legacy_path: str, reason: str, detail: str, candidates: list[str], result: ConversionResult
) -> None:
    logger.warning("Legacy image %s cannot be imported (%s: %s); left as it is", legacy_path, reason, detail)
    LegacyImageFailure.objects.filter(legacy_path__in=candidates).exclude(legacy_path=legacy_path).delete()
    LegacyImageFailure.objects.update_or_create(
        legacy_path=legacy_path, defaults={"reason": reason, "detail": detail[:255]}
    )
    (result.missing if reason == LegacyImageFailure.Reason.MISSING else result.unusable).append(legacy_path)
    result.details[legacy_path] = detail


def _fetch(candidates: list[str], files: Mapping[str, Path]) -> tuple[str, bytes]:
    """The first candidate there is a file for, with its bytes. Raises like read_legacy_file."""
    images = _image_candidates(candidates)
    if not images:
        raise LegacyFileUnusable("not a JPEG, PNG, GIF or WebP file name")
    for path in images:
        try:
            return path, read_legacy_file(path, files)
        except LegacyFileMissing:
            continue
    raise LegacyFileMissing(f"not in {LEGACY_DIRECTORY}")


def _import(candidates: list[str], files: Mapping[str, Path], result: ConversionResult) -> CmsImage | None:
    """Bring one file into the library; None when it is missing or unusable (recorded)."""
    from .services import record_change, store_prepared_image

    try:
        path, data = _fetch(candidates, files)
    except LegacyFileMissing as exc:
        _record_failure(_name_of(candidates), LegacyImageFailure.Reason.MISSING, str(exc), candidates, result)
        return None
    except LegacyFileUnusable as exc:
        _record_failure(
            _name_of(candidates), LegacyImageFailure.Reason.UNUSABLE, str(exc), candidates, result
        )
        return None
    try:
        if not _decodes(data):
            raise ImageRejected("Pillow cannot decode it")
        prepared = prepare_image(BytesIO(data))
    except ImageRejected as exc:
        _record_failure(path, LegacyImageFailure.Reason.UNUSABLE, str(exc), candidates, result)
        return None
    with transaction.atomic():
        image = store_prepared_image(
            prepared, PurePosixPath(path).name, _caption(path), None, legacy_path=path
        )
        record_change(
            action=ContentChange.Action.UPLOAD,
            kind=ContentChange.Kind.IMAGE,
            object_id=image.pk,
            label=str(image),
            changes={"file": [f"{OLD_URL_PREFIX}{path}", image.file.name]},
            by_name=SYSTEM_NAME,
        )
        # Also a "deleted" record: content refers to the file again, so it is back in the library.
        LegacyImageFailure.objects.filter(legacy_path__in=candidates).delete()
    result.imported.append(path)
    return image


@dataclass
class _Plan:
    """What an import run has to do, and what the overview reports."""

    rows: list[Row]  # the rows that may hold a legacy reference
    referenced: set[str]  # every candidate path of their references
    files: dict[str, Path]  # every file below LEGACY_DIRECTORY, by legacy path
    listed: list[str]  # the ones below upload/: every one is imported
    library: dict[str, CmsImage]  # legacy path -> library image, for every path in question
    recorded: dict[str, str]  # legacy path -> LegacyImageFailure.reason, for every path in question
    todo: list[list[str]]  # what to import, each as its candidate paths: the references first
    skipped: list[str]  # failed in an earlier run, left out this time


def _plan(retry_failed: bool) -> _Plan:
    rows = _rows_with_references()
    wanted = _wanted(rows)
    files = legacy_files()
    listed = sorted(path for path in files if path.startswith(LISTED_DIRECTORY))
    referenced = {path for candidates in wanted for path in candidates}
    in_question = sorted(referenced | set(listed))
    library = {
        image.legacy_path: image
        for image in CmsImage.objects.filter(legacy_path__in=in_question)
        if image.legacy_path
    }
    recorded = dict(
        LegacyImageFailure.objects.filter(legacy_path__in=in_question).values_list("legacy_path", "reason")
    )
    deleted = LegacyImageFailure.Reason.DELETED

    def failed(path: str) -> bool:
        return not retry_failed and path in recorded and recorded[path] != deleted

    todo: list[list[str]] = []
    skipped: list[str] = []
    # What the content refers to, even a file an editor deleted from the library before: it is in
    # use again. Then the rest of the list, without the deleted ones.
    queue = wanted + [[path] for path in listed if path not in referenced]
    for candidates in queue:
        if any(path in library for path in candidates):
            continue
        if any(failed(path) for path in candidates):
            skipped.append(_name_of(candidates))
            continue
        if candidates[0] not in referenced and recorded.get(candidates[0]) == deleted:
            continue
        todo.append(candidates)
    return _Plan(rows, referenced, files, listed, library, recorded, todo, skipped)


def convert_legacy_images(
    *,
    retry_failed: bool = True,
    max_files: int | None = None,
    seconds: float | None = None,
    progress: Progress | None = None,
) -> ConversionResult:
    """Import every file below upload/ and every referenced legacy image into the library, and point
    the content at them.

    `retry_failed=False` leaves out the files an earlier run recorded as missing or unusable (never
    asked again: the deleted ones). `max_files` and `seconds` bound the imports; what is left is
    counted in `deferred`. `progress` hears of each file asked for. Does nothing (`busy`) while
    another import runs.
    """
    with _import_lock() as taken:
        if not taken:
            return ConversionResult(busy=True)
        return _convert(retry_failed, max_files, seconds, progress)


def import_pending_legacy_images() -> ConversionResult:
    """The automatic run (cms.tasks.sync_gahk_web): bounded, recorded failures left out."""
    return convert_legacy_images(retry_failed=False, max_files=AUTO_MAX_FILES, seconds=AUTO_SECONDS)


def _outcome(result: ConversionResult, path: str) -> str:
    if result.imported and result.imported[-1] == path:
        return "imported"
    if path in result.details:
        kind = "missing" if path in result.missing else "not a usable image"
        return f"{kind} ({result.details[path]})"
    return "done"


def _convert(
    retry_failed: bool,
    max_files: int | None,
    seconds: float | None,
    progress: Progress | None,
) -> ConversionResult:
    deadline = time.monotonic() + seconds if seconds is not None else None
    result = ConversionResult()
    plan = _plan(retry_failed)
    result.skipped = plan.skipped
    library, todo = plan.library, plan.todo
    if not todo and not plan.rows:
        return result

    # Two references may name the same file (an unquoted url() and a plain src): it is asked for once.
    asked: set[str] = set()

    def settled(candidates: list[str]) -> bool:
        return any(path in library or path in asked for path in candidates)

    imports = 0
    for index, candidates in enumerate(todo):
        if settled(candidates):
            continue
        if (max_files is not None and imports >= max_files) or _out_of_time(deadline):
            result.deferred = sum(1 for left in todo[index:] if not settled(left))
            break
        imports += 1
        asked.update(candidates)
        try:
            image = _import(candidates, plan.files, result)
        except Exception as exc:
            # Storage or the database, not the file: the next run tries it again. What this run did
            # import is still pointed at below, so a stubborn error never holds that back.
            logger.exception(
                "Legacy image import stopped at %s, to go on in a later run", _name_of(candidates)
            )
            result.error = f"{_name_of(candidates)}: {exc.__class__.__name__}: {exc}"[:500]
        else:
            name = image.legacy_path if image is not None and image.legacy_path else _name_of(candidates)
            if image is not None and image.legacy_path:
                library[image.legacy_path] = image
            if progress is not None:
                progress(index + 1, len(todo), name, _outcome(result, name))
            continue
        asked.difference_update(candidates)
        result.deferred = sum(1 for left in todo[index:] if not settled(left))
        break

    if not any(path in library for path in plan.referenced):
        return result  # nothing to point the content at: no rewrite, no transaction

    def url_for(path: str) -> str | None:
        image = library.get(path)
        return image.url if image is not None and image.file else None

    with transaction.atomic():
        _rewrite_rows(plan.rows, url_for, result)
    return result


def _label(row: Model) -> str:
    for name in ("header", "title"):
        if value := getattr(row, name, ""):
            return str(value)
    return str(row)


def _rewrite_rows(rows: list[Row], url_for: Callable[[str], str | None], result: ConversionResult) -> None:
    from .services import record_change

    versions: list[int] = []
    for model, fields, stale in rows:
        # The imports above take a while (a downscale and an S3 upload per file); an editor may have
        # saved this row meanwhile. Rewrite what is stored now, locked until the commit, never the
        # copy read before.
        row = model._default_manager.select_for_update().filter(pk=stale.pk).first()
        if row is None:
            continue
        changes: dict[str, list[str]] = {}
        for name in fields:
            before = getattr(row, name)
            after = rewrite(before, url_for)
            if after != before:
                setattr(row, name, after)
                changes[name] = [before, after]
        if not changes:
            continue
        row.save(update_fields=list(changes))
        result.rewritten += 1
        if model is PageVersion:
            versions.append(row.pk)
            continue
        record_change(
            action=ContentChange.Action.UPDATE,
            kind=KIND_OF[model],
            object_id=row.pk,
            label=_label(row) if model is not SiteSettings else "Indstillinger",
            changes=changes,
            by_name=SYSTEM_NAME,
        )
    if versions:
        # One entry for the history rather than one per old version: it is the same mechanical
        # rewrite, and the versions themselves still show what they said.
        record_change(
            action=ContentChange.Action.UPDATE,
            kind=ContentChange.Kind.PAGE,
            label=f"Billedadresser i {len(versions)} gamle sideversioner",
            changes={"page_versions": [None, versions]},
            by_name=SYSTEM_NAME,
        )


# ------------------------------------------------------------------------------------ overview
@dataclass
class LegacyState:
    total: int  # files below upload/ (all of the old site's images)
    in_library: int  # of those, in the library now
    deleted: int  # of those, deleted from the library by an editor: not imported again
    waiting: int  # files and references not in the library and not failed: the next runs import them
    failures: list[LegacyImageFailure]  # what could not be imported (not the deleted ones)


def legacy_state() -> LegacyState:
    """For the Hjemmeside overview: how far the import is, and what it could not do."""
    plan = _plan(retry_failed=False)
    deleted = LegacyImageFailure.Reason.DELETED
    failed = [path for path, reason in plan.recorded.items() if reason != deleted]
    return LegacyState(
        total=len(plan.listed),
        in_library=sum(1 for path in plan.listed if path in plan.library),
        deleted=sum(
            1 for path in plan.listed if path not in plan.library and plan.recorded.get(path) == deleted
        ),
        waiting=len(plan.todo),
        failures=list(LegacyImageFailure.objects.filter(legacy_path__in=failed)),
    )


def legacy_image_map() -> dict[str, str]:
    """Each imported image's old public URL (/static/legacy/image/<rest>) -> its gahk.dk URL.

    gahk.dk answers the old URLs (and the /public/image/ ones) with a 301 to the copy, so links to
    the old images from other websites keep working.
    """
    from .snapshot import image_entry

    mapping: dict[str, str] = {}
    for image in CmsImage.objects.filter(legacy_path__isnull=False).order_by("legacy_path"):
        entry = image_entry(image)
        if entry is not None and image.legacy_path:
            mapping[f"{OLD_URL_PREFIX}{image.legacy_path}"] = entry["url"]
    return mapping
