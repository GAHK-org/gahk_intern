"""CMS editing in Django admin — the single, role-gated write path back into page content.

Editing of Pages / news / events is tied to the **content-editor roles** — administrator,
indstilling, inspektion and pr (the frontpage/PR group) — or a superuser, via `has_active_role`.
Django's per-model permission bits are not used (role-holders have `is_staff` but no perms, so
without these overrides they'd see nothing).

`Page.body` etc. are rendered with `|safe`, but every editable field is run through `clean_html` on
save (see the *AdminForm classes), so a content editor cannot inject scripts/dangerous HTML.

Most of what follows exists because of one incident: an editor opened /faciliteter/kokken, changed
the address to `faciliteter-kokken`, saved — and the page vanished from the site with no way back,
because `/` was rejected as an illegal character. Three habits of this screen made that possible,
and each has an answer here:

  * a page's address was a free-text field contradicting the router (cms.paths) → it is now composed
    from a section picker plus a final segment, and the old address redirects itself (cms.services);
  * nothing showed whether a page was still linked from anywhere → the changelist carries a
    reachability badge and saving an unreachable page says so out loud (cms.nav);
  * history recorded field *names* and no values → every save snapshots the page, with a diff and a
    restore button (PageAdmin.versions_view).

A note for anyone extending this: Django admin does not load the project's Vite bundle, so there is
no Tailwind, no Alpine and no htmx on any of these screens. Client-side behaviour is plain DOM code
under static/cms/ (insert_image.js, page_path.js) and styling uses admin's own classes.
"""

from typing import Any

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import OuterRef, Q, QuerySet, Subquery
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, render
from django.urls import URLPattern, path, reverse
from django.utils import timezone
from django.utils.html import format_html

from core.uploads import validate_image_upload
from residents.models import Role
from residents.permissions import CMS_EDITOR_ROLES, current_resident, has_active_role

from .diff import line_diff
from .models import CmsImage, Event, NewsItem, Page, PageRedirect, PageVersion
from .nav import PROBLEM_STATUSES, STATUS_LABELS, is_reachable, statuses
from .paths import join_path, normalize_segment, split_path, validate_page_path
from .sanitize import clean_html
from .services import record_slug_change, snapshot_page


class BodyEditorMixin:
    """Adds the upload/insert toolbar above a form's HTML fields.

    Plain admin-side JavaScript: Django admin does not load the Vite bundle (that is the intern
    shell only), so this cannot use Alpine or htmx and never goes through the frontend build.
    """

    class Media:
        js = ("cms/insert_image.js",)


