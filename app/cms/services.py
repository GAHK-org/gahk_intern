"""Every write to the website's content, in one place: the Hjemmeside editor calls these (and cms.bin,
which puts things in the bin, restores them and deletes them for good) and nothing else writes
content at runtime. Nothing here deletes what an editor made: see cms.bin.

Each write does the same four things, in one transaction:

  1. cleans its input (HTML through cms.sanitize, plain text collapsed to one line, addresses
     through cms.paths) and raises a ValidationError keyed by field when something is wrong;
  2. saves, keeping a full PageVersion for pages so any earlier version can be restored;
  3. runs inside an audit event (audit.trail) naming what it does — "cms.page.updated" — whose
     fields, before and after, the trail captures as they are saved;
  4. publishes to gahk.dk once the transaction has committed (cms.tasks.publish_gahk_web).

Versions are written **explicitly**, never from a `post_save` signal: an import or a shell `save()`
must not look like an edit. The audit trail does record a shell or Django-admin write, as one with
no named action ("Uden handling"); the first edit afterwards still snapshots the database state
before it changes anything, so what an editor overwrites can always be got back.

Permissions are the caller's business (the views check CMS_EDITOR_ROLES, and administrator for
putting a page in the bin); these functions trust `by`.
"""

import datetime
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from audit import trail
from core.uploads import validate_image_upload
from residents.models import Resident

from .images import ImageRejected, PreparedImage, prepare_image, stored_stem
from .models import (
    FRONT_PAGE_ID,
    CmsImage,
    Event,
    MenuItem,
    NewsItem,
    Page,
    PageRedirect,
    PageVersion,
    SiteSettings,
)
from .paths import validate_page_path
from .sanitize import clean_html
from .snapshot import media_names

logger = logging.getLogger(__name__)

# Keeps history bounded by construction, so there is no pruning command and no cron to forget. At a
# handful of edits a year across ~20 pages this will never be reached; it exists so that a script
# looping over `save_page()` cannot quietly grow the table without limit.
VERSIONS_PER_PAGE_CAP = 100

MAX_MENU_ITEMS = 30
MENU_URL_FORBIDDEN = re.compile(r"[\x00-\x20\x7f\"'<>\\`]")

SITE = ("cms.site", None, "gahk.dk")


# ----------------------------------------------------------------------------------- publishing
def publish_on_commit(reason: str = "save", by: Resident | None = None) -> None:
    """Publish to gahk.dk once the current transaction commits (at once outside a transaction).

    After the commit, so the worker reads what was saved, and a rolled-back edit publishes nothing.
    A failure to queue is logged and swallowed: the save has happened, and cms.tasks.sync_gahk_web
    notices the stale revision within minutes and publishes anyway.
    """
    triggered_by_id = by.pk if by is not None else None

    def _enqueue() -> None:
        from .tasks import publish_gahk_web

        try:
            publish_gahk_web.delay(reason=reason, triggered_by_id=triggered_by_id)
        except Exception:
            logger.exception("Could not queue a gahk.dk publish (reason=%s)", reason)

    transaction.on_commit(_enqueue)


def request_publish(by: Resident | None) -> None:
    """The overview's "Udgiv nu": publish even if gahk.dk already reports the current revision."""
    with trail.event("cms.site.published", actor=by) as event:
        event.involve_kind(*SITE, role="subject")
        publish_on_commit("manual", by)


def request_gahk_web_update(by: Resident | None) -> None:
    """The overview's "Opdatér gahk.dk nu" (administrators): ask the webhost to update its code now."""
    with trail.event("cms.site.update_requested", actor=by) as event:
        event.involve_kind(*SITE, role="subject")

    def _enqueue() -> None:
        from .tasks import nudge_gahk_web_update

        try:
            nudge_gahk_web_update.delay()
        except Exception:
            logger.exception("Could not queue a gahk.dk update nudge")

    transaction.on_commit(_enqueue)


# ---------------------------------------------------------------------------------- text fields
def plain_text(value: object) -> str:
    """One line of plain text: whitespace (newlines included) collapsed, ends trimmed.

    No tag stripping: plain text is escaped wherever it is shown, so a typed "<" is just a "<".
    """
    return " ".join(str(value or "").split())


