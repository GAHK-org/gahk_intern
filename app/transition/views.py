"""The old public site, as gahk.dk showed it before gahk_web, and gahk.dk's other answers. TEMPORARY.

Served only for TRANSITION_PUBLIC_HOSTS (transition.middleware.PublicSiteHost routes them to
transition.urls_public). Restored from the views this app had until "Remove the old public site from
the intranet" (ee69bd4), adapted to today's content:

  * titles are plain text (escaped), bodies HTML that cms.sanitize cleaned;
  * pictures are library images at /media/cms/… (the legacy import rewrote the old /public/image/…
    references, and the Hjemmeside editor inserts them so), served here to anyone (library_image);
    an old address of a picture still in a body goes to its library copy (legacy_image);
  * no visit counter (gahk.dk counts its own visits now), and the old hard-coded menu.

The admission forms store applications straight into intern's database, as the old site did, and
mail the applicant the same auto-reply as one pulled from gahk.dk's outbox.

The rest answers as gahk.dk will after the DNS switch (gahk_web's routing.py and .htaccess), so
nobody notices the switch: the intranet's paths and the wiki go to their own hosts (308, method
kept), /intern… shows the old installed app how to move.
"""

import json
import logging
import re
import urllib.parse
import urllib.request
from urllib.parse import quote

from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.core.files.storage import storages
from django.db import transaction
from django.db.models import Q
from django.forms import CharField, HiddenInput, ModelForm, ValidationError
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponsePermanentRedirect,
    HttpResponseRedirect,
)
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import Resolver404, get_urlconf, resolve, set_urlconf
from django.utils import timezone
from django.utils.safestring import SafeString, mark_safe
from django.views.decorators.csrf import csrf_exempt

from admissions.forms import FremlejeForm, RundvisningForm
from admissions.models import Application
from admissions.tasks import send_application_auto_reply
from cms.models import FRONT_PAGE_ID, CmsImage, Event, NewsItem, Page, PageRedirect, SiteSettings
from core import media
from events import views as events_views

from .middleware import PATH_SAFE, PUBLIC_URLCONF, intern_url, permanent_redirect

logger = logging.getLogger(__name__)

# The old site's menu, labels and order as on the legacy gahk.dk.
MENU = [
    ("/", "Hjem"),
    ("/kollegielivet/historie", "Historie"),
    ("/vision", "Vision"),
    ("/kollegielivet", "Kollegielivet"),
    ("/faciliteter", "Faciliteter"),
    ("/begivenheder/", "Begivenheder"),
    ("/optagelse/", "Optagelse"),
    ("/legater", "Legater"),
    ("/kontakt", "Kontakt"),
]

# The old site's pictures (below its static/legacy/image/): the front page's hero, and the
# background of a page without one of its own when the site has no default.
HOME_HERO = "upload/images/72352712_3043170802378120_6023122459278966784_n.jpg"
DEFAULT_BACKGROUND = "upload/images/gang.jpg"
LEGACY_IMAGE_URL = "/static/legacy/image/"

# An old picture address in a body, in any of the forms cms.legacy_images knows. Relative ones
# ("public/image/…") broke on any page below the root, so all of them become the absolute
# /static/legacy/image/…, which legacy_image answers.
_LEGACY_REF_RE = re.compile(
    r"(?<![\w./:-])(?:https?://(?:www\.)?gahk\.dk)?(?:/public/image/|public/image/|/static/legacy/image/)",
    re.IGNORECASE,
)

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def body_html(html: str) -> SafeString:
    """A stored body (sanitized on save) for the page, with old picture addresses made absolute."""
    return mark_safe(_LEGACY_REF_RE.sub(LEGACY_IMAGE_URL, html or ""))  # noqa: S308  # nosec — sanitized on save


def _legacy_image_url(legacy_path: str) -> str:
    """The library copy of an old picture, or its old address (which legacy_image answers)."""
    image = CmsImage.objects.filter(legacy_path=legacy_path).exclude(file="").first()
    return _background(image.url if image is not None else LEGACY_IMAGE_URL + legacy_path)


