"""The Hjemmeside editor (/hjemmeside/): where gahk.dk's content is edited.

Every write goes through cms.services, which cleans the input, keeps page versions, records a
ContentChange and publishes to gahk.dk after the commit. The views only check who may do what, shape
the forms' input into the services' `data`, and show the result.

Who: CMS_EDITOR_ROLES (administrator, indstilling, inspektion, pr) and superusers, through the same
role check as the rest of the intranet (so "vis siden som rolle" applies here too). Deleting a page
and asking gahk.dk to update its code are for administrators only.

gahk.dk's "Rediger siden" links here (/hjemmeside/sider/<id>/ and /hjemmeside/), shown to browsers
that carry the `gahk_editor` hint cookie, which cms.middleware keeps for editors. It grants
nothing: these views check the role.
"""

import datetime
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.forms import Form
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from residents.models import Resident, Role
from residents.permissions import CMS_EDITOR_ROLES, current_resident, request_has_role, role_required

from . import services
from .diff import DiffRow, html_diff
from .forms import CaptionForm, EventForm, NewsForm, PageForm, RedirectForm, SiteSettingsForm
from .gahk_web import GahkWebDisabled, GahkWebError
from .legacy_images import legacy_state
from .models import (
    FRONT_PAGE_ID,
    CmsImage,
    ContentChange,
    Event,
    GahkWebSync,
    MenuItem,
    NewsItem,
    Page,
    PageRedirect,
    Publication,
    SiteSettings,
)
from .paths import split_path
from .sanitize import clean_html

View = Callable[..., HttpResponseBase]

# How long the overview waits for a requested publish or code update before it stops asking.
PENDING_TIMEOUT = datetime.timedelta(minutes=3)
PUBLISH_PENDING_KEY = "cms_publish_requested"
UPDATE_PENDING_KEY = "cms_update_requested"

# Only CMS editors (the effective roles, so a role preview applies here as everywhere).
editor_view: Callable[[View], View] = role_required(*CMS_EDITOR_ROLES)


def is_administrator(request: HttpRequest) -> bool:
    return request_has_role(request, Role.ADMINISTRATOR)


def _require_administrator(request: HttpRequest) -> None:
    if not is_administrator(request):
        raise PermissionDenied


def apply_service_errors(form: Form, exc: ValidationError, field_map: dict[str, str]) -> None:
    """Put a cms.services ValidationError on the form fields it names (non-field otherwise)."""
    if hasattr(exc, "error_dict"):
        for name, errors in exc.error_dict.items():
            target = field_map.get(name, name)
            for error in errors:
                form.add_error(target if target in form.fields else None, error)
    else:
        form.add_error(None, exc)


def public_url(path: str = "") -> str:
    """A page's address on gahk.dk (settings.PUBLIC_SITE_URL)."""
    return f"{settings.PUBLIC_SITE_URL.rstrip('/')}/{path.lstrip('/')}"


def _page_path(page: Page) -> str | None:
    """The address gahk.dk shows the page at ("" for the front page), None for none."""
    if page.pk == FRONT_PAGE_ID:
        return ""
    return page.slug or None


# ------------------------------------------------------------------------------------- overview
def _pending(request: HttpRequest, key: str, done: Callable[[datetime.datetime], bool]) -> str:
    """ "waiting" while a requested publish/update has not been answered, "timeout" once (after
    PENDING_TIMEOUT), "" otherwise. The request time lives in the session, so it is per editor."""
    raw = request.session.get(key)
    since = parse_datetime(raw) if isinstance(raw, str) else None
    if since is None:
        return ""
    if done(since):
        del request.session[key]
        return ""
    if timezone.now() - since > PENDING_TIMEOUT:
        del request.session[key]
        return "timeout"
    return "waiting"


# The kinds of change that alter what gahk.dk shows (an image only does so through a page using it).
PUBLISHED_KINDS = (
    ContentChange.Kind.PAGE,
    ContentChange.Kind.NEWS,
    ContentChange.Kind.EVENT,
    ContentChange.Kind.MENU,
    ContentChange.Kind.REDIRECT,
    ContentChange.Kind.SETTINGS,
)


