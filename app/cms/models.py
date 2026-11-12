"""Website content ("Hjemmeside"): the source of truth for everything gahk.dk shows.

gahk.dk itself is rendered by gahk_web on separate hosting. This app holds the content, the image
library, the menu and the audit trail, and cms.tasks publishes a signed snapshot of it to gahk.dk
(see cms.snapshot). Every write goes through cms.services, which sanitizes HTML, keeps page
versions, records a ContentChange and publishes after the commit; nothing else writes here at
runtime (the Django admin no longer registers these models, so it cannot become an unaudited
second write path).

Plain text and HTML are kept apart: titles, headers and labels are PLAIN TEXT and every renderer
escapes them; bodies are HTML that cms.sanitize cleaned on the way in.
"""

from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.color import no_style
from django.db import connections, models, router
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from core.files import delete_attached_files

from .paths import validate_page_path

# The page gahk.dk shows at "/", whatever its own slug (`velkommen`) says. Pinned since the legacy
# site, where it was gahk_page row 1.
FRONT_PAGE_ID = 1


class Page(models.Model):  # gahk_page
    # Legacy menuCat. Nothing reads it — the menu is MenuItem and sub-pages are found by slug
    # prefix — but the ETL writes it on every run and the values are the only surviving record of
    # the legacy menu grouping, so the column stays. `editable=False` keeps it out of every ModelForm: it is an unlabelled number
    # whose only effect on an editor is to invite a wrong guess.
    menu_category = models.PositiveSmallIntegerField(default=0, editable=False)
    # A path, not a slug: multi-segment by design (`faciliteter/kokken`), seeded from the legacy
    # routes.php map and served whole by gahk.dk. NULL where a page has no public address (the old
    # legacy optagelse bodies) — `unique` permits many NULLs but only one "", hence the services
    # storing None rather than "" for a blank address. Page id 1 is the front page and is served
    # at "/" whatever its slug says (cms.snapshot.FRONT_PAGE_ID).
    #
    # CharField + explicit validators, NOT SlugField: `forms.SlugField` re-adds `validate_slug`
    # independently of the model field, so a SlugField could never accept the `/` the router needs.
    # NOTE: naming `validate_page_path` here pins that import path for migration 0004 — changing the
    # rules inside the function is free, changing this list is not.
    slug = models.CharField(
        max_length=80,
        unique=True,
        blank=True,
        null=True,
        validators=[validate_page_path],
        verbose_name="Adresse (URL)",
        help_text="Sidens adresse på sitet, fx faciliteter/kokken. Ændrer du den, "
        "oprettes der automatisk en omdirigering fra den gamle adresse.",
    )
    # Plain text, escaped wherever it is shown. It used to be rendered `|safe` without ever being
    # sanitized; migration 0005 turned the stored values into plain text.
    header = models.CharField(max_length=255, verbose_name="Overskrift")
    body = models.TextField(blank=True, verbose_name="Indhold")  # legacy `text` (HTML)
    # A URL string, not a FileField: a library image's /media/cms/… URL ("" for none, which means
    # SiteSettings.default_background). Legacy /public/image/… values are converted into the
    # library by cms.legacy_images.
    background_image = models.CharField(max_length=255, blank=True, verbose_name="Baggrundsbillede")
    description = models.CharField(
        max_length=300,
        blank=True,
        verbose_name="Beskrivelse",
        help_text="Kort tekst til søgemaskiner og delte links. Valgfri.",
    )
    updated_at = models.DateTimeField(default=timezone.now, verbose_name="Sidst rettet")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cms_pages_updated",
        verbose_name="Rettet af",
    )

    class Meta:
        verbose_name = "Side"
        verbose_name_plural = "Sider"

    def __str__(self) -> str:
        return self.slug or self.header

    def get_absolute_url(self) -> str:
        """The page's public URL on gahk.dk (settings.PUBLIC_SITE_URL), or "" when it has no address.

        Absolute, because the public site is served by gahk_web on other hosting, not by this app.
        "" (not None) because templates concatenate it and admin reads it via view_on_site.
        """
        return f"{settings.PUBLIC_SITE_URL.rstrip('/')}/{self.slug}" if self.slug else ""

    @classmethod
    def put_front_page(cls, **fields: Any) -> "Page":  # noqa: ANN401
        """Create or update the front page, at its pinned id FRONT_PAGE_ID, with `fields`.

        THE way to make the front page outside the legacy ETL. An insert with an explicit id does not
        move PostgreSQL's id sequence, so on a database where no page was ever created the next
        ordinary Page.objects.create() is handed id 1 again and fails on the primary key. The
        sequence is moved past the highest id here, in the same breath.
        """
        page, _created = cls.objects.update_or_create(pk=FRONT_PAGE_ID, defaults=fields)
        connection = connections[router.db_for_write(cls)]
        with connection.cursor() as cursor:
            for statement in connection.ops.sequence_reset_sql(no_style(), [cls]):
                cursor.execute(statement)
        return page