class PageAdminForm(BodyEditorMixin, forms.ModelForm):
    """The page editor. Composes the address from a section picker instead of accepting free text.

    `path_parent` / `path_segment` are declared at class level rather than built in `__init__` — and
    that is load-bearing, not style. `ModelAdmin.get_form` passes the flattened fieldsets as
    `fields=` into `modelform_factory`, and `ModelFormMetaclass` raises `FieldError: Unknown
    field(s)` for any name that is neither a model field nor a *declared* form field. (The
    `background_image` override below gets away with `__init__` only because it *is* a model field.)
    """

    path_parent = forms.ChoiceField(
        choices=[],
        required=False,
        label="Placering",
        help_text="Hvilken sektion siden ligger under. Sider uden sektion vises kun, hvis de står i menuen.",
    )
    path_segment = forms.CharField(
        max_length=80,
        required=False,
        label="Sidste del af adressen",
        widget=forms.TextInput(attrs={"class": "vTextField", "autocapitalize": "off"}),
        help_text="Kun små bogstaver, tal og bindestreg — fx “kokken”. "
        "Lad feltet stå tomt, hvis siden ikke skal have en offentlig adresse.",
    )

    class Meta:
        model = Page
        fields = ["slug", "header", "body", "background_image"]

    class Media:
        # Must re-list insert_image.js. `media_property` takes `getattr(cls, "Media")` as *the*
        # definition, and an inherited one counts — so declaring a Media here shadows
        # BodyEditorMixin's entirely and would silently drop the image toolbar from this form.
        js = (*BodyEditorMixin.Media.js, "cms/page_path.js")

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        # Pick the background from the library rather than typing a path. The model field stays a
        # CharField — no migration, no data change, only the widget differs. Whatever is stored today
        # is kept as an option, so the migrated /public/… paths round-trip untouched.
        current = self.instance.background_image if self.instance else ""
        choices: list[tuple[str, str]] = [("", "— ingen —")]
        choices += [(image.url, str(image)) for image in CmsImage.objects.all()]
        if current and current not in {value for value, _label in choices}:
            choices.insert(1, (current, f"{current} (nuværende)"))
        self.fields["background_image"] = forms.ChoiceField(
            choices=choices, required=False, label="Baggrundsbillede"
        )

        parent, segment = split_path(self.instance.slug if self.instance else None)
        self.fields["path_parent"] = forms.ChoiceField(
            choices=self._parent_choices(parent),
            required=False,
            initial=parent,
            label=self.declared_fields["path_parent"].label,
            help_text=self.declared_fields["path_parent"].help_text,
        )
        self.fields["path_segment"].initial = segment

        # Kept in the form but hidden, rather than dropped and assigned in save_model: a field the
        # form does not carry is excluded from `validate_unique`, which would turn a duplicate
        # address from a field error into a 500 IntegrityError. `construct_instance` reads
        # cleaned_data, so overwriting cleaned_data["slug"] in clean() is what actually gets saved.
        self.fields["slug"].widget = forms.HiddenInput()
        self.fields["slug"].required = False

    def _parent_choices(self, current_parent: str) -> list[tuple[str, str]]:
        """Top-level pages, offered as sections. Never the page itself — that would nest it in itself."""
        sections = (
            Page.objects.filter(slug__isnull=False)
            .exclude(slug="")
            .exclude(slug__contains="/")
            .exclude(pk=self.instance.pk)
            .order_by("slug")
        )
        choices: list[tuple[str, str]] = [("", "— øverste niveau —")]
        choices += [(page.slug or "", f"{page.header} (/{page.slug})") for page in sections]
        # Same reasoning as background_image above: a page currently filed under a section that has
        # no top-level Page row of its own must still round-trip untouched.
        if current_parent and current_parent not in {value for value, _label in choices}:
            choices.insert(1, (current_parent, f"{current_parent} (nuværende)"))
        return choices

    def clean(self) -> dict[str, Any]:
        """Compose the address, and report every problem with it on the field the editor can see.

        Errors deliberately never land on `slug`: it is hidden, and admin renders hidden-field
        errors in a detached list at the top of the page — which is how you get an editor staring at
        a complaint with no field attached to it.
        """
        super().clean()
        cleaned = self.cleaned_data
        parent = (cleaned.get("path_parent") or "").strip()
        segment = normalize_segment(cleaned.get("path_segment") or "")

        if parent and not segment:
            self.add_error(
                "path_segment",
                "Skriv den sidste del af adressen, når siden ligger i en sektion.",
            )
            return cleaned

        composed = join_path(parent, segment)
        if composed:
            try:
                validate_page_path(composed)
            except ValidationError as exc:
                self.add_error("path_segment", exc)
                return cleaned
            clash = Page.objects.filter(slug=composed).exclude(pk=self.instance.pk).first()
            if clash:
                self.add_error(
                    "path_segment",
                    f"Adressen /{composed} bruges allerede af siden “{clash.header}”.",
                )
                return cleaned

        # None, not "": `unique` allows any number of NULL addresses but only one empty string, so
        # storing "" would make the second address-less page a uniqueness error.
        cleaned["slug"] = composed or None
        return cleaned

    def clean_body(self) -> str | None:
        return clean_html(self.cleaned_data.get("body", ""))


class NewsItemAdminForm(BodyEditorMixin, forms.ModelForm):
    class Meta:
        model = NewsItem
        fields = ["title", "body", "published_at"]

    def clean_body(self) -> str | None:
        return clean_html(self.cleaned_data.get("body", ""))


class EventAdminForm(BodyEditorMixin, forms.ModelForm):
    class Meta:
        model = Event
        fields = ["title", "description", "starts_on"]

    def clean_description(self) -> str | None:
        return clean_html(self.cleaned_data.get("description", ""))


