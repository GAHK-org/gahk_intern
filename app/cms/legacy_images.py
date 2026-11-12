"""Moving the old site's images into the image library, by itself.

The legacy site kept its content images as files (/public/image/…), and this project used to carry
them in git under static/legacy/image/upload/. They are content, not code: they belong in the image
library (CmsImage, in S3) like every other picture, and gahk.dk gets them from there. The repository
no longer holds them, but its public history does, at LEGACY_SOURCE_COMMIT, and GitHub serves any
file of any commit: that is where the import downloads them from.

For every legacy image reference in the stored content —

    /public/image/<rest>   public/image/<rest>   http(s)://(www.)gahk.dk/public/image/<rest>
    /static/legacy/image/<rest>   (and the same with http(s)://(www.)gahk.dk in front)

— in Page.body/background_image, NewsItem.body, Event.description, PageVersion.body/
background_image and SiteSettings.default_background, the file <rest> (below the old
static/legacy/image/) is imported into the library once (checked and downscaled like an upload,
`legacy_path` = <rest>, uploaded by nobody), and the reference is rewritten to the library URL. Every
import and every rewrite is recorded as a system ContentChange.

It runs by itself: cms.tasks.sync_gahk_web calls `import_pending_legacy_images` every 2 minutes,
before it builds the snapshot. A run that finds no reference does nothing (a few queries, no HTTP).
Otherwise it downloads at most AUTO_MAX_FILES files in at most AUTO_SECONDS and leaves the rest to
the next run, so the outbox pull behind it is never held up for long. A file that does not exist or
is not a usable image is logged, recorded as a LegacyImageFailure and not asked for again; its
references stay as they are (gahk.dk shows such a picture as broken, such a background as a plain
band). A network problem is recorded nowhere: the next run simply tries again.

`manage.py import_legacy_images` runs the same import by hand, without the budget and retrying the
recorded failures; `--from DIR` reads a local copy instead of GitHub.

Idempotent, and one run at a time (a PostgreSQL advisory lock): a second run finds the images in
the library and the content already rewritten.

TEMPORARY. Once production has no legacy reference left (the Hjemmeside overview shows none), this
module, the command and LegacyImageFailure can go; `legacy_path` stays, because gahk.dk's redirects
for old image links are built from it (legacy_image_map).
"""

import logging
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import quote, unquote

import requests
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

# The last commit on main ("Dev to main (#381)") that still carried the old site's image files under
# app/static/legacy/image/ (upload/ left git afterwards: content belongs in S3). The repository is
# public, and raw.githubusercontent.com serves any file of any commit. Pinned by the full sha,
# because a sha always names the same files, where a branch moves on and no longer has them.
LEGACY_SOURCE_COMMIT = "3fc1817b0d5ef07a2b636b4a01c892ba3fe3a8ba"
LEGACY_SOURCE_URL = (
    f"https://raw.githubusercontent.com/GAHK-org/gahk_intern/{LEGACY_SOURCE_COMMIT}/app/static/legacy/image/"
)
DOWNLOAD_TIMEOUT = (5, 20)  # seconds: to connect, and between two reads
MAX_FILE_BYTES = 25 * 1024 * 1024  # the largest old image is 13 MB
USER_AGENT = "intern.gahk.dk legacy image import"

# What one automatic run may spend before it leaves the rest to the next one, 2 minutes later.
AUTO_MAX_FILES = 20
AUTO_SECONDS = 60

# pg advisory lock id for "a legacy image import is running" ("gahkleg" in ASCII). Not cms.tasks'
# publish lock: a publish need not wait for an import, only two imports for each other.
IMPORT_LOCK_ID = 0x6761686B6C6567

# A legacy reference, wherever it sits: an attribute, a CSS url(), or the whole of a background
# field. The look-behind keeps it from matching inside a longer URL (example.com/public/image/…).
# `rest` stops at what cannot be part of a URL in an attribute; a ")" it swallows from an unquoted
# CSS url() is given back below when the file name does not end in one.
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

ACCEPTED_TYPES = frozenset(EXTENSION_TYPES.values())


# ------------------------------------------------------------------------------------- sources
class LegacyFileMissing(Exception):
    """The source has no such file."""


class LegacyFileUnusable(Exception):
    """The source has the file, but it is not an image the library takes."""


class LegacySourceUnavailable(Exception):
    """The source could not be asked (network, rate limit, server error, out of time): try later."""


class LegacySource(Protocol):
    @property
    def description(self) -> str: ...

    def read(self, legacy_path: str, deadline: float | None) -> bytes:
        """The file's bytes; raises LegacyFileMissing, LegacyFileUnusable or LegacySourceUnavailable."""
        ...


