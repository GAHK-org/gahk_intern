"""The content snapshot intern publishes to gahk.dk: everything the public site shows, as one JSON
document (format 1, the contract in the gahk.dk split design; gahk_web's snapshot.py validates it).

What is sent, and how it differs from what is stored:

  * Pages with an address, plus the front page (id 1), which is always at path "" — its own slug
    becomes a redirect to "/". Paths have no leading or trailing slash.
  * Images are content-addressed copies: /media/cms/<name> in bodies and backgrounds becomes
    /billeder/<sha256[:16]>/<slugified basename>, which gahk.dk serves without asking intern (or S3)
    anything. A background that is not a library image is sent as "" (gahk.dk refuses any other).
  * Links into the intranet (/intern/…, http(s)://(www.)gahk.dk/intern/…) point at intern.gahk.dk.
  * Legacy /public/image/… references are not sent at all: cms.legacy_images converts them in the
    database before every publish, and `legacy_images` maps their old URLs to the copies.

`revision` is the sha256 of the canonical JSON of everything except `revision` and `published_at`,
so it changes exactly when what gahk.dk would show changes. cms.tasks compares it with the revision
gahk.dk reports to decide whether to publish.

Anything that would make gahk.dk refuse the whole snapshot (an illegal or reserved path, a blank
title) is left out with a warning in the log instead, so one bad row cannot stop every publish.
"""

import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from .images import billeder_url, content_type_of, sha256_hex
from .models import FRONT_PAGE_ID, CmsImage, Event, MenuItem, NewsItem, Page, PageRedirect, SiteSettings
from .paths import is_reserved, validate_page_path

logger = logging.getLogger(__name__)

FORMAT = 1
MAX_IMAGE_BYTES = 15 * 1024 * 1024  # gahk.dk refuses anything larger
MAX_DESCRIPTION = 300