class ContentEditorAdmin(admin.ModelAdmin):
    """Base admin whose every access check requires one of the real CMS-editor roles (or superuser)."""

    def _may(self, request: HttpRequest) -> bool:
        return has_active_role(request.user, *CMS_EDITOR_ROLES)

    def has_module_permission(self, request: HttpRequest) -> bool:
        return self._may(request)

    def has_view_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return self._may(request)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return self._may(request)

    def has_change_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return self._may(request)

    def has_delete_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return self._may(request)


class SectionFilter(admin.SimpleListFilter):
    """Filter by top-level section, using the same prefix expression as the section sidebar."""

    title = "Sektion"
    parameter_name = "sektion"

    def lookups(self, request: HttpRequest, model_admin: admin.ModelAdmin) -> list[tuple[str, str]]:
        slugs = Page.objects.filter(slug__isnull=False).exclude(slug="").values_list("slug", flat=True)
        sections = sorted({slug.split("/", 1)[0] for slug in slugs if slug})
        return [(section, f"/{section}") for section in sections]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Page]) -> QuerySet[Page]:
        section = self.value()
        if not section:
            return queryset
        return queryset.filter(Q(slug=section) | Q(slug__startswith=section + "/"))


class VisibilityFilter(admin.SimpleListFilter):
    """Filter by how a page is reachable — the one-click answer to "hvad er gået i stykker?"."""

    title = "Synlighed"
    parameter_name = "synlighed"

    def lookups(self, request: HttpRequest, model_admin: admin.ModelAdmin) -> list[tuple[str, str]]:
        return list(STATUS_LABELS.items())

    def queryset(self, request: HttpRequest, queryset: QuerySet[Page]) -> QuerySet[Page]:
        wanted = self.value()
        if not wanted:
            return queryset
        # Reachability is not a column, so it cannot be expressed in SQL. Resolving it to a pk list
        # is fine at this size (~20 rows) and keeps a single definition of the predicate.
        by_pk = statuses(list(Page.objects.only("id", "slug", "body")))
        return queryset.filter(pk__in=[pk for pk, status in by_pk.items() if status == wanted])