def _required_text(value: object, field_name: str, max_length: int, label: str) -> str:
    text = plain_text(value)
    if not text:
        raise ValidationError({field_name: f"Skriv {label}."})
    if len(text) > max_length:
        raise ValidationError({field_name: f"Højst {max_length} tegn."})
    return text


def _optional_text(value: object, field_name: str, max_length: int) -> str:
    text = plain_text(value)
    if len(text) > max_length:
        raise ValidationError({field_name: f"Højst {max_length} tegn."})
    return text


def _html(value: object) -> str:
    # `or ""`: clean_html passes falsy input straight back, and the columns are not nullable.
    return clean_html(str(value or "")) or ""


def library_urls() -> set[str]:
    return {image.url for image in CmsImage.objects.all() if image.file}


def _image_url(value: object, field_name: str, current: str) -> str:
    """ "" or an image from the library. The value already stored is accepted as it is, so a page
    whose old background is not in the library yet can still be saved unchanged."""
    url = str(value or "").strip()
    if not url or url == current:
        return url
    if url not in library_urls():
        raise ValidationError({field_name: "Vælg et billede fra billedbiblioteket."})
    return url


# ---------------------------------------------------------------------------------------- pages
PAGE_FIELDS = ("slug", "header", "body", "background_image", "description")


def page_state(page: Page) -> dict[str, Any]:
    return {name: getattr(page, name) for name in PAGE_FIELDS}


def _clean_slug(page: Page, value: object) -> str | None:
    slug = str(value or "").strip().strip("/").lower() or None
    if slug is None:
        return None
    try:
        validate_page_path(slug)
    except ValidationError as exc:
        raise ValidationError({"slug": exc.messages}) from exc
    clash = Page.objects.filter(slug=slug).exclude(pk=page.pk).first()
    if clash:
        raise ValidationError({"slug": f"Adressen /{slug} bruges allerede af siden “{clash.header}”."})
    return slug


def clean_page_data(page: Page, data: Mapping[str, Any]) -> dict[str, Any]:
    """The cleaned values of the PAGE_FIELDS present in `data` (absent fields keep their value)."""
    cleaned: dict[str, Any] = {}
    if "slug" in data:
        cleaned["slug"] = _clean_slug(page, data["slug"])
    if "header" in data or page.pk is None:
        cleaned["header"] = _required_text(data.get("header"), "header", 255, "en overskrift")
    if "body" in data:
        cleaned["body"] = _html(data["body"])
    if "background_image" in data:
        cleaned["background_image"] = _image_url(
            data["background_image"], "background_image", page.background_image
        )
    if "description" in data:
        cleaned["description"] = _optional_text(data["description"], "description", 300)
    return cleaned


def snapshot_page(page: Page, author: Resident | None, note: str = "") -> PageVersion:
    """Store the page's current content as a restorable version.

    `author` is None where it is genuinely unknown — a baseline snapshot taken of content that was
    already in the database before anyone was recorded as editing it.
    """
    version = PageVersion.objects.create(
        page=page,
        slug=page.slug or "",
        header=page.header,
        body=page.body,
        background_image=page.background_image,
        description=page.description,
        created_by=author,
        note=note[:120],
    )
    _trim_history(page)
    return version


def _trim_history(page: Page) -> None:
    """Drop the oldest snapshots beyond VERSIONS_PER_PAGE_CAP for one page."""
    stale = PageVersion.objects.filter(page=page).order_by("-created_at", "-id")[VERSIONS_PER_PAGE_CAP:]
    # Two steps because Django cannot delete() a sliced queryset — the pks have to be materialised.
    stale_pks = list(stale.values_list("pk", flat=True))
    if stale_pks:
        PageVersion.objects.filter(pk__in=stale_pks).delete()


def record_slug_change(page: Page, old_slug: str | None, author: Resident | None) -> None:
    """Point `old_slug` at `page` so its former address keeps working. Idempotent.

    Safe to call when nothing changed, when the page never had an address, or when it has just lost
    one — each of those simply does nothing.
    """
    if not page.slug or not old_slug or old_slug == page.slug:
        return

    # The page has been renamed *back* onto a path that previously redirected away. Left alone, that
    # row would now send the new address to itself, so it goes in the bin (cms.bin).
    for stale in PageRedirect.objects.filter(old_path=page.slug):
        stale.binned_at = timezone.now()
        stale.binned_by = author
        stale.save(update_fields=["binned_at", "binned_by"])

    # update_or_create, not create: an address may have been abandoned by another page earlier, and
    # the most recent claimant is the one that should own the redirect.
    PageRedirect.objects.update_or_create(old_path=old_slug, defaults={"page": page, "created_by": author})


