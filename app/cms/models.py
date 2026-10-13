"""CMS content (F-006/07/08). Migrated from the legacy site and rendered read-only on the public
side; edited at runtime only through the role-gated Django admin (see cms.admin), whose every HTML
field is sanitized on save. HTML bodies are also sanitized on import, as defence-in-depth.

Three models exist purely so a runtime edit cannot lose anything:

  * CmsImage — editors upload pictures instead of committing them to the repo (the old workflow),
    and body HTML references them by /media/ URL.
  * PageRedirect — a page's former addresses, so renaming one never breaks a link or a bookmark.
  * PageVersion — a full snapshot per save, because Django's admin LogEntry records only which
    field *names* changed. An editor who overwrote a body could not previously get it back.

The last two were added after an editor renamed /faciliteter/kokken and the page dropped out of its
section sidebar with no way to undo it; cms.paths carries the full account.
"""

from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import reverse

from core.files import delete_attached_files

from .paths import validate_page_path


class Page(models.Model):  # gahk_page
    # Legacy menuCat. Nothing reads it — the public menus are hard-coded lists in
    # core.context_processors and sub-pages are found by slug prefix (cms.nav) — but the ETL writes
    # it on every run and the values are the only surviving record of the legacy menu grouping, so
    # the column stays. `editable=False` keeps it out of every ModelForm: it is an unlabelled number
    # whose only effect on an editor is to invite a wrong guess.
    menu_category = models.PositiveSmallIntegerField(default=0, editable=False)
    # A path, not a slug: multi-segment by design (`faciliteter/kokken`), seeded from the legacy
    # routes.php map and matched whole by the catch-all in config.urls. NULL where a page has no
    # public address (the `optagelse` bodies, rendered by that app instead) — `unique` permits many
    # NULLs but only one "", hence the form storing None rather than "" for a blank address.
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
    header = models.CharField(max_length=255, verbose_name="Overskrift")
    body = models.TextField(blank=True, verbose_name="Indhold")  # legacy `text` (HTML)
    background_image = models.CharField(max_length=255, blank=True)  # legacy bgpic

    class Meta:
        verbose_name = "Side"
        verbose_name_plural = "Sider"

    def __str__(self) -> str:
        return self.slug or self.header

    def get_absolute_url(self) -> str:
        """The page's public URL, or "" when it has no address.

        Reversed against the catch-all rather than f-stringed so the routing contract keeps one
        owner; "" (not None) because templates concatenate it and admin reads it via view_on_site.
        """
        return reverse("page", kwargs={"url_path": self.slug}) if self.slug else ""


class NewsItem(models.Model):  # gahk_news — archive-only (live site shows the Facebook feed, F-007)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    published_at = models.DateTimeField()

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
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    starts_on = models.DateField()

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
    """An image an editor uploaded for use in page/news/event HTML.

    Exists so putting a picture on the site no longer means committing a file to the repo and
    hand-writing a path: upload here, and the body editor inserts the <img> for you.

    FileField, not ImageField: ImageField requires Pillow, which is not a dependency of this project
    (same call as rooms.RoomConditionScore.photo). core.uploads does the checking.
    """

    file = models.FileField(upload_to="cms/%Y/%m/", max_length=255, verbose_name="Fil")
    # Doubles as the default alt text when the image is inserted, so a described image starts
    # accessible instead of relying on the editor to remember.
    caption = models.CharField(
        max_length=255, blank=True, verbose_name="Beskrivelse", help_text="Bruges som alt-tekst."
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cms_images",
    )

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
    """A complete snapshot of one page's editable content, written on every save from the admin.

    Django's admin LogEntry records only which field *names* changed, so a body somebody overwrote
    was simply gone. This holds the content itself, which is what makes "gendan" possible.

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