class NewsItem(models.Model):  # gahk_news; shown on gahk.dk's front page and /begivenheder/
    title = models.CharField(max_length=255, verbose_name="Titel")  # plain text
    body = models.TextField(blank=True, verbose_name="Tekst")  # HTML
    published_at = models.DateTimeField(verbose_name="Udgivet")

    class Meta:
        ordering = ["-published_at"]
        verbose_name = "Nyhed"
        verbose_name_plural = "Nyheder"

    def __str__(self) -> str:
        return self.title


class PylonEvent(models.Model):  # gahk_pylon_calendar — likely retired; migrated as archive
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    starts_on = models.DateField()

    class Meta:
        ordering = ["starts_on"]
        # Deliberately not registered in the admin: the Pylon calendar is a retired feature kept as
        # an archive, so it is data to migrate, not a screen anybody needs.
        verbose_name = "Pylon-begivenhed"
        verbose_name_plural = "Pylon-begivenheder"

    def __str__(self) -> str:
        return f"{self.starts_on}: {self.title}"


class Event(
    models.Model
):  # NEW — replaces the hard-coded array in legacy begivenheder.php (developer-edited)
    title = models.CharField(max_length=255, verbose_name="Titel")  # plain text
    description = models.TextField(blank=True, verbose_name="Beskrivelse")  # HTML
    starts_on = models.DateField(verbose_name="Dato")

    class Meta:
        ordering = ["starts_on"]
        # NOT "Begivenhed": events.Event already claims that label explicitly, so that the admin
        # index does not show two identical sections (see the events.admin docstring). This is the
        # public front-page list; that one is the residents' own event system.
        verbose_name = "Forsidebegivenhed"
        verbose_name_plural = "Forsidebegivenheder"

    def __str__(self) -> str:
        return f"{self.starts_on}: {self.title}"


class CmsImage(models.Model):
    """An image in the website's library, for use in page/news/event HTML and as a background.

    ALL of gahk.dk's content images live here (in S3), including the ones the old site kept in
    static/legacy/ (imported by cms.legacy_images, which sets `legacy_path`). gahk.dk gets a
    copy of each image it needs at a content-hashed URL, /billeder/<sha256[:16]>/<name>, which is
    why `sha256` is kept: it names the copy and lets publishing skip the ones gahk.dk already has.

    Uploads are downscaled and stripped of metadata before they are stored (cms.images), so the
    stored bytes are the published bytes and `sha256` is theirs.

    FileField, not ImageField: core.uploads and cms.images do the checking, and ImageField would
    open every file on each save to read its dimensions.
    """

    file = models.FileField(upload_to="cms/%Y/%m/", max_length=255, verbose_name="Fil")
    # Doubles as the default alt text when the image is inserted, so a described image starts
    # accessible instead of relying on the editor to remember.
    caption = models.CharField(
        max_length=255, blank=True, verbose_name="Beskrivelse", help_text="Bruges som alt-tekst."
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    # None for a system import (cms.legacy_images).
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cms_images",
    )
    # sha256 of the stored bytes (hex), their content type and size: what gahk.dk is told about its
    # copy. Blank only for a row stored before these fields existed; cms.snapshot fills all three in
    # from the file the first time it needs them (content_type stays "" for a file gahk.dk cannot
    # take, which is then left out of every snapshot).
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    content_type = models.CharField(max_length=30, blank=True)
    size = models.PositiveIntegerField(null=True, blank=True)
    # Where the old site served this image from, below static/legacy/image/ (e.g.
    # "upload/images/gang.jpg"). Set only by the legacy import; unique so an image is never imported
    # twice, and kept so gahk.dk can send old image links to the new copy.
    legacy_path = models.CharField(max_length=255, unique=True, null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-uploaded_at"]
        verbose_name = "Billede"
        verbose_name_plural = "Billeder"

    def __str__(self) -> str:
        # `file.name` is Optional to the type checker (a FileField can be blank), so fall back
        # rather than let Path() take a None.
        return self.caption or Path(self.file.name or "").name or "(uden fil)"

    @property
    def url(self) -> str:
        return self.file.url if self.file else ""


@receiver(post_delete, sender=CmsImage)
def _delete_cms_image_file(sender: type[CmsImage], instance: CmsImage, **kwargs: Any) -> None:  # noqa: ANN401
    """Drop the file from storage with the row — Django has not done this since 1.3, and an orphaned
    upload is invisible: nothing lists it and nothing ever cleans it up. See core.files."""
    delete_attached_files(instance)


class LegacyImageFailure(models.Model):
    """An old-site image the automatic import (cms.legacy_images) could not bring into the library.

    Recorded so the 2-minute sync does not ask for it again forever; a manual
    `manage.py import_legacy_images` tries every recorded one again and drops the row once it works.
    The Hjemmeside overview lists them: the content still points at the old address, which gahk.dk
    cannot show.
    """

    class Reason(models.TextChoices):
        MISSING = "missing", "Findes ikke"
        UNUSABLE = "unusable", "Kan ikke bruges"

    # Below the old static/legacy/image/, like CmsImage.legacy_path.
    legacy_path = models.CharField(max_length=255, unique=True)
    reason = models.CharField(max_length=10, choices=Reason.choices)
    detail = models.CharField(max_length=255, blank=True)
    failed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["legacy_path"]
        verbose_name = "Gammelt billede, der ikke kunne flyttes"
        verbose_name_plural = "Gamle billeder, der ikke kunne flyttes"

    def __str__(self) -> str:
        return f"{self.legacy_path}: {self.get_reason_display()}"


class PageRedirect(models.Model):
    """A page's former address, so renaming a page never breaks a link, a bookmark or a search result.

    Points at the **page**, not at a replacement path string. That one choice removes chain-following
    and loop detection entirely instead of implementing them: the destination is always `page.slug`
    as of now, so renaming `a → b → c` leaves both `a` and `b` resolving to `c` in a single hop, and
    a rename back to `a` cannot produce a redirect that points at itself (cms.services deletes it).

    CASCADE, unlike PageVersion: a redirect whose page is gone has nowhere to send anyone, and
    dropping the row also frees `old_path` for reuse.
    """

    old_path = models.CharField(
        max_length=80,
        unique=True,
        validators=[validate_page_path],
        verbose_name="Gammel adresse",
    )
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="redirects")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cms_page_redirects",
    )

    class Meta:
        ordering = ["old_path"]
        verbose_name = "Gammel adresse"
        verbose_name_plural = "Gamle adresser"

    def __str__(self) -> str:
        return f"/{self.old_path} → /{self.page.slug or ''}"