def save_page(page: Page | None, data: Mapping[str, Any], by: Resident | None) -> Page:
    """Create (`page` None) or update a page from `data` (any of PAGE_FIELDS; absent ones are kept).

    Raises ValidationError keyed by field. Renaming a page leaves a redirect from the old address.
    Saving without a change writes nothing. Returns the saved page — when updating, a fresh instance
    read under a row lock, not the object passed in.
    """
    creating = page is None or page.pk is None
    action = "cms.page.created" if creating else "cms.page.updated"
    with trail.event(action, actor=by, subject=page, only_if_changed=True) as event:
        if page is None:
            page = Page()
        previous = None if creating else Page.objects.select_for_update().get(pk=page.pk)
        cleaned = clean_page_data(previous or page, data)
        before = page_state(previous) if previous is not None else {}

        if previous is not None:
            if {**before, **cleaned} == before:
                return previous
            # First edit of a page that predates version history: capture what it said beforehand,
            # or that one save would be unrecoverable.
            if not previous.versions.exists():
                snapshot_page(previous, None, note="Før første redigering (forfatter ukendt)")
            page = previous

        for name, value in cleaned.items():
            setattr(page, name, value)
        if creating and "slug" not in cleaned:
            page.slug = None
        page.updated_at = timezone.now()
        page.updated_by = by
        page.save()

        snapshot_page(page, by, note="Oprettet" if creating else "Redigeret")
        if previous is not None:
            record_slug_change(page, before["slug"], by)
        event.involve(page, "subject")
        publish_on_commit("save", by)
    return page


@dataclass
class RestoreResult:
    page: Page
    # Danish, for the editor: why the old address was not restored along with the content.
    warnings: list[str] = field(default_factory=list)


def _restorable_slug(page: Page, version: PageVersion, warnings: list[str]) -> str | None:
    """The snapshot's address if it can be used again, else None (and say why).

    A snapshot may hold an address that is no longer legal (anything from before cms.paths existed)
    or one another page has since taken. Neither is a reason to refuse the restore — the body is
    what people come for — so the content goes back and the address does not.
    """
    wanted = version.slug or None
    if not wanted or wanted == page.slug:
        return None
    try:
        validate_page_path(wanted)
    except ValidationError:
        warnings.append(
            f"Den gamle adresse /{wanted} er ikke en gyldig adresse længere, "
            "så siden beholder sin nuværende adresse. Indholdet er gendannet."
        )
        return None
    if Page.objects.filter(slug=wanted).exclude(pk=page.pk).exists():
        warnings.append(
            f"Den gamle adresse /{wanted} bruges nu af en anden side, "
            "så siden beholder sin nuværende adresse. Indholdet er gendannet."
        )
        return None
    return wanted


def restore_version(page: Page, version: PageVersion, by: Resident | None) -> RestoreResult:
    """Put a previous version of `page` back, without ever deleting history to do it."""
    if version.page_id != page.pk:
        raise ValidationError("Versionen hører ikke til denne side.")
    warnings: list[str] = []
    stamp = timezone.localtime(version.created_at).strftime("%d-%m-%Y %H:%M")
    details = {"version": version.pk, "version_from": stamp}
    with trail.event("cms.page.restored", actor=by, subject=page, details=details):
        page = Page.objects.select_for_update().get(pk=page.pk)
        # Snapshot first: a restore is itself an edit, and undoing an unwanted restore has to be
        # possible too. History only ever grows.
        snapshot_page(page, by, note="Før gendannelse")

        old_slug = page.slug
        page.header = plain_text(version.header) or page.header
        # Through the sanitizer again: a snapshot taken before the allowlist was tightened must not
        # become a way back in for markup that is no longer permitted.
        page.body = _html(version.body)
        page.background_image = version.background_image
        page.description = plain_text(version.description)[:300]
        if restored_slug := _restorable_slug(page, version, warnings):
            page.slug = restored_slug
        page.updated_at = timezone.now()
        page.updated_by = by
        page.save()

        # So the address it is being moved away from also keeps working.
        record_slug_change(page, old_slug, by)
        snapshot_page(page, by, note=f"Gendannet fra version af {stamp}")
        publish_on_commit("save", by)
    return RestoreResult(page=page, warnings=warnings)