def http_get(url: str, *, timeout: tuple[float, float], **kwargs: Any) -> requests.Response:  # noqa: ANN401
    """requests.get, under a name of this module's own, which the tests replace (they never download)."""
    return requests.get(url, timeout=timeout, **kwargs)


def _out_of_time(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


@dataclass
class GitHubSource:
    """The files as the public repository held them at LEGACY_SOURCE_COMMIT."""

    base_url: str = LEGACY_SOURCE_URL

    @property
    def description(self) -> str:
        return self.base_url

    def read(self, legacy_path: str, deadline: float | None) -> bytes:
        url = self.base_url + quote(legacy_path, safe="/")
        try:
            with http_get(
                url, stream=True, timeout=DOWNLOAD_TIMEOUT, headers={"User-Agent": USER_AGENT}
            ) as response:
                if response.status_code in (404, 410):
                    raise LegacyFileMissing(f"GitHub answered {response.status_code}")
                if response.status_code != 200:
                    raise LegacySourceUnavailable(f"GitHub answered {response.status_code} for {url}")
                content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if content_type not in ACCEPTED_TYPES:
                    raise LegacyFileUnusable(f"content type {content_type or '(none)'}")
                length = response.headers.get("Content-Length", "")
                if length.isdigit() and int(length) > MAX_FILE_BYTES:
                    raise LegacyFileUnusable(f"{length} bytes, more than {MAX_FILE_BYTES}")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise LegacyFileUnusable(f"more than {MAX_FILE_BYTES} bytes")
                    if _out_of_time(deadline):
                        raise LegacySourceUnavailable("out of time for this run")
                    chunks.append(chunk)
        except requests.RequestException as exc:
            raise LegacySourceUnavailable(f"{url}: {exc}") from exc
        return b"".join(chunks)


@dataclass
class DirectorySource:
    """Local copies of the old static/legacy/image/ (or public/image/) directory, the one that holds
    upload/. The first directory that has the file wins."""

    directories: Sequence[Path]

    @property
    def description(self) -> str:
        return ", ".join(str(directory) for directory in self.directories)

    def read(self, legacy_path: str, deadline: float | None) -> bytes:
        for directory in self.directories:
            candidate = Path(directory) / legacy_path
            try:
                if not candidate.is_file() or not candidate.resolve().is_relative_to(
                    Path(directory).resolve()
                ):
                    continue
                if candidate.stat().st_size > MAX_FILE_BYTES:
                    raise LegacyFileUnusable(f"more than {MAX_FILE_BYTES} bytes")
                return candidate.read_bytes()
            except OSError:
                continue
        raise LegacyFileMissing(f"not in {self.description}")


# ---------------------------------------------------------------------------------- references
def _normalize(rest: str) -> str | None:
    """The file below static/legacy/image/ that `rest` names, or None if it is not a plain path."""
    path = unquote(rest).strip().replace("\\", "/")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def references(text: str) -> list[list[str]]:
    """The legacy references in `text`, each as its candidate paths below static/legacy/image/.

    Usually one candidate. A reference inside an unquoted CSS url(…) has two — with and without the
    ")" it swallowed — because a file name may end in ")" itself; the file decides which it is.
    """
    found: list[list[str]] = []
    for match in LEGACY_REF_RE.finditer(text or ""):
        candidates = _candidates(match.group("rest"))
        if candidates and candidates not in found:
            found.append(candidates)
    return found


def _candidates(rest: str) -> list[str]:
    """`rest` itself and, for an unquoted CSS url(), `rest` without the ")" it swallowed."""
    options = [rest]
    stripped = rest
    while stripped.endswith(")"):
        stripped = stripped[:-1]
        options.append(stripped)
    return [path for option in options if (path := _normalize(option))]


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
        trimmed = rest
        while True:
            path = _normalize(trimmed)
            new_url = url_for(path) if path else None
            if new_url:
                return new_url + rest[len(trimmed) :]
            if not trimmed.endswith(")"):
                return match.group(0)
            trimmed = trimmed[:-1]

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
@dataclass
class ConversionResult:
    imported: list[str] = field(default_factory=list)  # legacy paths imported this run
    rewritten: int = 0  # rows whose references were rewritten
    missing: list[str] = field(default_factory=list)  # referenced, but no such file (recorded)
    unusable: list[str] = field(default_factory=list)  # a file, but not an image gahk.dk can show
    skipped: list[str] = field(default_factory=list)  # failed in an earlier run, not asked again
    deferred: int = 0  # references left for a later run (budget spent, or source unavailable)
    unavailable: str = ""  # why the source could not be asked, when it could not
    busy: bool = False  # another import was running, so this one did nothing

    def summary(self) -> dict[str, Any]:
        return {
            "imported": len(self.imported),
            "rewritten": self.rewritten,
            "missing": len(self.missing),
            "unusable": len(self.unusable),
            "deferred": self.deferred,
            "unavailable": self.unavailable,
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


def _fetch(candidates: list[str], source: LegacySource, deadline: float | None) -> tuple[str, bytes]:
    """The first candidate the source has, with its bytes. Raises like LegacySource.read."""
    images = _image_candidates(candidates)
    if not images:
        raise LegacyFileUnusable("not a JPEG, PNG, GIF or WebP file name")
    for path in images:
        try:
            return path, source.read(path, deadline)
        except LegacyFileMissing:
            continue
    raise LegacyFileMissing(f"not in {source.description}")


def _import(
    candidates: list[str], source: LegacySource, deadline: float | None, result: ConversionResult
) -> CmsImage | None:
    """Bring one referenced file into the library; None when it is missing or unusable (recorded).
    LegacySourceUnavailable passes through: nothing is recorded, and the caller stops the run."""
    from .services import record_change, store_prepared_image

    try:
        path, data = _fetch(candidates, source, deadline)
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
        LegacyImageFailure.objects.filter(legacy_path__in=candidates).delete()
    result.imported.append(path)
    return image


def convert_legacy_images(
    source: LegacySource | None = None,
    *,
    retry_failed: bool = True,
    max_files: int | None = None,
    seconds: float | None = None,
) -> ConversionResult:
    """Import the referenced legacy images from `source` (GitHub by default) into the library and
    point the content at them.

    `retry_failed=False` leaves out the files an earlier run recorded as missing or unusable.
    `max_files` and `seconds` bound the downloads; what is left is counted in `deferred`. Does
    nothing (`busy`) while another import runs.
    """
    with _import_lock() as taken:
        if not taken:
            return ConversionResult(busy=True)
        return _convert(source or GitHubSource(), retry_failed, max_files, seconds)


def import_pending_legacy_images() -> ConversionResult:
    """The automatic run (cms.tasks.sync_gahk_web): from GitHub, bounded, recorded failures left out."""
    return convert_legacy_images(retry_failed=False, max_files=AUTO_MAX_FILES, seconds=AUTO_SECONDS)


def _convert(
    source: LegacySource, retry_failed: bool, max_files: int | None, seconds: float | None
) -> ConversionResult:
    deadline = time.monotonic() + seconds if seconds is not None else None
    result = ConversionResult()
    rows = _rows_with_references()
    wanted = _wanted(rows)
    if not wanted:
        return result

    every_candidate = [path for candidates in wanted for path in candidates]
    library = {image.legacy_path: image for image in CmsImage.objects.filter(legacy_path__in=every_candidate)}
    failed: set[str] = set()
    if not retry_failed:
        failed = set(
            LegacyImageFailure.objects.filter(legacy_path__in=every_candidate).values_list(
                "legacy_path", flat=True
            )
        )
    todo: list[list[str]] = []
    for candidates in wanted:
        if any(path in library for path in candidates):
            continue
        if any(path in failed for path in candidates):
            result.skipped.append(_name_of(candidates))
            continue
        todo.append(candidates)

    for index, candidates in enumerate(todo):
        if (max_files is not None and index >= max_files) or _out_of_time(deadline):
            result.deferred = len(todo) - index
            break
        try:
            image = _import(candidates, source, deadline, result)
        except LegacySourceUnavailable as exc:
            logger.warning("Legacy image import stopped, to go on in a later run: %s", exc)
            result.unavailable = str(exc)[:500]
            result.deferred = len(todo) - index
            break
        if image is not None and image.legacy_path:
            library[image.legacy_path] = image

    if not library:
        return result  # nothing to point the content at: no rewrite, no transaction

    def url_for(path: str) -> str | None:
        image = library.get(path)
        return image.url if image is not None and image.file else None

    with transaction.atomic():
        _rewrite_rows(rows, url_for, result)
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
        # The imports above take a while (a download and an S3 upload per file); an editor may have
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
    waiting: int  # references not in the library and not failed: the next runs import them
    failures: list[LegacyImageFailure]


def legacy_state() -> LegacyState:
    """For the Hjemmeside overview: what the import still has to do, and what it could not do."""
    wanted = _wanted(_rows_with_references())
    every_candidate = [path for candidates in wanted for path in candidates]
    done = set(CmsImage.objects.filter(legacy_path__in=every_candidate).values_list("legacy_path", flat=True))
    failures = list(LegacyImageFailure.objects.filter(legacy_path__in=every_candidate))
    failed = {failure.legacy_path for failure in failures}
    waiting = sum(
        1 for candidates in wanted if not any(path in done or path in failed for path in candidates)
    )
    return LegacyState(waiting=waiting, failures=failures)


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
