"""Moving the old site's images into the image library, once.

The legacy site kept its content images as files (/public/image/…), and this project carried them
on in git under static/legacy/image/. They are content, not code: they belong in the image library
(CmsImage, in S3) like every other picture, and gahk.dk gets them from there.

`convert_legacy_images()` does the move. For every legacy image reference in the stored content —

    /public/image/<rest>   public/image/<rest>   http(s)://(www.)gahk.dk/public/image/<rest>
    /static/legacy/image/<rest>   (and the same with http(s)://(www.)gahk.dk in front)

— in Page.body/background_image, NewsItem.body, Event.description, PageVersion.body/
background_image and SiteSettings.default_background, it imports the file static/legacy/image/<rest>
into the library once (downscaled like an upload, `legacy_path` = <rest>, uploaded by nobody) and
rewrites the reference to the library URL. Every rewrite is recorded as a system ContentChange.

It runs at the start of every publish (cms.tasks.publish_gahk_web), so production converts itself
on its first publish and every later run finds nothing to do. A file that is missing or is not a
usable image is logged and its references are left as they are.

TEMPORARY. Once this has run in production, a follow-up removes static/legacy/image/upload/ and this
module (DEPLOY.md, "gahk.dk (gahk_web)"). static/legacy/image/elements/ and intern/ stay: the
intranet itself uses them.
"""

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from django.conf import settings
from django.db import transaction
from django.db.models import Model, Q

from .images import ImageRejected, prepare_image
from .models import CmsImage, ContentChange, Event, NewsItem, Page, PageVersion, SiteSettings

logger = logging.getLogger(__name__)

SYSTEM_NAME = "System (gamle billeder flyttet til billedbiblioteket)"
OLD_URL_PREFIX = "/static/legacy/image/"

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


@dataclass
class ConversionResult:
    imported: list[str] = field(default_factory=list)  # legacy paths imported this run
    rewritten: int = 0  # rows whose references were rewritten
    missing: list[str] = field(default_factory=list)  # referenced, but no such file
    unusable: list[str] = field(default_factory=list)  # a file, but not an image gahk.dk can show


def legacy_image_dirs() -> list[Path]:
    """Where the legacy files are: collected static files in production, app/static in dev."""
    return [Path(directory) for directory in settings.CMS_LEGACY_IMAGE_DIRS]


def _normalize(rest: str) -> str | None:
    """The file below static/legacy/image/ that `rest` names, or None if it is not a plain path."""
    path = unquote(rest).strip().replace("\\", "/")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _find_file(legacy_path: str) -> Path | None:
    for directory in legacy_image_dirs():
        candidate = directory / legacy_path
        try:
            if candidate.is_file() and candidate.resolve().is_relative_to(directory.resolve()):
                return candidate
        except OSError:
            continue
    return None


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


def _caption(legacy_path: str) -> str:
    """Alt text from the file name, when it says something (not "10355627_1015275…_o")."""
    stem = Path(legacy_path).stem
    words = " ".join(re.sub(r"\(\d+\)$", "", stem).replace("_", " ").replace("-", " ").split())
    return words if re.search(r"[^\W\d_]{3,}", words) else ""


def _import(legacy_path: str, source: Path, result: ConversionResult) -> CmsImage | None:
    from .services import record_change, store_prepared_image

    try:
        with source.open("rb") as handle:
            prepared = prepare_image(handle)
    except (ImageRejected, OSError) as exc:
        logger.warning("Legacy image %s cannot be imported (%s); left as it is", legacy_path, exc)
        result.unusable.append(legacy_path)
        return None
    image = store_prepared_image(prepared, source.name, _caption(legacy_path), None, legacy_path=legacy_path)
    record_change(
        action=ContentChange.Action.UPLOAD,
        kind=ContentChange.Kind.IMAGE,
        object_id=image.pk,
        label=str(image),
        changes={"file": [f"{OLD_URL_PREFIX}{legacy_path}", image.file.name]},
        by_name=SYSTEM_NAME,
    )
    result.imported.append(legacy_path)
    return image


def _rows_with_references(model: type[Model], fields: Iterable[str]) -> list[Model]:
    query = Q()
    for name in fields:
        query |= Q(**{f"{name}__icontains": "public/image/"}) | Q(**{f"{name}__icontains": "legacy/image/"})
    return list(model._default_manager.filter(query))


def _label(row: Model) -> str:
    for name in ("header", "title"):
        if value := getattr(row, name, ""):
            return str(value)
    return str(row)


def convert_legacy_images() -> ConversionResult:
    """Import every referenced legacy image into the library and point the content at it.

    Idempotent: a second run finds no references (or only ones whose file is missing).
    """
    result = ConversionResult()
    rows = [
        (model, fields, row) for model, fields in TEXT_FIELDS for row in _rows_with_references(model, fields)
    ]
    wanted: list[list[str]] = []
    for _model, fields, row in rows:
        for name in fields:
            for candidates in references(getattr(row, name)):
                if candidates not in wanted:
                    wanted.append(candidates)
    if not wanted:
        return result

    with transaction.atomic():
        every_candidate = [path for candidates in wanted for path in candidates]
        library = {
            image.legacy_path: image for image in CmsImage.objects.filter(legacy_path__in=every_candidate)
        }
        for candidates in wanted:
            if any(path in library for path in candidates):
                continue
            found = next(((path, file) for path in candidates if (file := _find_file(path))), None)
            if found is None:
                if candidates[0] not in result.missing:
                    logger.warning(
                        "Legacy image %s is referenced but not found; left as it is", candidates[0]
                    )
                    result.missing.append(candidates[0])
                continue
            path, source = found
            if (image := _import(path, source, result)) is not None:
                library[path] = image

        def url_for(path: str) -> str | None:
            image = library.get(path)
            return image.url if image is not None and image.file else None

        _rewrite_rows(rows, url_for, result)
    return result


def _rewrite_rows(
    rows: list[tuple[type[Model], tuple[str, ...], Model]],
    url_for: Callable[[str], str | None],
    result: ConversionResult,
) -> None:
    from .services import record_change

    versions: list[int] = []
    for model, fields, row in rows:
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