# ------------------------------------------------------------------------------ news and events
def _aware_datetime(value: object, field_name: str) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        moment = value
    elif isinstance(value, str) and (parsed := parse_datetime(value.strip())):
        moment = parsed
    else:
        raise ValidationError({field_name: "Angiv et gyldigt tidspunkt."})
    return timezone.make_aware(moment) if timezone.is_naive(moment) else moment


def _date(value: object, field_name: str) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str) and (parsed := parse_date(value.strip())):
        return parsed
    raise ValidationError({field_name: "Angiv en gyldig dato."})


NEWS_FIELDS = ("title", "body", "published_at")
EVENT_FIELDS = ("title", "description", "starts_on")


def save_news(item: NewsItem | None, data: Mapping[str, Any], by: Resident | None) -> NewsItem:
    """Create or update a news item. `published_at` defaults to now for a new one."""
    creating = item is None or item.pk is None
    item = item if item is not None else NewsItem()
    before = {} if creating else {name: getattr(item, name) for name in NEWS_FIELDS}
    if "title" in data or creating:
        item.title = _required_text(data.get("title"), "title", 255, "en titel")
    if "body" in data:
        item.body = _html(data["body"])
    if data.get("published_at") not in (None, ""):
        published_at = _aware_datetime(data["published_at"], "published_at")
        # The editor's field shows minutes only, so an untouched field sends the stored time without
        # its seconds. That is no change: keep the stored time rather than record and publish one.
        stored = None if creating else item.published_at
        if stored is None or published_at != stored.replace(second=0, microsecond=0):
            item.published_at = published_at
    elif creating:
        item.published_at = timezone.now()
    if not creating and before == {name: getattr(item, name) for name in NEWS_FIELDS}:
        return item
    with trail.event("cms.news.created" if creating else "cms.news.updated", actor=by, subject=item):
        item.save()
        publish_on_commit("save", by)
    return item


def save_event(item: Event | None, data: Mapping[str, Any], by: Resident | None) -> Event:
    """Create or update a "Forsidebegivenhed" (the public event list, not the residents' events)."""
    creating = item is None or item.pk is None
    item = item if item is not None else Event()
    before = {} if creating else {name: getattr(item, name) for name in EVENT_FIELDS}
    if "title" in data or creating:
        item.title = _required_text(data.get("title"), "title", 255, "en titel")
    if "description" in data:
        item.description = _html(data["description"])
    if "starts_on" in data or creating:
        item.starts_on = _date(data.get("starts_on"), "starts_on")
    if not creating and before == {name: getattr(item, name) for name in EVENT_FIELDS}:
        return item
    with trail.event("cms.event.created" if creating else "cms.event.updated", actor=by, subject=item):
        item.save()
        publish_on_commit("save", by)
    return item


# --------------------------------------------------------------------------------------- images
def store_prepared_image(
    prepared: PreparedImage,
    name: str,
    caption: str,
    by: Resident | None,
    legacy_path: str | None = None,
) -> CmsImage:
    """Save already-prepared bytes as a library image (inside the caller's audit event)."""
    stem = stored_stem(name)
    image = CmsImage(
        caption=plain_text(caption)[:255],
        uploaded_by=by,
        sha256=prepared.sha256,
        content_type=prepared.content_type,
        size=len(prepared.data),
        legacy_path=legacy_path,
    )
    image.file.save(f"{stem}{prepared.extension}", ContentFile(prepared.data), save=False)
    image.save()
    return image