@admin.register(Page)
class PageAdmin(ContentEditorAdmin):
    form = PageAdminForm
    list_display_links = ("header",)
    list_filter = (SectionFilter, VisibilityFilter)
    search_fields = ("header", "slug", "body")
    # By address, so sub-pages sort directly under the section they belong to — the way editors
    # picture the site. `list_select_related` is deliberately absent: Page has no foreign key.
    ordering = ("slug",)
    list_per_page = 100
    readonly_fields = ("historik",)
    fieldsets = (
        (
            "Adresse",
            {
                "fields": ("path_parent", "path_segment", "slug"),
                "description": "⚠ Ændrer du adressen, flytter siden. Den gamle adresse "
                "omdirigerer automatisk til den nye, så links og bogmærker bliver ved at "
                "virke — men links, der er skrevet ind i <em>andre</em> siders indhold, skal "
                "rettes i hånden.",
            },
        ),
        (None, {"fields": ("header", "background_image", "historik")}),
        (
            "Indhold (HTML)",
            {
                "fields": ("body",),
                "description": "Rå HTML — vises som den er. Brug knappen over feltet til at uploade "
                "og indsætte billeder. Gamle stier (/public/… eller /static/legacy/…) virker "
                "fortsat; de ligger under static/legacy/ (kør sync_cms_media efter behov).",
            },
        ),
    )

    # ---- changelist -------------------------------------------------------------------------
    def get_list_display(self, request: HttpRequest) -> tuple[Any, ...]:
        """Built per request so the reachability scan runs ONCE per page load, not once per row.

        `list_display` accepts plain callables, so the badge column can close over a precomputed map
        instead of asking the database per row — the trap CmsImageAdmin.usage documents below. The
        closure also keeps per-request state off `self`, which admin reuses process-wide.
        """
        by_pk = statuses(list(Page.objects.only("id", "slug", "body")))

        def synlighed(page: Page) -> str:
            status = by_pk.get(page.pk, "orphan")
            label = STATUS_LABELS[status]
            if status in PROBLEM_STATUSES:
                return format_html('<span style="color:#ba2121;font-weight:600">⚠ {}</span>', label)
            if status == "unrouted":
                return format_html('<span style="color:#666">{}</span>', label)
            return format_html('<span style="color:#2b6b2b">✓ {}</span>', label)

        synlighed.short_description = "Synlighed"  # type: ignore[attr-defined]
        return ("header", "live_url", synlighed, "sidst_rettet", "sidst_rettet_af")

    def get_queryset(self, request: HttpRequest) -> QuerySet[Page]:
        """Annotate the newest version's author and timestamp with subqueries, not per-row lookups."""
        newest = PageVersion.objects.filter(page=OuterRef("pk")).order_by("-created_at", "-id")
        return (
            super()
            .get_queryset(request)
            .annotate(
                last_edit=Subquery(newest.values("created_at")[:1]),
                last_editor_first=Subquery(newest.values("created_by__first_name")[:1]),
                last_editor_last=Subquery(newest.values("created_by__last_name")[:1]),
            )
        )

    @admin.display(description="Adresse", ordering="slug")
    def live_url(self, obj: Page) -> str:
        """The page's real URL, clickable — the fastest way to notice an edit did something odd."""
        url = obj.get_absolute_url()
        if not url:
            return "—"
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', url, url)

    @admin.display(description="Sidst rettet", ordering="last_edit")
    def sidst_rettet(self, obj: Page) -> str:
        edited = getattr(obj, "last_edit", None)
        return timezone.localtime(edited).strftime("%d-%m-%Y %H:%M") if edited else "—"

    @admin.display(description="Rettet af")
    def sidst_rettet_af(self, obj: Page) -> str:
        first = getattr(obj, "last_editor_first", "") or ""
        last = getattr(obj, "last_editor_last", "") or ""
        return f"{first} {last}".strip() or "—"

    # ---- permissions ------------------------------------------------------------------------
    def has_delete_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        """Only administrator may delete a page. Editing stays open to all four CMS roles.

        There is no soft-delete for a Page and no undo, so a mis-click here is unrecoverable — a
        strictly worse version of the accident this screen was rebuilt to prevent. Returning False
        also removes admin's "delete selected" action, so no `get_actions` override is needed.
        """
        return has_active_role(request.user, Role.ADMINISTRATOR)

    def view_on_site(self, obj: Page) -> str | None:
        return obj.get_absolute_url() or None  # None => admin renders no button, which is right

    # ---- saving -----------------------------------------------------------------------------
    def save_model(self, request: HttpRequest, obj: Page, form: forms.ModelForm, change: bool) -> None:
        """Save, then keep the old address alive and store a restorable snapshot.

        The pre-mutation database read is the only trustworthy source of the *old* address and body:
        `obj` has already been overwritten by `construct_instance`, and `form.initial` holds the
        widget's view of the value rather than what the row actually said.
        """
        author = current_resident(request)
        previous = Page.objects.filter(pk=obj.pk).first() if change else None

        # First edit of a page that predates version history: capture what it said beforehand, or
        # that one save would be unrecoverable — the exact gap that caused this work.
        if previous and not previous.versions.exists():
            snapshot_page(previous, None, note="Før første redigering (forfatter ukendt)")

        super().save_model(request, obj, form, change)

        snapshot_page(obj, author, note="Redigeret i CMS")
        if previous:
            record_slug_change(obj, previous.slug, author)
            if previous.slug and obj.slug and previous.slug != obj.slug:
                messages.info(
                    request,
                    f"Adressen er ændret. /{previous.slug} sender nu automatisk videre til /{obj.slug}.",
                )

        # The warning that would have caught the original incident as it happened.
        if obj.slug and not is_reachable(obj):
            messages.warning(
                request,
                "Siden står ikke i nogen menu og kan kun nås via et direkte link. "
                "Læg den under en sektion, der vises i menuen, hvis den skal kunne findes.",
            )

    # ---- version history --------------------------------------------------------------------
    @admin.display(description="Historik")
    def historik(self, obj: Page | None) -> str:
        if obj is None or obj.pk is None:
            return "Historikken oprettes, når siden er gemt første gang."
        url = reverse("admin:cms_page_versions", args=[obj.pk])
        return format_html('<a href="{}">Se og gendan tidligere versioner</a>', url)

    def get_urls(self) -> list[URLPattern]:
        # Declared here so they sit in the admin URL namespace and reuse this class's role check —
        # the same gate as every other CMS write (see CmsImageAdmin.get_urls).
        return [
            path(
                "<int:pk>/versioner/",
                self.admin_site.admin_view(self.versions_view),
                name="cms_page_versions",
            ),
            path(
                "<int:pk>/versioner/<int:version_id>/gendan",
                self.admin_site.admin_view(self.restore_view),
                name="cms_page_restore",
            ),
            *super().get_urls(),
        ]

    def versions_view(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Read a page's history: what changed, who changed it, and a button to put it back."""
        if not self._may(request):
            raise PermissionDenied
        page_obj = get_object_or_404(Page, pk=pk)
        versions = list(page_obj.versions.select_related("created_by"))

        # Each version is paired with its predecessor, so an entry answers "what did THIS save
        # change?" rather than just showing a wall of content.
        entries = []
        for index, version in enumerate(versions):
            older = versions[index + 1] if index + 1 < len(versions) else None
            entries.append(
                {
                    "version": version,
                    "is_current": index == 0,
                    "changes": self._short_field_changes(older, version),
                    "body_diff": line_diff(older.body if older else "", version.body),
                    "is_oldest": older is None,
                }
            )
        return render(
            request,
            "admin/cms/page/versions.html",
            {
                **self.admin_site.each_context(request),
                "title": f"Historik — {page_obj.header}",
                "cms_page": page_obj,
                "entries": entries,
                "opts": self.model._meta,
            },
        )

    @staticmethod
    def _short_field_changes(older: PageVersion | None, newer: PageVersion) -> list[dict[str, str]]:
        """Before/after rows for the one-line fields — a plain pair reads better than a diff here."""
        if older is None:
            return []
        labels = (("slug", "Adresse"), ("header", "Overskrift"), ("background_image", "Baggrund"))
        rows = []
        for attr, label in labels:
            before, after = getattr(older, attr), getattr(newer, attr)
            if before != after:
                rows.append({"label": label, "before": before or "—", "after": after or "—"})
        return rows

    def restore_view(self, request: HttpRequest, pk: int, version_id: int) -> HttpResponse:
        """Put a previous version back, without ever deleting history to do it."""
        if not self._may(request):
            raise PermissionDenied
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])

        page_obj = get_object_or_404(Page, pk=pk)
        version = get_object_or_404(PageVersion, pk=version_id, page_id=pk)
        author = current_resident(request)

        with transaction.atomic():
            # Snapshot first: a restore is itself an edit, and undoing an unwanted restore has to be
            # possible too. History only ever grows.
            snapshot_page(page_obj, author, note="Før gendannelse")

            old_slug = page_obj.slug
            page_obj.header = version.header
            # Through the sanitizer again: a snapshot taken before the allowlist was tightened must
            # not become a way back in for markup that is no longer permitted.
            # `or ""`: clean_html passes falsy input straight back, and body is a non-null column.
            page_obj.body = clean_html(version.body) or ""
            page_obj.background_image = version.background_image

            if restored_slug := self._restorable_slug(request, page_obj, version):
                page_obj.slug = restored_slug

            page_obj.save()
            # So the address it is being moved away from also keeps working.
            record_slug_change(page_obj, old_slug, author)
            stamp = timezone.localtime(version.created_at).strftime("%d-%m-%Y %H:%M")
            snapshot_page(page_obj, author, note=f"Gendannet fra version af {stamp}")

        # Keep the built-in admin timeline coherent with this one rather than forking the record.
        self.log_change(request, page_obj, "Gendannede en tidligere version.")
        messages.success(request, "Siden er gendannet til den valgte version.")
        return HttpResponseRedirect(reverse("admin:cms_page_change", args=[pk]))

    def _restorable_slug(self, request: HttpRequest, page_obj: Page, version: PageVersion) -> str | None:
        """The snapshot's address if it can be used again, else None (and say why).

        A snapshot may hold an address that is no longer legal (anything from before cms.paths
        existed) or one another page has since taken. Neither is a reason to refuse the restore —
        the body is what people come here for — so the content goes back and the address does not.
        """
        wanted = version.slug or None
        if not wanted or wanted == page_obj.slug:
            return None
        try:
            validate_page_path(wanted)
        except ValidationError:
            messages.warning(
                request,
                f"Den gamle adresse /{wanted} er ikke en gyldig adresse længere, "
                "så siden beholder sin nuværende adresse. Indholdet er gendannet.",
            )
            return None
        if Page.objects.filter(slug=wanted).exclude(pk=page_obj.pk).exists():
            messages.warning(
                request,
                f"Den gamle adresse /{wanted} bruges nu af en anden side, "
                "så siden beholder sin nuværende adresse. Indholdet er gendannet.",
            )
            return None
        return wanted