# What could end a CSS url('…') early, percent-encoded instead (a URL means the same either way).
_CSS_URL_ESCAPES = str.maketrans({char: f"%{ord(char):02X}" for char in "'\"()\\ \n\r\t"})


def _background(value: str) -> str:
    """A stored background (a library URL, or an old address not converted yet) for a CSS url()."""
    return _LEGACY_REF_RE.sub(LEGACY_IMAGE_URL, value.strip()).translate(_CSS_URL_ESCAPES) if value else ""


def _default_background() -> str:
    return _background(SiteSettings.load().default_background) or _legacy_image_url(DEFAULT_BACKGROUND)


def _context(**extra: object) -> dict[str, object]:
    return {"menu": MENU, "intern_url": settings.SITE_URL.rstrip("/") + "/", **extra}


# ------------------------------------------------------------------------------------ pages
def home(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()
    page = Page.objects.filter(pk=FRONT_PAGE_ID).first()
    return render(
        request,
        "transition/home.html",
        _context(
            page=page,
            body=body_html(page.body if page else ""),
            hero=_legacy_image_url(HOME_HERO),
            upcoming_events=Event.objects.filter(starts_on__gte=today).order_by("starts_on")[:3],
            latest_news=NewsItem.objects.all()[:4],
        ),
    )


def events_news(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()
    upcoming = Event.objects.filter(starts_on__gte=today).order_by("starts_on")
    return render(
        request,
        "transition/events_news.html",
        _context(
            hero=_default_background(),
            upcoming_events=[(event, body_html(event.description)) for event in upcoming],
            past_events=Event.objects.filter(starts_on__lt=today).order_by("-starts_on")[:20],
            news=[(item, body_html(item.body)) for item in NewsItem.objects.all()[:40]],
        ),
    )


def _section_nav(current: Page) -> tuple[str, list[dict[str, object]]]:
    """The pages of `current`'s top-level section (by address prefix), when there are two or more."""
    if not current.slug:
        return "", []
    section = current.slug.split("/", 1)[0]
    pages = list(Page.objects.filter(Q(slug=section) | Q(slug__startswith=section + "/")).order_by("slug"))
    if len(pages) < 2:
        return "", []
    title = next((p.header for p in pages if p.slug == section), current.header)
    links = [{"url": f"/{p.slug}", "header": p.header, "current": p.pk == current.pk} for p in pages]
    return title, links


def _redirect_keeping_query(request: HttpRequest, target: str) -> HttpResponsePermanentRedirect:
    query = request.META.get("QUERY_STRING", "")
    return HttpResponsePermanentRedirect(f"{target}?{query}" if query else target)


def page(request: HttpRequest, url_path: str) -> HttpResponse:
    """A page by its address; then a former address (301); then, as APPEND_SLASH would, the same
    path with a slash when that is another view (/optagelse, /begivenheder)."""
    slug = url_path.strip("/")
    found = Page.objects.filter(slug=slug).first()
    if found is not None:
        section_title, section_links = _section_nav(found)
        return render(
            request,
            "transition/page.html",
            _context(
                page=found,
                body=body_html(found.body),
                background=_background(found.background_image) or _default_background(),
                section_title=section_title,
                section_links=section_links,
            ),
        )
    moved = PageRedirect.objects.filter(old_path=slug).select_related("page").first()
    if moved is not None and moved.page.slug and moved.page.slug != slug:
        target = "/" if moved.page.pk == FRONT_PAGE_ID else f"/{moved.page.slug}"
        return _redirect_keeping_query(request, target)
    candidate = f"/{slug}/"
    try:
        match = resolve(candidate, urlconf=PUBLIC_URLCONF)
    except Resolver404:
        raise Http404("no such page") from None
    if match.func is page:
        raise Http404("no such page")
    return _redirect_keeping_query(request, candidate)


# ------------------------------------------------------------------------------------ images
def library_image(request: HttpRequest, path: str) -> HttpResponseBase:
    """A website image (/media/cms/…), for anyone, as /media/cms/ was before gahk_web.

    core.media.serve_media wants a login for these now and sends an anonymous request to gahk.dk's
    own copy — which, while gahk.dk is this app, is this very address. Anything else under /media/
    goes to intern.gahk.dk, which checks the login.
    """
    name = media._clean(path)
    if name is None or not name.startswith("cms/"):
        return permanent_redirect(intern_url(request.path, request.META.get("QUERY_STRING", "")))
    storage = storages["default"]
    signer = getattr(storage, "signed_url", None)
    try:
        if signer is not None:
            moved = HttpResponseRedirect(signer(name, expire=media.PRESIGN_TTL))
            moved.headers["Cache-Control"] = f"public, max-age={media.REDIRECT_MAX_AGE}"
            return moved
        return FileResponse(storage.open(name))
    except SuspiciousOperation as exc:
        raise Http404("not a media path") from exc
    except (FileNotFoundError, IsADirectoryError, PermissionError, ValueError) as exc:
        raise Http404("no such image") from exc


def legacy_image(request: HttpRequest, rest: str) -> HttpResponse:
    """An old picture address (/static/legacy/image/…, /public/image/…): to the picture's library
    copy, found by CmsImage.legacy_path (the files are no longer served from here). 302, because the
    copy's address changes once gahk.dk is the webhost."""
    parts = [part for part in rest.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise Http404("not a picture")
    image = CmsImage.objects.filter(legacy_path="/".join(parts)).exclude(file="").first()
    if image is None:
        raise Http404("no such picture")
    return HttpResponseRedirect(image.url)


# ------------------------------------------------------------------------------- admissions
class _SpamCheck(ModelForm):
    """The honeypot the old forms had: real people leave the hidden field empty, bots fill it."""

    website = CharField(required=False, widget=HiddenInput)

    def clean_website(self) -> str:
        if self.cleaned_data.get("website"):
            raise ValidationError("Spam.")
        return ""


class TourForm(_SpamCheck, RundvisningForm):
    pass


class SubletForm(_SpamCheck, FremlejeForm):
    pass


def _turnstile_ok(request: HttpRequest) -> bool:
    """Cloudflare Turnstile, as the old forms checked it. Skipped without TURNSTILE_SECRET_KEY."""
    secret = settings.TURNSTILE_SECRET_KEY
    if not secret:
        return True
    token = request.POST.get("cf-turnstile-response", "")
    if not token:
        return False
    data = urllib.parse.urlencode(
        {"secret": secret, "response": token, "remoteip": request.META.get("REMOTE_ADDR", "")}
    ).encode()
    try:
        # A fixed https URL, not user-controlled.
        with urllib.request.urlopen(TURNSTILE_VERIFY_URL, data=data, timeout=5) as answer:  # nosec B310
            return bool(json.loads(answer.read()).get("success"))
    except Exception:
        logger.warning("Turnstile verification request failed", exc_info=True)
        return False


def _apply(
    request: HttpRequest,
    form_class: type[TourForm] | type[SubletForm],
    application_type: str,
    post_url: str,
    title: str,
    intro: str,
    show_criteria: bool = False,
) -> HttpResponse:
    form = form_class()
    captcha_failed = False
    if request.method == "POST":
        form = form_class(request.POST)
        turnstile_ok = _turnstile_ok(request)
        captcha_failed = not turnstile_ok
        if form.is_valid() and turnstile_ok:
            application = form.save(commit=False)
            application.type = application_type
            application.submitted_at = timezone.now()
            with transaction.atomic():
                application.save()
                transaction.on_commit(lambda: send_application_auto_reply.delay(application.pk))
            return redirect("transition_application_sent")
    return render(
        request,
        "transition/apply_form.html",
        _context(
            hero=_default_background(),
            form=form,
            post_url=post_url,
            title=title,
            intro=intro,
            show_criteria=show_criteria,
            captcha_failed=captcha_failed,
            turnstile_site_key=settings.TURNSTILE_SITE_KEY,
        ),
    )


def admissions(request: HttpRequest) -> HttpResponse:
    return render(request, "transition/admissions.html", _context(hero=_default_background()))


def apply_for_tour(request: HttpRequest) -> HttpResponse:
    return _apply(
        request,
        TourForm,
        Application.Type.TOUR,
        "transition_send_tour",
        "Anmod om rundvisning",
        "Udfyld formularen for at anmode om en rundvisning. Indstillingen kontakter dig, "
        "hvis der er udsigt til ledige værelser, og din profil passer.",
        show_criteria=True,
    )


def apply_for_sublet(request: HttpRequest) -> HttpResponse:
    return _apply(
        request,
        SubletForm,
        Application.Type.SUBLET,
        "transition_send_sublet",
        "Ansøg om fremleje",
        "Skal du fremleje et værelse midlertidigt? Udfyld formularen, så vender vi tilbage til dig.",
    )


def application_sent(request: HttpRequest) -> HttpResponse:
    return render(request, "transition/application_sent.html", _context(hero=_default_background()))


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """settings.CSRF_FAILURE_VIEW while the transition lasts: the public site's own page for a public
    host (the intranet's page links to intranet URLs, which the public URLconf does not have), the
    intranet's (residents.auth.csrf_failure) for any other."""
    if getattr(request, "urlconf", None) != PUBLIC_URLCONF:
        from residents.auth import csrf_failure as intranet_csrf_failure

        return intranet_csrf_failure(request, reason)
    logger.warning("CSRF refused %s %s: %s", request.method, request.path, reason)
    return render(request, "transition/csrf_failure.html", _context(hero=_default_background()), status=403)


# -------------------------------------------------------------------- gahk.dk's other answers
_READ_METHODS = ("GET", "HEAD")


@csrf_exempt
def intern_moved(request: HttpRequest, rest: str = "") -> HttpResponse:
    """/intern, /intern/…, /nyintern, /nyintern/…: the intranet is at the root of intern.gahk.dk.

    GET and HEAD get gahk_web's static/intern-moved.html (as a template): a browser goes on to the
    same address there at once, the prefix dropped; inside the old installed app, which cannot follow
    a redirect to another origin, it explains how to install the new one. Any other method gets a
    308 there, so a POST stays a POST. csrf_exempt: a redirect changes nothing, and the view it
    lands on checks CSRF itself.
    """
    if request.method not in _READ_METHODS:
        return permanent_redirect(intern_url(rest, request.META.get("QUERY_STRING", "")))
    origin = settings.SITE_URL.rstrip("/")
    return render(request, "transition/intern_moved.html", {"intern_origin": origin})


@csrf_exempt
def to_intern(request: HttpRequest) -> HttpResponse:
    """The intranet's tools and files, which gahk.dk sends to intern.gahk.dk at the same path (308)."""
    return permanent_redirect(intern_url(request.path, request.META.get("QUERY_STRING", "")))


@csrf_exempt
def to_intern_path(request: HttpRequest, target: str) -> HttpResponse:
    """As to_intern, to `target` (the path without a trailing slash, as gahk.dk's rule writes it)."""
    return permanent_redirect(intern_url(target, request.META.get("QUERY_STRING", "")))


def calendar_feed(request: HttpRequest, token: str) -> HttpResponse:
    """A calendar feed handed out with a gahk.dk address: the intranet's own view, as before. It
    names events by their intranet URL, so it is run against the intranet's URLconf."""
    previous = get_urlconf()
    set_urlconf(None)
    try:
        return events_views.calendar_feed(request, token)
    finally:
        set_urlconf(previous)


# The wiki at wiki.gahk.dk's root (gahk_web's routing._WIKI_RULES): /wiki → its front page, the
# MediaWiki entry points and file folders at the root, every other /wiki/<title> at /wiki/<title>.
_WIKI_ROOT_ENTRIES = re.compile(
    r"^(?:(?:index|load|api|rest|img_auth|thumb|thumb_handler|opensearch_desc)\.php"
    r"|images|skins|resources|extensions)(?:/.*)?$"
)


@csrf_exempt
def to_wiki(request: HttpRequest, rest: str = "") -> HttpResponse:
    """/wiki and /wiki/…: to the same page on wiki.gahk.dk (308; its login form POSTs)."""
    if not rest:
        path = "/"
    elif _WIKI_ROOT_ENTRIES.match(rest):
        path = "/" + rest
    else:
        path = "/wiki/" + rest
    url = settings.WIKI_URL.rstrip("/") + quote(path, safe=PATH_SAFE)
    query = request.META.get("QUERY_STRING", "")
    return permanent_redirect(f"{url}?{query}" if query else url)