def upload_image(upload: Any, caption: str, by: Resident | None) -> CmsImage:  # noqa: ANN401 — an UploadedFile
    """Add an uploaded file to the library: checked, downscaled to at most 2400 px, metadata gone.

    Raises ValidationError({"file": ...}) for anything that is not a usable image. Publishes
    nothing: an image appears on gahk.dk once some content uses it.
    """
    try:
        upload = validate_image_upload(upload, settings.CMS_IMAGE_MAX_MB)
        prepared = prepare_image(upload)
    except ValidationError as exc:
        raise ValidationError({"file": exc.messages}) from exc
    except ImageRejected as exc:
        raise ValidationError({"file": str(exc)}) from exc
    with trail.event("cms.image.uploaded", actor=by) as event:
        image = store_prepared_image(prepared, getattr(upload, "name", "") or "image", caption, by)
        event.involve(image, "subject")
    return image


def update_image(image: CmsImage, caption: str, by: Resident | None) -> CmsImage:
    """Change an image's description (its default alt text). Content already using it is unchanged."""
    new_caption = _optional_text(caption, "caption", 255)
    if new_caption == image.caption:
        return image
    with trail.event("cms.image.updated", actor=by, subject=image):
        image.caption = new_caption
        image.save(update_fields=["caption"])
    return image


def usage_map(images: Iterable[CmsImage], *, include_binned: bool = False) -> dict[int, list[str]]:
    """Where each image is used, as Danish labels — so deleting one does not silently break a page.

    `include_binned` counts content in the bin too, labelled so (cms.bin, before deleting for good).

    Five queries for any number of images. A body uses an image when it holds its URL as
    Storage.url() writes it, or when the snapshot would publish it (cms.snapshot.media_names): a name
    may also be written with its letters as they are ("Værelse", typed in the HTML view) or with
    other percent-encoding, and gahk.dk gets the picture all the same. A background is chosen from
    the library, so it is always the URL exactly.
    """
    site = SiteSettings.load()
    manager = "all_objects" if include_binned else "objects"

    def binned(row: Page | NewsItem | Event) -> str:
        return " (i papirkurven)" if row.binned_at else ""

    bodies: list[tuple[str, str, set[str]]] = []
    backgrounds: list[tuple[str, str]] = []
    for page in getattr(Page, manager).only("header", "body", "background_image", "binned_at"):
        bodies.append((f"Side: {page.header}{binned(page)}", page.body, media_names(page.body)))
        backgrounds.append((f"Baggrund: {page.header}{binned(page)}", page.background_image))
    bodies += [
        (f"Nyhed: {n.title}{binned(n)}", n.body, media_names(n.body))
        for n in getattr(NewsItem, manager).only("title", "body", "binned_at")
    ]
    bodies += [
        (f"Begivenhed: {e.title}{binned(e)}", e.description, media_names(e.description))
        for e in getattr(Event, manager).only("title", "description", "binned_at")
    ]
    backgrounds.append(("Standardbaggrund", site.default_background))
    usage: dict[int, list[str]] = {}
    for image in images:
        if not image.file:
            usage[image.pk] = []
            continue
        url, name = image.url, image.file.name
        usage[image.pk] = [label for label, text, names in bodies if url in text or name in names]
        usage[image.pk] += [label for label, background in backgrounds if background == url]
    return usage


def image_usage(image: CmsImage) -> list[str]:
    """Where the image is used, as Danish labels (see usage_map)."""
    return usage_map([image])[image.pk]


# ----------------------------------------------------------------------------------------- menu
def clean_menu_url(value: object) -> str:
    """A site path ("/vision", "/begivenheder/"), an http(s) URL or a mailto: link."""
    url = str(value or "").strip()
    if not url:
        raise ValidationError("Skriv et link.")
    if len(url) > 500 or MENU_URL_FORBIDDEN.search(url):
        raise ValidationError("Linket indeholder tegn, et link ikke må have.")
    site_path = url.startswith("/") and not url.startswith("//")
    if not (site_path or re.match(r"^https?://[^/]", url) or url.startswith("mailto:")):
        raise ValidationError("Linket skal starte med / (en side på gahk.dk) eller https://.")
    return url


def menu_state() -> list[dict[str, str]]:
    return [{"label": item.label, "url": item.url} for item in MenuItem.objects.all()]