# A library image URL: /media/cms/…, also written absolute on one of our hosts.
MEDIA_REF_RE = re.compile(
    r"(?<![\w./:-])(?:https?://(?:www\.|intern\.)?gahk\.dk)?/media/(?P<name>cms/[^\"'\s<>?#\\)]+)",
    re.IGNORECASE,
)
# A link into the intranet's old home on gahk.dk. `rest` starts at the "/", "?" or "#" after the
# prefix; without one the prefix must end the URL, so "/internat" is left alone.
INTERN_LINK_RE = re.compile(
    r"(?<![\w./:-])(?:https?://(?:www\.)?gahk\.dk)?/(?:ny)?intern"
    r"(?:(?P<rest>[/?#][^\"'\s<>]*)|(?=[\"'\s<>)]|$))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Snapshot:
    data: dict[str, Any]
    # sha256 -> library image, for every image in data["images"] (what publishing may upload).
    images: dict[str, CmsImage]

    @property
    def revision(self) -> str:
        return str(self.data["revision"])

    def body(self) -> bytes:
        return canonical_json(self.data)


def canonical_json(value: Any) -> bytes:  # noqa: ANN401 — any JSON value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_revision(data: Mapping[str, Any]) -> str:
    content = {key: value for key, value in data.items() if key not in ("revision", "published_at")}
    return hashlib.sha256(canonical_json(content)).hexdigest()


def _iso(moment: Any) -> str:  # noqa: ANN401 — a datetime
    return timezone.localtime(moment).isoformat()


def _plain(value: object) -> str:
    return " ".join(str(value or "").split())


# --------------------------------------------------------------------------------------- images
def ensure_image_metadata(image: CmsImage) -> bool:
    """Make sure `image` knows its sha256, content type and size, reading the file if it must.

    False when the image cannot go to gahk.dk: no file, unreadable, or not JPEG/PNG/GIF/WebP.
    """
    if not image.file:
        return False
    if not image.sha256:
        try:
            with image.file.open("rb") as handle:
                data = handle.read()
        except (OSError, ValueError):
            logger.warning("Library image %s (%s) cannot be read", image.pk, image.file.name)
            return False
        image.sha256 = sha256_hex(data)
        image.content_type = content_type_of(data) or ""
        image.size = len(data)
        CmsImage.objects.filter(pk=image.pk).update(
            sha256=image.sha256, content_type=image.content_type, size=image.size
        )
    return bool(image.content_type) and 0 < (image.size or 0) <= MAX_IMAGE_BYTES


def image_entry(image: CmsImage) -> dict[str, Any] | None:
    """The snapshot's `images` entry for `image`, or None when gahk.dk cannot have it."""
    if not ensure_image_metadata(image):
        return None
    return {
        "url": billeder_url(image.sha256, image.file.name or "", image.content_type),
        "sha256": image.sha256,
        "content_type": image.content_type,
        "size": image.size,
    }


def media_names(text: str) -> set[str]:
    """The storage names (cms/…) of the library images `text` refers to."""
    return {unquote(match.group("name")) for match in MEDIA_REF_RE.finditer(text or "")}


def rewrite_media(text: str, url_for: Callable[[str], str | None]) -> str:
    """`text` with each /media/cms/… reference that `url_for` knows replaced by its gahk.dk URL."""

    def _replace(match: re.Match[str]) -> str:
        return url_for(unquote(match.group("name"))) or match.group(0)

    return MEDIA_REF_RE.sub(_replace, text or "")


def rewrite_intern_links(text: str) -> str:
    """Links to the intranet's old home on gahk.dk, pointed at intern.gahk.dk (settings.SITE_URL)."""
    base = settings.SITE_URL.rstrip("/")

    def _replace(match: re.Match[str]) -> str:
        rest = match.group("rest") or "/"
        return f"{base}/{rest.lstrip('/')}" if rest.startswith("/") else f"{base}/{rest}"

    return INTERN_LINK_RE.sub(_replace, text or "")


# --------------------------------------------------------------------------------------- pieces
def _page_path(page: Page) -> str | None:
    """The path gahk.dk serves `page` at, or None when it is not published."""
    if page.pk == FRONT_PAGE_ID:
        return ""
    if not page.slug:
        return None
    try:
        validate_page_path(page.slug)
    except ValidationError:
        logger.warning("Page %s has the address %r, which gahk.dk cannot serve; left out", page.pk, page.slug)
        return None
    return page.slug


def page_item(page: Page, path: str, body: str, background: str) -> dict[str, Any]:
    return {
        "id": page.pk,
        "path": path,
        "title": _plain(page.header) or "Side",
        "body": body,
        "background": background,
        "description": _plain(page.description)[:MAX_DESCRIPTION],
        "updated_at": _iso(page.updated_at or timezone.now()),
    }


def news_item(item: NewsItem, body: str) -> dict[str, Any]:
    return {
        "id": item.pk,
        "title": _plain(item.title),
        "body": body,
        "published_at": _iso(item.published_at),
    }


def event_item(item: Event, description: str) -> dict[str, Any]:
    return {
        "id": item.pk,
        "title": _plain(item.title),
        "description": description,
        "starts_on": item.starts_on.isoformat(),
    }


def _menu() -> list[dict[str, str]]:
    from .services import clean_menu_url

    menu = []
    for item in MenuItem.objects.all():
        label = _plain(item.label)
        try:
            url = clean_menu_url(item.url)
        except ValidationError:
            logger.warning("Menu item %s has the link %r, which gahk.dk refuses; left out", item.pk, item.url)
            continue
        if label and len(label) <= 60:
            menu.append({"label": label, "url": url})
    return menu


# ---------------------------------------------------------------------------------------- build
def build_snapshot(published_at: Any = None) -> Snapshot:  # noqa: ANN401 — a datetime
    """The snapshot of what is in the database now. Reads image files only for rows missing their
    sha256 (once per image, ever); otherwise database-only, so it is cheap enough to build every
    couple of minutes just to compare revisions."""
    site = SiteSettings.load()
    pages = [(page, path) for page in Page.objects.order_by("pk") if (path := _page_path(page)) is not None]
    news = list(NewsItem.objects.order_by("-published_at", "-pk"))
    events = list(Event.objects.order_by("starts_on", "pk"))

    texts = [site.default_background]
    texts += [text for page, _path in pages for text in (page.body, page.background_image)]
    texts += [item.body for item in news] + [item.description for item in events]
    referenced = set().union(*(media_names(text) for text in texts))

    images: dict[str, CmsImage] = {}
    url_by_name: dict[str, str] = {}
    entries: dict[str, dict[str, Any]] = {}
    legacy_images: dict[str, str] = {}
    for image in CmsImage.objects.order_by("pk"):
        name = image.file.name or ""
        if name not in referenced and not image.legacy_path:
            continue
        entry = image_entry(image)
        if entry is None:
            continue
        url_by_name[name] = entry["url"]
        entries[entry["url"]] = entry
        images[image.sha256] = image
        if image.legacy_path:
            legacy_images[f"/static/legacy/image/{image.legacy_path}"] = entry["url"]

    def body(text: str) -> str:
        return rewrite_intern_links(rewrite_media(text, url_by_name.get))

    def background(value: str) -> str:
        names = media_names(value)
        return url_by_name.get(names.pop(), "") if len(names) == 1 else ""

    shipped_paths = {path for _page, path in pages}
    target_by_page = {page.pk: path for page, path in pages}
    redirects: dict[str, str] = {}
    front = next((page for page, path in pages if path == ""), None)
    if front is not None and front.slug and not is_reserved(front.slug):
        redirects[front.slug] = ""
    for redirect in PageRedirect.objects.order_by("old_path"):
        target = target_by_page.get(redirect.page_id)
        if target is None or redirect.old_path in shipped_paths:
            continue
        try:
            validate_page_path(redirect.old_path)
        except ValidationError:
            continue
        redirects[redirect.old_path] = target

    data: dict[str, Any] = {
        "format": FORMAT,
        "revision": "",
        "published_at": _iso(published_at or timezone.now()),
        "site": {"front_page_id": FRONT_PAGE_ID, "default_background": background(site.default_background)},
        "legacy_images": legacy_images,
        "pages": [
            page_item(page, path, body(page.body), background(page.background_image)) for page, path in pages
        ],
        "news": [news_item(item, body(item.body)) for item in news if _plain(item.title)],
        "events": [event_item(item, body(item.description)) for item in events if _plain(item.title)],
        "menu": _menu(),
        "redirects": redirects,
        "images": [entries[url] for url in sorted(entries)],
    }
    data["revision"] = compute_revision(data)
    return Snapshot(data=data, images=images)


def current_revision() -> str:
    return build_snapshot().revision


# -------------------------------------------------------------------------------------- preview
def preview_request(item: Page | NewsItem | Event) -> dict[str, Any]:
    """A POST /api/preview body for an unsaved (or saved) page, news item or event.

    The item's library images stay at intern (/media/cms/…, with media_base = intern's origin): a
    draft may use an image gahk.dk has not been sent yet, and the editor's browser, which is logged
    in to intern, can load it from there.
    """
    media_base = settings.SITE_URL.rstrip("/")
    # Never 0: gahk.dk wants a positive id, and an unsaved item has none yet.
    item_id = item.pk or 2**31 - 1
    if isinstance(item, Page):
        path = "" if item.pk == FRONT_PAGE_ID else (item.slug or "")
        try:
            if path:
                validate_page_path(path)
        except ValidationError:
            path = ""
        names = media_names(item.background_image)
        background = f"/media/{names.pop()}" if len(names) == 1 else ""
        page = page_item(item, path, rewrite_intern_links(item.body), background)
        return {"kind": "page", "item": {**page, "id": item_id}, "media_base": media_base}
    if isinstance(item, NewsItem):
        news = {
            "id": item_id,
            "title": _plain(item.title) or "Nyhed",
            "body": rewrite_intern_links(item.body),
            "published_at": _iso(item.published_at or timezone.now()),
        }
        return {"kind": "news", "item": news, "media_base": media_base}
    event = event_item(item, rewrite_intern_links(item.description))
    event["title"] = event["title"] or "Begivenhed"
    return {"kind": "event", "item": {**event, "id": item_id}, "media_base": media_base}