class PageVersion(models.Model):
    """A complete snapshot of one page's editable content, written on every save (cms.services).

    This holds the content itself, which is what makes "gendan" possible; ContentChange is the
    site-wide trail of who changed what.

    `page` is SET_NULL rather than CASCADE on purpose: the version history *is* the recovery story,
    so deleting a page must not destroy the only remaining copy of its content. The snapshotted
    `header` keeps an orphaned row identifiable afterwards.
    """

    page = models.ForeignKey(Page, null=True, blank=True, on_delete=models.SET_NULL, related_name="versions")
    # No validator on this one: history must be able to hold pre-fix values such as the
    # `faciliteter-kokken` that prompted all of this, or it could not be shown, let alone reverted.
    slug = models.CharField(max_length=80, blank=True, default="")
    header = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)
    background_image = models.CharField(max_length=255, blank=True)
    description = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cms_page_versions",
    )
    note = models.CharField(max_length=120, blank=True)

    class Meta:
        # `-id` is not decoration: several snapshots can share a timestamp (a restore writes two in
        # one transaction), and without the tiebreak "newest" would be arbitrary between them.
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["page", "-created_at"])]
        verbose_name = "Sideversion"
        verbose_name_plural = "Ændringshistorik"

    def __str__(self) -> str:
        return f"{self.header} ({self.created_at:%d-%m-%Y %H:%M})"


class MenuItem(models.Model):
    """One entry in gahk.dk's main menu, in `order`. Seeded from the old hard-coded menu."""

    label = models.CharField(max_length=60, verbose_name="Tekst")
    # A site path ("/vision", "/begivenheder/") or an absolute http(s) URL.
    url = models.CharField(max_length=500, verbose_name="Link")
    order = models.PositiveIntegerField(default=0, verbose_name="Rækkefølge")

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Menupunkt"
        verbose_name_plural = "Menu"

    def __str__(self) -> str:
        return f"{self.label} ({self.url})"


class SiteSettings(models.Model):
    """Website-wide settings. A singleton: always pk=1, fetched with SiteSettings.load()."""

    # A library image URL (/media/cms/…), the hero behind every page without a background of its
    # own. "" means a plain green band.
    default_background = models.CharField(max_length=255, blank=True, verbose_name="Standardbaggrund")

    class Meta:
        verbose_name = "Indstillinger for hjemmesiden"
        verbose_name_plural = "Indstillinger for hjemmesiden"

    def __str__(self) -> str:
        return "Indstillinger for hjemmesiden"

    @classmethod
    def load(cls) -> "SiteSettings":
        settings_row, _created = cls.objects.get_or_create(pk=1)
        return settings_row