def save_menu(items: Sequence[Mapping[str, Any]], by: Resident | None) -> list[MenuItem]:
    """Make the menu `items` ({"label", "url"}, and "id" for an entry that is already there), in that
    order.

    One call covers adding, renaming, reordering and removing entries: an entry with the id of a menu
    item keeps that row (one without, a row with its text and link), any other entry becomes a new
    row, and a menu item no entry kept goes in the bin (cms.bin). Raises ValidationError({"items": [...]}) naming each bad entry by its
    position.
    """
    if len(items) > MAX_MENU_ITEMS:
        raise ValidationError({"items": f"Menuen kan højst have {MAX_MENU_ITEMS} punkter."})
    errors: list[str] = []
    cleaned: list[dict[str, str]] = []
    ids: list[int | None] = []
    for number, entry in enumerate(items, start=1):
        label = plain_text(entry.get("label"))
        try:
            url = clean_menu_url(entry.get("url"))
        except ValidationError as exc:
            errors.append(f"Punkt {number}: {' '.join(exc.messages)}")
            continue
        if not label:
            errors.append(f"Punkt {number}: skriv en tekst.")
        elif len(label) > 60:
            errors.append(f"Punkt {number}: teksten må højst være 60 tegn.")
        else:
            cleaned.append({"label": label, "url": url})
            raw_id = str(entry.get("id") or "")
            ids.append(int(raw_id) if raw_id.isdigit() else None)
    if errors:
        raise ValidationError({"items": errors})

    with trail.event("cms.menu.updated", actor=by, only_if_changed=True) as event:
        before = menu_state()
        if before == cleaned:
            return list(MenuItem.objects.all())
        # The menu is one thing to the editor: one change, the list before and after, rather than a
        # line for every row written. The entries removed are named, so each one's own history shows
        # when it went in the bin.
        removed = {item.pk: item for item in MenuItem.objects.select_for_update()}
        # Rows by id first; an entry without one (a caller that knows no ids) takes an unclaimed row
        # with its text and link, so only what really left the menu goes in the bin.
        rows: list[MenuItem | None] = [removed.pop(pk, None) if pk is not None else None for pk in ids]
        for index, entry in enumerate(cleaned):
            if rows[index] is None:
                same = next(
                    (i for i in removed.values() if (i.label, i.url) == (entry["label"], entry["url"])), None
                )
                rows[index] = removed.pop(same.pk) if same is not None else None
        now = timezone.now()
        with trail.unaudited():
            for index, (entry, item) in enumerate(zip(cleaned, rows, strict=True)):
                if item is None:
                    MenuItem.objects.create(label=entry["label"], url=entry["url"], order=index * 10)
                    continue
                item.label, item.url, item.order = entry["label"], entry["url"], index * 10
                item.save(update_fields=["label", "url", "order"])
            for item in removed.values():
                item.binned_at = now
                item.binned_by = by
                item.save(update_fields=["binned_at", "binned_by"])
        # The menu first: it is what the change log names the event after.
        event.change_kind("cms.menu", None, "Menu", "menu", before, cleaned)
        for item in removed.values():
            event.involve(item, "binned")
        publish_on_commit("save", by)
    return list(MenuItem.objects.all())


def menu_paths() -> set[str]:
    """The site paths the menu links to, bare ("" for "/", "begivenheder" for "/begivenheder/")."""
    return {
        item.url.split("?", 1)[0].split("#", 1)[0].strip("/")
        for item in MenuItem.objects.all()
        if item.url.startswith("/") and not item.url.startswith("//")
    }


def is_reachable(page: Page, menu: set[str] | None = None) -> bool:
    """Whether a visitor can find `page` from the menu: the page itself or its section is in it.

    gahk.dk lists a section's pages beside each page of the section, so `faciliteter/kokken` is
    reachable when `/faciliteter` is in the menu. The front page is always reachable.
    """
    if page.pk == FRONT_PAGE_ID:
        return True
    if not page.slug:
        return False
    menu = menu_paths() if menu is None else menu
    return page.slug in menu or page.slug.split("/", 1)[0] in menu


def unreachable_pages() -> list[Page]:
    """Pages with an address that nothing in the menu leads to — for the editor's warning."""
    menu = menu_paths()
    pages = Page.objects.filter(slug__isnull=False).exclude(pk=FRONT_PAGE_ID).order_by("slug")
    return [page for page in pages if not is_reachable(page, menu)]