def _in_sync(sync: GahkWebSync) -> bool:
    """gahk.dk shows the newest content: it reports the revision of the last settled publish (sent,
    or skipped as already there), and no content change has been saved since that publish began.

    The second half catches a save whose publish is still queued, failed to queue, or waits for a
    stopped worker. A query rather than build_snapshot(), because the status card polls every 3 s.
    """
    settled = Publication.objects.filter(
        status__in=(Publication.Status.OK, Publication.Status.SKIPPED)
    ).first()
    if settled is None or not sync.remote_revision or sync.remote_revision != settled.revision:
        return False
    return not (
        ContentChange.objects.filter(at__gt=settled.started_at, kind__in=PUBLISHED_KINDS)
        .exclude(action=ContentChange.Action.PUBLISH)
        .exists()
    )


def _status_context(request: HttpRequest) -> dict[str, Any]:
    sync = GahkWebSync.load()
    publications = list(Publication.objects.select_related("triggered_by")[:5])
    last_ok = Publication.objects.filter(status=Publication.Status.OK).first()
    publish_state = _pending(
        request,
        PUBLISH_PENDING_KEY,
        lambda since: (
            Publication.objects.filter(started_at__gte=since)
            .exclude(status=Publication.Status.RUNNING)
            .exists()
        ),
    )
    update_state = _pending(
        request,
        UPDATE_PENDING_KEY,
        lambda since: sync.update_checked_at is not None and sync.update_checked_at >= since,
    )
    return {
        "enabled": bool(settings.GAHK_WEB_KEY),
        "sync": sync,
        "latest": publications[0] if publications else None,
        "publications": publications,
        "last_ok": last_ok,
        "in_sync": _in_sync(sync),
        "update_waiting": sync.update_available_version
        and sync.remote_version
        and sync.update_available_version != sync.remote_version,
        "publish_state": publish_state,
        "update_state": update_state,
        "polling": "waiting" in (publish_state, update_state),
        "is_admin": is_administrator(request),
        "gahk_web_url": settings.GAHK_WEB_URL,
    }


@editor_view
def overview(request: HttpRequest) -> HttpResponse:
    context = _status_context(request)
    context.update(
        changes=ContentChange.objects.all()[:10],
        unreachable=services.unreachable_pages(),
        legacy=legacy_state(),
        counts={
            "pages": Page.objects.count(),
            "news": NewsItem.objects.count(),
            "events": Event.objects.filter(starts_on__gte=timezone.localdate()).count(),
            "images": CmsImage.objects.count(),
        },
        public_site_url=settings.PUBLIC_SITE_URL,
    )
    return render(request, "cms/overview.html", context)


@editor_view
def status(request: HttpRequest) -> HttpResponse:
    """The overview's status card on its own, which it polls while a publish or update is pending."""
    return render(request, "cms/_status.html", _status_context(request))


@editor_view
@require_POST
def publish_now(request: HttpRequest) -> HttpResponse:
    if not settings.GAHK_WEB_KEY:
        messages.error(request, "gahk.dk er ikke forbundet, så der kan ikke udgives.")
        return redirect("cms:overview")
    services.request_publish(current_resident(request))
    request.session[PUBLISH_PENDING_KEY] = timezone.now().isoformat()
    messages.success(request, "gahk.dk udgives igen nu.")
    return redirect("cms:overview")


@editor_view
@require_POST
def update_now(request: HttpRequest) -> HttpResponse:
    """ "Opdatér gahk.dk nu": the webhost has no cron, so this and the 15-minute beat task are the only
    ways its code is updated."""
    _require_administrator(request)
    if not settings.GAHK_WEB_KEY:
        messages.error(request, "gahk.dk er ikke forbundet, så den kan ikke bedes om at opdatere.")
        return redirect("cms:overview")
    services.request_gahk_web_update(current_resident(request))
    request.session[UPDATE_PENDING_KEY] = timezone.now().isoformat()
    messages.success(request, "gahk.dk er bedt om at opdatere sin kode. Svaret vises her om lidt.")
    return redirect("cms:overview")


# ---------------------------------------------------------------------------------------- pages
@dataclass
class PageRow:
    page: Page
    path: str | None
    live_url: str
    # "front", "menu" (in the menu itself), "section" (its section is), "none" (not reachable),
    # "hidden" (no address: not on gahk.dk at all)
    menu_state: str

    @property
    def section(self) -> str:
        return (self.page.slug or "").split("/", 1)[0]


@dataclass
class PageSection:
    title: str
    rows: list[PageRow] = field(default_factory=list)