@admin.register(NewsItem)
class NewsItemAdmin(ContentEditorAdmin):
    form = NewsItemAdminForm
    list_display = ("title", "published_at")
    list_filter = ("published_at",)  # Django's date filter, Danish for free at LANGUAGE_CODE="da"
    date_hierarchy = "published_at"
    search_fields = ("title", "body")
    ordering = ("-published_at",)


class UpcomingFilter(admin.SimpleListFilter):
    """Kommende / afholdt — the same split cms.views.events_news renders the public page with."""

    title = "Tidspunkt"
    parameter_name = "tidspunkt"

    def lookups(self, request: HttpRequest, model_admin: admin.ModelAdmin) -> list[tuple[str, str]]:
        return [("kommende", "Kommende"), ("afholdt", "Afholdt")]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Event]) -> QuerySet[Event]:
        today = timezone.localdate()
        if self.value() == "kommende":
            return queryset.filter(starts_on__gte=today)
        if self.value() == "afholdt":
            return queryset.filter(starts_on__lt=today)
        return queryset


@admin.register(Event)
class EventAdmin(ContentEditorAdmin):
    form = EventAdminForm
    list_display = ("title", "starts_on")
    list_filter = (UpcomingFilter,)
    date_hierarchy = "starts_on"
    search_fields = ("title", "description")
    ordering = ("starts_on",)