# ------------------------------------------------------------------------------------ redirects
def save_redirect(
    redirect: PageRedirect | None, data: Mapping[str, Any], by: Resident | None
) -> PageRedirect:
    """Create or update an old address that sends visitors on to a page (`data`: old_path, page).

    `page` may be a Page or its pk.
    """
    creating = redirect is None or redirect.pk is None
    redirect = redirect if redirect is not None else PageRedirect()
    before = {} if creating else {"old_path": redirect.old_path, "page": redirect.page_id}

    old_path = str(data.get("old_path", redirect.old_path) or "").strip().strip("/").lower()
    if not old_path:
        raise ValidationError({"old_path": "Skriv den gamle adresse."})
    try:
        validate_page_path(old_path)
    except ValidationError as exc:
        raise ValidationError({"old_path": exc.messages}) from exc
    if live := Page.objects.filter(slug=old_path).first():
        raise ValidationError({"old_path": f"/{old_path} er adressen på siden “{live.header}”."})
    if PageRedirect.objects.filter(old_path=old_path).exclude(pk=redirect.pk).exists():
        raise ValidationError({"old_path": f"/{old_path} sender allerede videre til en anden side."})

    target: Any = data.get("page", None if creating else redirect.page)
    if not isinstance(target, Page):
        target = Page.objects.filter(pk=target).first() if target else None
    if target is None:
        raise ValidationError({"page": "Vælg den side, adressen skal sende videre til."})
    if not target.slug and target.pk != FRONT_PAGE_ID:
        raise ValidationError({"page": "Siden har ingen adresse, så der er intet at sende videre til."})

    redirect.old_path = old_path
    redirect.page = target
    after = {"old_path": redirect.old_path, "page": redirect.page_id}
    if not creating and before == after:
        return redirect
    action = "cms.redirect.created" if creating else "cms.redirect.updated"
    with trail.event(action, actor=by, subject=redirect):
        if creating:
            redirect.created_by = by
        redirect.save()
        publish_on_commit("save", by)
    return redirect


# -------------------------------------------------------------------------------------- preview
def render_preview(
    kind: str, data: Mapping[str, Any], instance: Page | NewsItem | Event | None = None
) -> str:
    """gahk.dk's rendering of an unsaved page, news item or event (`kind`: page/news/event), with
    `data` cleaned as saving would clean it. Nothing is stored, here or on gahk.dk.

    Raises cms.gahk_web.GahkWebError when gahk.dk cannot be asked (GahkWebDisabled without a key).
    """
    from .gahk_web import GahkWebClient
    from .snapshot import preview_request

    pk = instance.pk if instance is not None else None
    item: Page | NewsItem | Event
    if kind == "page":
        item = Page(
            pk=pk,
            slug=str(data.get("slug") or "").strip().strip("/").lower() or None,
            header=plain_text(data.get("header")),
            body=_html(data.get("body")),
            background_image=str(data.get("background_image") or "").strip(),
            description=plain_text(data.get("description"))[:300],
            updated_at=timezone.now(),
        )
    elif kind == "news":
        try:
            published_at = _aware_datetime(data.get("published_at"), "published_at")
        except ValidationError:
            published_at = timezone.now()
        item = NewsItem(
            pk=pk,
            title=plain_text(data.get("title")),
            body=_html(data.get("body")),
            published_at=published_at,
        )
    elif kind == "event":
        try:
            starts_on = _date(data.get("starts_on"), "starts_on")
        except ValidationError:
            starts_on = timezone.localdate()
        item = Event(
            pk=pk,
            title=plain_text(data.get("title")),
            description=_html(data.get("description")),
            starts_on=starts_on,
        )
    else:
        raise ValueError(f"unknown preview kind {kind!r}")
    return GahkWebClient().preview(preview_request(item))


# -------------------------------------------------------------------------------- site settings
def save_site_settings(data: Mapping[str, Any], by: Resident | None) -> SiteSettings:
    """Change the website-wide settings (today: the default background image)."""
    with trail.event("cms.settings.updated", actor=by, only_if_changed=True) as event:
        SiteSettings.load()
        site = SiteSettings.objects.select_for_update().get(pk=1)
        before = site.default_background
        if "default_background" in data:
            site.default_background = _image_url(
                data["default_background"], "default_background", site.default_background
            )
        if site.default_background == before:
            return site
        site.save()
        event.involve(site, "subject")
        publish_on_commit("save", by)
    return site