def _menu_state(page: Page, menu: set[str]) -> str:
    if page.pk == FRONT_PAGE_ID:
        return "front"
    if not page.slug:
        return "hidden"
    if page.slug in menu:
        return "menu"
    return "section" if services.is_reachable(page, menu) else "none"


def page_sections(pages: Iterable[Page]) -> list[PageSection]:
    """Pages grouped by the first segment of their address: the front page, each section, and the
    pages without an address last."""
    menu = services.menu_paths()
    front = PageSection("Forsiden")
    hidden = PageSection("Uden adresse (vises ikke på gahk.dk)")
    sections: dict[str, PageSection] = {}
    for page in pages:
        path = _page_path(page)
        row = PageRow(page, path, public_url(path) if path is not None else "", _menu_state(page, menu))
        if page.pk == FRONT_PAGE_ID:
            front.rows.append(row)
        elif not page.slug:
            hidden.rows.append(row)
        else:
            top = page.slug.split("/", 1)[0]
            sections.setdefault(top, PageSection(f"/{top}")).rows.append(row)
    for section in sections.values():
        # The section's own page first, then its sub-pages by address.
        section.rows.sort(key=lambda row: ("/" in (row.page.slug or ""), row.page.slug or ""))
    ordered = [front] if front.rows else []
    ordered += [sections[key] for key in sorted(sections)]
    if hidden.rows:
        ordered.append(hidden)
    return ordered


@editor_view
def page_list(request: HttpRequest) -> HttpResponse:
    pages = Page.objects.select_related("updated_by").order_by("slug", "pk")
    return render(
        request,
        "cms/page_list.html",
        {"sections": page_sections(pages), "page_count": pages.count()},
    )


def _page_initial(page: Page | None) -> dict[str, Any]:
    if page is None:
        return {}
    parent, segment = split_path(page.slug)
    return {
        "header": page.header,
        "path_parent": parent,
        "path_segment": segment,
        "description": page.description,
        "background_image": page.background_image,
        "body": page.body,
    }


def _stamp(moment: datetime.datetime | None) -> str:
    return moment.isoformat() if moment else ""


def _after_page_save(request: HttpRequest, page: Page, old_slug: str | None) -> None:
    published = " gahk.dk opdateres om et øjeblik." if settings.GAHK_WEB_KEY else ""
    messages.success(request, f"Siden “{page.header}” er gemt.{published}")
    if old_slug and page.slug and old_slug != page.slug:
        messages.info(request, f"Den gamle adresse /{old_slug} sender nu besøgende videre til /{page.slug}.")
    if page.pk != FRONT_PAGE_ID:
        if not page.slug:
            messages.warning(request, "Siden har ingen adresse, så den vises ikke på gahk.dk.")
        elif not services.is_reachable(page):
            messages.warning(
                request,
                "Siden kan ikke findes fra menuen på gahk.dk. Tilføj den under Menu, eller læg den "
                "under en sektion, der står i menuen.",
            )