class CmsImageAdminForm(forms.ModelForm):
    class Meta:
        model = CmsImage
        fields = ["file", "caption"]

    def clean_file(self) -> object:
        upload = self.cleaned_data.get("file")
        # Validate only a *new* upload: re-saving an existing row hands back a FieldFile, which has
        # no content_type and would fail a check aimed at browser uploads.
        if upload and hasattr(upload, "content_type"):
            validate_image_upload(upload, settings.CMS_IMAGE_MAX_MB)
        return upload


@admin.register(CmsImage)
class CmsImageAdmin(ContentEditorAdmin):
    """The image library: upload here, then insert from the toolbar above any body field.

    Replaces the old workflow of committing a file to the repo and hand-writing its path.
    """

    form = CmsImageAdminForm
    list_display = ("thumbnail", "__str__", "url_display", "uploaded_at", "uploaded_by")
    list_display_links = ("thumbnail", "__str__")
    # `uploaded_by` is a column, so without the join this list costs one query per image.
    list_select_related = ("uploaded_by",)
    list_filter = ("uploaded_by",)
    date_hierarchy = "uploaded_at"
    search_fields = ("caption", "file")
    readonly_fields = ("uploaded_at", "uploaded_by", "usage")

    @admin.display(description="")
    def thumbnail(self, obj: CmsImage) -> str:
        if not obj.file:
            return ""
        return format_html('<img src="{}" alt="" style="height:40px;border-radius:4px">', obj.url)

    @admin.display(description="URL")
    def url_display(self, obj: CmsImage) -> str:
        # user-select:all so one click grabs the whole path. The toolbar is the normal route; this
        # stays the escape hatch for hand-written HTML.
        return format_html('<code style="user-select:all">{}</code>', obj.url)

    @admin.display(description="Bruges på")
    def usage(self, obj: CmsImage) -> str:
        """Where the image is referenced, so deleting it does not silently 404 a live page.

        Computed only on the change form (one object at a time) — running four scans per row in the
        list view would be a table scan per image for information nobody is reading there.
        """
        if not obj.file:
            return "—"
        used = [
            *(f"Side: {p.header}" for p in Page.objects.filter(body__icontains=obj.url)),
            *(f"Nyhed: {n.title}" for n in NewsItem.objects.filter(body__icontains=obj.url)),
            *(f"Begivenhed: {e.title}" for e in Event.objects.filter(description__icontains=obj.url)),
            *(f"Baggrund: {p.header}" for p in Page.objects.filter(background_image=obj.url)),
        ]
        return ", ".join(used) if used else "Ingen steder endnu"

    def save_model(self, request: HttpRequest, obj: CmsImage, form: forms.ModelForm, change: bool) -> None:
        if not change:
            obj.uploaded_by = current_resident(request)
        super().save_model(request, obj, form, change)

    def get_urls(self) -> list[URLPattern]:
        # What the toolbar talks to. Declared here so they sit in the admin URL namespace and reuse
        # this class's role check — the same gate as every other CMS write.
        return [
            path("toolbar/list", self.admin_site.admin_view(self.toolbar_list), name="cms_image_list"),
            path(
                "toolbar/upload",
                self.admin_site.admin_view(self.toolbar_upload),
                name="cms_image_upload",
            ),
            *super().get_urls(),
        ]

    def toolbar_list(self, request: HttpRequest) -> HttpResponse:
        if not self._may(request):
            return JsonResponse({"error": "forbidden"}, status=403)
        return JsonResponse(
            {"images": [{"url": i.url, "label": str(i), "alt": i.caption} for i in CmsImage.objects.all()]}
        )

    def toolbar_upload(self, request: HttpRequest) -> HttpResponse:
        if not self._may(request):
            return JsonResponse({"error": "forbidden"}, status=403)
        if request.method != "POST":
            return JsonResponse({"error": "Kun POST."}, status=405)
        upload = request.FILES.get("file")
        if not upload:
            return JsonResponse({"error": "Vælg en fil."}, status=400)
        try:
            validate_image_upload(upload, settings.CMS_IMAGE_MAX_MB)
        except ValidationError as exc:
            return JsonResponse({"error": " ".join(exc.messages)}, status=400)
        image = CmsImage.objects.create(
            file=upload,
            caption=(request.POST.get("caption") or "").strip(),
            uploaded_by=current_resident(request),
        )
        return JsonResponse({"url": image.url, "label": str(image), "alt": image.caption}, status=201)