class ContentChange(models.Model):
    """The audit trail for every change to the website's content: who, when, what, before/after.

    Written by cms.services on every write (and by cms.legacy_images for its system changes), never
    by a signal, so a script or an import cannot fake an editor. `changes` holds the values
    themselves — bodies included — as {field: [before, after]}, so "what did it say before?" can be
    answered for news, events, the menu and images too, not only for pages (which also keep full
    PageVersion snapshots for restoring).

    `by_name` is a snapshot of the editor's name, so the trail still reads after the account is
    gone; it is "System" for automatic changes.
    """

    class Action(models.TextChoices):
        CREATE = "create", "Oprettet"
        UPDATE = "update", "Rettet"
        DELETE = "delete", "Slettet"
        RESTORE = "restore", "Gendannet"
        PUBLISH = "publish", "Udgivet"
        UPLOAD = "upload", "Uploadet"
        MENU = "menu", "Menu ændret"

    class Kind(models.TextChoices):
        PAGE = "page", "Side"
        NEWS = "news", "Nyhed"
        EVENT = "event", "Begivenhed"
        IMAGE = "image", "Billede"
        MENU = "menu", "Menu"
        REDIRECT = "redirect", "Omdirigering"
        SETTINGS = "settings", "Indstillinger"
        SITE = "site", "Hjemmesiden"

    at = models.DateTimeField(default=timezone.now, db_index=True)
    by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cms_content_changes",
    )
    by_name = models.CharField(max_length=255, blank=True)
    action = models.CharField(max_length=10, choices=Action.choices)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    label = models.CharField(max_length=255, blank=True)
    changes = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["kind", "object_id", "-at"])]
        verbose_name = "Ændring"
        verbose_name_plural = "Ændringer"

    def __str__(self) -> str:
        return (
            f"{self.at:%d-%m-%Y %H:%M} {self.by_name or 'System'}: {self.get_action_display()} {self.label}"
        )


class Publication(models.Model):
    """One attempt to publish the content to gahk.dk (cms.tasks.publish_gahk_web)."""

    class Reason(models.TextChoices):
        SAVE = "save", "Ændring gemt"
        RECONCILE = "reconcile", "Afstemning"
        MANUAL = "manual", "Manuelt"
        NIGHTLY = "nightly", "Natlig genopbygning"

    class Status(models.TextChoices):
        RUNNING = "running", "I gang"
        OK = "ok", "OK"
        FAILED = "failed", "Fejlede"
        SKIPPED = "skipped", "Sprunget over"

    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    revision = models.CharField(max_length=64, blank=True)
    reason = models.CharField(max_length=10, choices=Reason.choices)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cms_publications",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    error = models.TextField(blank=True)
    pages_written = models.PositiveIntegerField(null=True, blank=True)
    images_uploaded = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-started_at", "-id"]
        verbose_name = "Udgivelse"
        verbose_name_plural = "Udgivelser"

    def __str__(self) -> str:
        return f"{self.started_at:%d-%m-%Y %H:%M} {self.get_reason_display()}: {self.get_status_display()}"


class GahkWebSync(models.Model):
    """What intern last heard from gahk.dk. A singleton (pk=1), written by cms.tasks.

    For the Hjemmeside overview: is gahk.dk reachable, which content revision and code version does
    it run, when were applications and visits last pulled, and how did the last self-update go.
    """

    status_checked_at = models.DateTimeField(null=True, blank=True)
    remote_version = models.CharField(max_length=64, blank=True)
    remote_revision = models.CharField(max_length=64, blank=True)
    outbox_pulled_at = models.DateTimeField(null=True, blank=True)
    # Applications from gahk.dk that failed intern's form rules and were dropped (logged).
    rejected_applications = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    # The webhost's self-updater, as reported by the last nudge (cms.tasks.nudge_gahk_web_update).
    update_checked_at = models.DateTimeField(null=True, blank=True)
    update_available_version = models.CharField(max_length=64, blank=True)
    update_result = models.CharField(max_length=255, blank=True)
    update_error = models.TextField(blank=True)

    class Meta:
        verbose_name = "Synkronisering med gahk.dk"
        verbose_name_plural = "Synkronisering med gahk.dk"

    def __str__(self) -> str:
        return "Synkronisering med gahk.dk"

    @classmethod
    def load(cls) -> "GahkWebSync":
        state, _created = cls.objects.get_or_create(pk=1)
        return state