@editor_view
def page_edit(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    page = get_object_or_404(Page.objects.select_related("updated_by"), pk=pk) if pk is not None else None
    loaded_at = _stamp(page.updated_at) if page else ""
    if request.method == "POST":
        form = PageForm(request.POST, page=page)
        if form.is_valid():
            posted_stamp = request.POST.get("loaded_at", "")
            if page is not None and posted_stamp and posted_stamp != loaded_at:
                # Somebody saved the page after this form was opened. The form comes back with this
                # editor's text and the new stamp, so pressing Gem again overwrites knowingly.
                who = page.updated_by.full_name if page.updated_by else "en anden"
                when = timezone.localtime(page.updated_at).strftime("%H:%M")
                form.add_error(
                    None,
                    f"{who} har gemt siden kl. {when}, efter du åbnede den. Dine ændringer er ikke gemt. "
                    "Se hvad der er ændret under Historik, eller tryk Gem igen for at overskrive.",
                )
            else:
                old_slug = page.slug if page else None
                try:
                    saved = services.save_page(page, form.service_data(), current_resident(request))
                except ValidationError as exc:
                    apply_service_errors(form, exc, PageForm.SERVICE_FIELDS)
                else:
                    if page is not None and saved.updated_at == page.updated_at:
                        messages.info(request, "Der var ingen ændringer at gemme.")
                    else:
                        _after_page_save(request, saved, old_slug)
                    return redirect("cms:page_edit", saved.pk)
    else:
        form = PageForm(initial=_page_initial(page), page=page)
    path = _page_path(page) if page else None
    return render(
        request,
        "cms/page_form.html",
        {
            "form": form,
            "page": page,
            "loaded_at": loaded_at,
            "images": _library(),
            "live_url": public_url(path) if path is not None else "",
            "redirects": list(page.redirects.all()) if page else [],
            "is_admin": is_administrator(request),
            "can_delete": page is not None and page.pk != FRONT_PAGE_ID and is_administrator(request),
            "preview_kind": "page",
        },
    )


@editor_view
def page_delete(request: HttpRequest, pk: int) -> HttpResponse:
    _require_administrator(request)
    page = get_object_or_404(Page, pk=pk)
    if page.pk == FRONT_PAGE_ID:
        messages.error(request, "Forsiden kan ikke slettes.")
        return redirect("cms:page_edit", page.pk)
    redirects = list(page.redirects.all())
    if request.method == "POST":
        header = page.header
        try:
            services.delete_page(page, current_resident(request))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("cms:page_edit", pk)
        messages.success(request, f"Siden “{header}” er slettet.")
        return redirect("cms:page_list")
    return render(
        request,
        "cms/page_delete.html",
        {"page": page, "redirects": redirects, "in_menu": page.slug in services.menu_paths()},
    )


@dataclass
class VersionEntry:
    version: Any
    previous: Any
    body_diff: list[DiffRow]
    changed: list[tuple[str, str, str]]  # (label, before, after) for the short fields


VERSION_FIELDS = (
    ("header", "Titel"),
    ("slug", "Adresse"),
    ("description", "Beskrivelse"),
    ("background_image", "Baggrundsbillede"),
)


@editor_view
def page_history(request: HttpRequest, pk: int) -> HttpResponse:
    page = get_object_or_404(Page, pk=pk)
    versions = list(page.versions.select_related("created_by"))
    entries = []
    for index, version in enumerate(versions):
        previous = versions[index + 1] if index + 1 < len(versions) else None
        changed = []
        if previous is not None:
            for name, label in VERSION_FIELDS:
                before, after = getattr(previous, name), getattr(version, name)
                if before != after:
                    changed.append((label, before or "–", after or "–"))
        body_diff = html_diff(previous.body, version.body) if previous is not None else []
        entries.append(VersionEntry(version, previous, body_diff, changed))
    return render(request, "cms/page_history.html", {"page": page, "entries": entries})


@editor_view
@require_POST
def page_restore(request: HttpRequest, pk: int, version_pk: int) -> HttpResponse:
    page = get_object_or_404(Page, pk=pk)
    version = get_object_or_404(page.versions, pk=version_pk)
    result = services.restore_version(page, version, current_resident(request))
    stamp = timezone.localtime(version.created_at).strftime("%d-%m-%Y %H:%M")
    messages.success(request, f"Versionen fra {stamp} er gendannet.")
    for warning in result.warnings:
        messages.warning(request, warning)
    return redirect("cms:page_edit", page.pk)


# -------------------------------------------------------------------------------------- preview
_HEAD_RE = re.compile(r"<head[^>]*>", re.IGNORECASE)
_BASE_RE = re.compile(r"<base\s", re.IGNORECASE)

# The preview is gahk.dk's HTML shown on intern's origin, so it is never allowed to run a script
# here: as a page of its own (no JavaScript) this header sandboxes it; in the editor it sits in an
# <iframe sandbox="allow-same-origin …"> without allow-scripts. Same-origin is kept so the editor's
# session reaches /media/cms/, where a draft's images still live.
PREVIEW_CSP = "sandbox allow-same-origin allow-popups allow-popups-to-escape-sandbox"


def with_base(html: str, base: str) -> str:
    """`html` with a <base href> so its own relative links (gahk.dk's /static/…) load from gahk.dk.
    gahk.dk's preview brings one; this only adds it when it does not."""
    if _BASE_RE.search(html):
        return html
    tag = f'<base href="{base.rstrip("/")}/">'
    if match := _HEAD_RE.search(html):
        return html[: match.end()] + tag + html[match.end() :]
    return tag + html


PREVIEW_KINDS = ("page", "news", "event")


def _preview_form(
    request: HttpRequest, kind: str
) -> tuple[PageForm | NewsForm | EventForm, Page | NewsItem | Event | None]:
    raw_id = request.POST.get("preview_id", "")
    pk = int(raw_id) if raw_id.isdigit() else None
    if kind == "page":
        page = Page.objects.filter(pk=pk).first() if pk else None
        return PageForm(request.POST, page=page), page
    if kind == "news":
        return NewsForm(request.POST), NewsItem.objects.filter(pk=pk).first() if pk else None
    return EventForm(request.POST), Event.objects.filter(pk=pk).first() if pk else None


@editor_view
@require_POST
def preview(request: HttpRequest) -> HttpResponse:
    """The unsaved form as gahk.dk would show it, or, when gahk.dk cannot be asked, the text alone in
    the intranet's styling with a note saying so. Nothing is saved."""
    kind = request.POST.get("preview_kind", "")
    if kind not in PREVIEW_KINDS:
        return HttpResponseBadRequest("Ukendt forhåndsvisning.")
    form, instance = _preview_form(request, kind)
    data = form.preview_data()
    try:
        html = services.render_preview(kind, data, instance)
        source = "gahk.dk"
    except GahkWebError as exc:
        title = data.get("header") or data.get("title") or ""
        body = data.get("body") if kind != "event" else data.get("description")
        html = render_to_string(
            "cms/preview_local.html",
            {
                "title": title,
                "body": clean_html(str(body or "")) or "",
                "disabled": isinstance(exc, GahkWebDisabled),
                "kind": kind,
            },
            request=request,
        )
        source = "local"
    else:
        html = with_base(html, settings.GAHK_WEB_URL)
    response = HttpResponse(html)
    response["Content-Security-Policy"] = PREVIEW_CSP
    response["Cache-Control"] = "no-store"
    response["X-CMS-Preview"] = source
    return response


# ------------------------------------------------------------------------------ news and events
@editor_view
def news_list(request: HttpRequest) -> HttpResponse:
    page_obj = Paginator(NewsItem.objects.all(), 30).get_page(request.GET.get("side"))
    return render(request, "cms/news_list.html", {"page_obj": page_obj})


def _item_edit(
    request: HttpRequest,
    *,
    instance: NewsItem | Event | None,
    form_class: type[NewsForm | EventForm],
    initial: dict[str, Any],
    save: Callable[[Any, dict[str, Any], Resident], NewsItem | Event],
    template: str,
    kind: str,
    edit_url: str,
    saved_message: str,
) -> HttpResponse:
    if request.method == "POST":
        form = form_class(request.POST)
        if form.is_valid():
            try:
                saved = save(instance, form.service_data(), current_resident(request))
            except ValidationError as exc:
                apply_service_errors(form, exc, form.SERVICE_FIELDS)
            else:
                published = " gahk.dk opdateres om et øjeblik." if settings.GAHK_WEB_KEY else ""
                messages.success(request, f"{saved_message}{published}")
                return redirect(edit_url, saved.pk)
    else:
        form = form_class(initial=initial)
    return render(
        request, template, {"form": form, "item": instance, "images": _library(), "preview_kind": kind}
    )


@editor_view
def news_edit(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    item = get_object_or_404(NewsItem, pk=pk) if pk is not None else None
    initial: dict[str, Any] = {}
    if item is not None:
        initial = {
            "title": item.title,
            "published_at": timezone.localtime(item.published_at),
            "body": item.body,
        }
    return _item_edit(
        request,
        instance=item,
        form_class=NewsForm,
        initial=initial,
        save=services.save_news,
        template="cms/news_form.html",
        kind="news",
        edit_url="cms:news_edit",
        saved_message="Nyheden er gemt.",
    )


@editor_view
@require_POST
def news_delete(request: HttpRequest, pk: int) -> HttpResponse:
    item = get_object_or_404(NewsItem, pk=pk)
    services.delete_news(item, current_resident(request))
    messages.success(request, f"Nyheden “{item.title}” er slettet.")
    return redirect("cms:news_list")


@editor_view
def event_list(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()
    upcoming = Event.objects.filter(starts_on__gte=today).order_by("starts_on")
    past = Paginator(Event.objects.filter(starts_on__lt=today).order_by("-starts_on"), 30).get_page(
        request.GET.get("side")
    )
    return render(request, "cms/event_list.html", {"upcoming": upcoming, "past": past})


@editor_view
def event_edit(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    item = get_object_or_404(Event, pk=pk) if pk is not None else None
    initial: dict[str, Any] = {}
    if item is not None:
        initial = {"title": item.title, "starts_on": item.starts_on, "description": item.description}
    return _item_edit(
        request,
        instance=item,
        form_class=EventForm,
        initial=initial,
        save=services.save_event,
        template="cms/event_form.html",
        kind="event",
        edit_url="cms:event_edit",
        saved_message="Begivenheden er gemt.",
    )


@editor_view
@require_POST
def event_delete(request: HttpRequest, pk: int) -> HttpResponse:
    item = get_object_or_404(Event, pk=pk)
    services.delete_event(item, current_resident(request))
    messages.success(request, f"Begivenheden “{item.title}” er slettet.")
    return redirect("cms:event_list")


# --------------------------------------------------------------------------------------- images
def _library() -> list[CmsImage]:
    return [image for image in CmsImage.objects.all() if image.file]


def image_json(image: CmsImage) -> dict[str, Any]:
    return {"id": image.pk, "url": image.url, "caption": image.caption, "name": str(image)}


def _upload_errors(exc: ValidationError) -> str:
    if hasattr(exc, "error_dict"):
        return " ".join(message for errors in exc.message_dict.values() for message in errors)
    return " ".join(exc.messages)


@editor_view
def image_list(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        files = request.FILES.getlist("file")
        caption = request.POST.get("caption", "")
        if not files:
            messages.error(request, "Vælg et eller flere billeder.")
        uploaded = 0
        for upload in files:
            try:
                services.upload_image(upload, caption, current_resident(request))
            except ValidationError as exc:
                messages.error(request, f"{upload.name}: {_upload_errors(exc)}")
            else:
                uploaded += 1
        if uploaded:
            messages.success(
                request, f"{uploaded} billede{'r' if uploaded != 1 else ''} er lagt i biblioteket."
            )
        return redirect("cms:image_list")
    query = request.GET.get("q", "").strip()
    images = CmsImage.objects.select_related("uploaded_by")
    if query:
        images = images.filter(Q(caption__icontains=query) | Q(file__icontains=query))
    page_obj = Paginator(images, 48).get_page(request.GET.get("side"))
    usage = services.usage_map(page_obj.object_list)
    return render(
        request,
        "cms/image_list.html",
        {
            "page_obj": page_obj,
            "tiles": [(image, usage[image.pk]) for image in page_obj.object_list],
            "query": query,
            "pager_query": urlencode({"q": query}) if query else "",
            "max_mb": settings.CMS_IMAGE_MAX_MB,
        },
    )


@editor_view
def image_detail(request: HttpRequest, pk: int) -> HttpResponse:
    image = get_object_or_404(CmsImage.objects.select_related("uploaded_by"), pk=pk)
    if request.method == "POST":
        form = CaptionForm(request.POST)
        if form.is_valid():
            try:
                services.update_image(image, form.cleaned_data["caption"], current_resident(request))
            except ValidationError as exc:
                apply_service_errors(form, exc, {})
            else:
                messages.success(request, "Beskrivelsen er gemt.")
                return redirect("cms:image_detail", image.pk)
    else:
        form = CaptionForm(initial={"caption": image.caption})
    return render(
        request,
        "cms/image_detail.html",
        {"image": image, "form": form, "usage": services.image_usage(image)},
    )


@editor_view
@require_POST
def image_delete(request: HttpRequest, pk: int) -> HttpResponse:
    image = get_object_or_404(CmsImage, pk=pk)
    if usage := services.image_usage(image):
        messages.error(request, f"Billedet bruges stadig ({', '.join(usage)}), så det kan ikke slettes.")
        return redirect("cms:image_detail", image.pk)
    services.delete_image(image, current_resident(request))
    messages.success(request, "Billedet er slettet.")
    return redirect("cms:image_list")


@editor_view
def image_library_json(request: HttpRequest) -> JsonResponse:
    """The body editor's image picker: every library image, newest first."""
    return JsonResponse({"images": [image_json(image) for image in _library()]})


@editor_view
@require_POST
def image_upload_json(request: HttpRequest) -> JsonResponse:
    """The body editor's upload: one file into the library, answered with the new image."""
    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse({"error": "Vælg et billede."}, status=400)
    try:
        image = services.upload_image(upload, request.POST.get("caption", ""), current_resident(request))
    except ValidationError as exc:
        return JsonResponse({"error": _upload_errors(exc)}, status=400)
    return JsonResponse({"image": image_json(image)}, status=201)


# ----------------------------------------------------------------------------------------- menu
def _posted_menu(request: HttpRequest) -> list[dict[str, str]]:
    """The menu rows as posted (label_<n>/url_<n>), in order; a row left blank is dropped."""
    raw_count = request.POST.get("rows", "")
    count = min(int(raw_count), services.MAX_MENU_ITEMS + 1) if raw_count.isdigit() else 0
    rows = []
    for index in range(count):
        label = request.POST.get(f"label_{index}", "").strip()
        url = request.POST.get(f"url_{index}", "").strip()
        rows.append({"label": label, "url": url})
    return rows


def apply_menu_action(rows: list[dict[str, str]], action: str) -> list[dict[str, str]]:
    """`rows` after one of the menu screen's buttons: up-<n>, down-<n>, remove-<n> or save."""
    verb, _, raw_index = action.partition("-")
    index = int(raw_index) if raw_index.isdigit() else -1
    rows = list(rows)
    if verb == "up" and 0 < index < len(rows):
        rows[index - 1], rows[index] = rows[index], rows[index - 1]
    elif verb == "down" and 0 <= index < len(rows) - 1:
        rows[index + 1], rows[index] = rows[index], rows[index + 1]
    elif verb == "remove" and 0 <= index < len(rows):
        del rows[index]
    return [row for row in rows if row["label"] or row["url"]]


def _menu_targets() -> list[str]:
    """Addresses to suggest in the menu's link field: every page, and gahk.dk's own pages."""
    paths = ["/"] + [
        f"/{slug}"
        for slug in Page.objects.exclude(slug__isnull=True)
        .exclude(pk=FRONT_PAGE_ID)
        .values_list("slug", flat=True)
        .order_by("slug")
    ]
    return [*paths, "/begivenheder/", "/optagelse/"]


@editor_view
def menu_edit(request: HttpRequest) -> HttpResponse:
    errors: list[str] = []
    if request.method == "POST":
        rows = apply_menu_action(_posted_menu(request), request.POST.get("action", "save"))
        try:
            services.save_menu(rows, current_resident(request))
        except ValidationError as exc:
            errors = (
                exc.message_dict.get("items", exc.messages) if hasattr(exc, "error_dict") else exc.messages
            )
        else:
            messages.success(request, "Menuen er gemt.")
            return redirect("cms:menu")
    else:
        rows = [{"label": item.label, "url": item.url} for item in MenuItem.objects.all()]
    site = SiteSettings.load()
    return render(
        request,
        "cms/menu.html",
        {
            "rows": rows,
            "new_index": len(rows),
            "row_count": len(rows) + 1,
            "errors": errors,
            "targets": _menu_targets(),
            "unreachable": services.unreachable_pages(),
            "settings_form": SiteSettingsForm(
                initial={"default_background": site.default_background}, current=site.default_background
            ),
            "default_background": site.default_background,
            "images": _library(),
        },
    )


@editor_view
@require_POST
def site_settings(request: HttpRequest) -> HttpResponse:
    site = SiteSettings.load()
    form = SiteSettingsForm(request.POST, current=site.default_background)
    if form.is_valid():
        try:
            services.save_site_settings(
                {"default_background": form.cleaned_data["default_background"]}, current_resident(request)
            )
        except ValidationError as exc:
            messages.error(request, _upload_errors(exc))
        else:
            messages.success(request, "Standardbaggrunden er gemt.")
    else:
        messages.error(request, "Vælg et billede fra biblioteket.")
    return redirect("cms:menu")


# ------------------------------------------------------------------------------------ redirects
@editor_view
def redirect_list(request: HttpRequest) -> HttpResponse:
    form = RedirectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.save_redirect(
                None,
                {"old_path": form.cleaned_data["old_path"], "page": form.cleaned_data["page"]},
                current_resident(request),
            )
        except ValidationError as exc:
            apply_service_errors(form, exc, {})
        else:
            messages.success(request, "Omdirigeringen er gemt.")
            return redirect("cms:redirect_list")
    redirects = PageRedirect.objects.select_related("page", "created_by")
    return render(request, "cms/redirect_list.html", {"redirects": redirects, "form": form})


@editor_view
@require_POST
def redirect_delete(request: HttpRequest, pk: int) -> HttpResponse:
    item = get_object_or_404(PageRedirect, pk=pk)
    services.delete_redirect(item, current_resident(request))
    messages.success(request, f"/{item.old_path} sender ikke længere videre.")
    return redirect("cms:redirect_list")


# -------------------------------------------------------------------------------------- the log
# Fields holding HTML (or long text), shown as a diff rather than before → after.
DIFF_FIELDS = {"body", "description"}
FIELD_LABELS = {
    "slug": "Adresse",
    "header": "Titel",
    "title": "Titel",
    "body": "Tekst",
    "description": "Beskrivelse",
    "background_image": "Baggrundsbillede",
    "published_at": "Udgivet",
    "starts_on": "Dato",
    "caption": "Beskrivelse",
    "file": "Fil",
    "menu": "Menu",
    "old_path": "Gammel adresse",
    "page": "Side",
    "default_background": "Standardbaggrund",
    "version": "Version",
}


@dataclass
class ChangeField:
    label: str
    before: str = ""
    after: str = ""
    diff: list[DiffRow] | None = None
    menu: tuple[list[Any], list[Any]] | None = None


def _text(value: object) -> str:
    if value is None or value == "":
        return "–"
    return str(value)


def change_fields(change: ContentChange) -> list[ChangeField]:
    fields = []
    for name, pair in (change.changes or {}).items():
        before, after = ([*pair, None, None])[:2] if isinstance(pair, list) else (None, pair)
        label = FIELD_LABELS.get(name, name)
        if name == "menu":
            fields.append(ChangeField(label, menu=(before or [], after or [])))
        elif name in DIFF_FIELDS and (before or after):
            fields.append(ChangeField(label, diff=html_diff(str(before or ""), str(after or ""))))
        else:
            fields.append(ChangeField(label, _text(before), _text(after)))
    return fields


LINKED_KINDS: dict[str, tuple[type[Page | NewsItem | Event | CmsImage], str]] = {
    ContentChange.Kind.PAGE: (Page, "cms:page_edit"),
    ContentChange.Kind.NEWS: (NewsItem, "cms:news_edit"),
    ContentChange.Kind.EVENT: (Event, "cms:event_edit"),
    ContentChange.Kind.IMAGE: (CmsImage, "cms:image_detail"),
}


@editor_view
def change_log(request: HttpRequest) -> HttpResponse:
    changes = ContentChange.objects.select_related("by")
    kind = request.GET.get("type", "")
    if kind in ContentChange.Kind.values:
        changes = changes.filter(kind=kind)
    person = request.GET.get("person", "")
    if person == "system":
        # Every automatic change: "System", and the variants that say which job it was (the
        # legacy image import's "System (gamle billeder …)"). A resident deleted since keeps their
        # by_name, so by__isnull alone would count their edits as the system's.
        changes = changes.filter(by__isnull=True, by_name__startswith=services.SYSTEM_NAME)
    elif person.isdigit():
        changes = changes.filter(by_id=int(person))
    page_obj = Paginator(changes, 50).get_page(request.GET.get("side"))
    existing: dict[str, set[int]] = {}
    for kind_value, (model, _url) in LINKED_KINDS.items():
        ids = {c.object_id for c in page_obj.object_list if c.kind == kind_value and c.object_id}
        existing[kind_value] = (
            set(model.objects.filter(pk__in=ids).values_list("pk", flat=True)) if ids else set()
        )
    rows = []
    for change in page_obj.object_list:
        link = ""
        if change.kind in LINKED_KINDS and change.object_id in existing.get(change.kind, set()):
            link = LINKED_KINDS[change.kind][1]
        rows.append((change, link, change_fields(change)))
    people = (
        Resident.objects.filter(cms_content_changes__isnull=False)
        .distinct()
        .order_by("first_name", "last_name")
    )
    query = request.GET.copy()
    query.pop("side", None)
    return render(
        request,
        "cms/change_log.html",
        {
            "page_obj": page_obj,
            "rows": rows,
            "kinds": ContentChange.Kind.choices,
            "people": people,
            "kind": kind,
            "person": person,
            "pager_query": query.urlencode(),
        },
    )