@admin.register(PageRedirect)
class PageRedirectAdmin(ContentEditorAdmin):
    """Old addresses, so an editor can see what still points where — and fix one by hand.

    Add and change stay open: these rows are ordinary content decisions ("send the old flyer's URL
    to the new page"). Deleting one only stops an old link working, which is why it is not held to
    the administrator-only rule that PageAdmin.has_delete_permission applies to pages themselves.
    """

    list_display = ("old_path", "target", "created_at", "created_by")
    list_select_related = ("page", "created_by")
    search_fields = ("old_path", "page__header", "page__slug")
    readonly_fields = ("created_at", "created_by")
    ordering = ("old_path",)

    @admin.display(description="Sender videre til", ordering="page__slug")
    def target(self, obj: PageRedirect) -> str:
        url = obj.page.get_absolute_url()
        if not url:
            return "— (siden har ingen adresse)"
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', url, url)

    def save_model(
        self, request: HttpRequest, obj: PageRedirect, form: forms.ModelForm, change: bool
    ) -> None:
        if not change:
            obj.created_by = current_resident(request)
        super().save_model(request, obj, form, change)


@admin.register(PageVersion)
class PageVersionAdmin(ContentEditorAdmin):
    """ "Hvem rettede hvad" across every page — and the only site-wide change log worth having.

    Read-only in every direction. History that can be edited is not history, and restoring is done
    from a page's own Historik screen (PageAdmin.restore_view), which snapshots before it acts.

    Note why this exists rather than a ModelAdmin for django.contrib.admin's LogEntry: that model
    logs every object touched through /django-admin/, and its `object_repr` is a `__str__` — for an
    admissions.Application that is an applicant's name, and for a RoleAssignment it is personnel
    data. CMS_EDITOR_ROLES includes `pr`, which has no business reading either. This table cannot
    leak across apps because it only ever holds CMS pages.
    """

    list_display = ("created_at", "page_link", "header", "slug", "created_by", "note")
    list_select_related = ("page", "created_by")
    list_filter = ("created_by",)
    date_hierarchy = "created_at"
    search_fields = ("header", "slug", "note", "page__header")
    ordering = ("-created_at", "-id")

    @admin.display(description="Side", ordering="page__header")
    def page_link(self, obj: PageVersion) -> str:
        if obj.page is None:
            return "(siden er slettet)"
        url = reverse("admin:cms_page_change", args=[obj.page.pk])
        return format_html('<a href="{}">{}</a>', url, obj.page.header)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False
