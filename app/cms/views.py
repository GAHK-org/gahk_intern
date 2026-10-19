"""Public CMS pages (F-006) — read-only rendering of Page content.

Bodies are editor-authored through the role-gated admin, sanitized with nh3 on every save (and on
import), and rendered with `|safe` in the template. URLs match the legacy slugs (incl. multi-segment
ones like `faciliteter/kokken`) for SEO, which is why the lookup key is the whole path.
"""

from django.http import Http404, HttpRequest, HttpResponse, HttpResponsePermanentRedirect
from django.shortcuts import render
from django.urls import Resolver404, resolve
from django.utils import timezone

from .models import Event, NewsItem, Page, PageRedirect
from .nav import section_pages


def _section_nav(current: Page) -> tuple[str, list]:
    """Sibling pages in the same top-level section (derived from the slug prefix) for the sidebar.

    Returns (section_title, links). Empty when the page stands alone (no siblings to list) — and
    that emptiness is the whole reason a renamed page can disappear, so cms.nav owns the prefix
    query and reports on it for the admin too.
    """
    if not current.slug:
        return "", []
    section = current.slug.split("/")[0]
    pages = section_pages(current.slug)
    if len(pages) < 2:
        return "", []
    section_title = next((p.header for p in pages if p.slug == section), current.header)
    links = [{"url": "/" + (p.slug or ""), "header": p.header, "current": p.id == current.id} for p in pages]
    return section_title, links


def _render(request: HttpRequest, page: Page) -> HttpResponse:
    section_title, section_links = _section_nav(page)
    return render(
        request,
        "cms/page.html",
        {
            "page": page,
            "bg_image": page.background_image,
            "section_title": section_title,
            "section_links": section_links,
        },
    )


HOME_HERO = "/public/image/upload/images/72352712_3043170802378120_6023122459278966784_n.jpg"


def home(request: HttpRequest) -> HttpResponse:
    # `/` is the canonical front page (legacy default_controller was page/show/1, "velkommen").
    page = Page.objects.filter(id=1).first()  # reuse its body as the intro text
    today = timezone.localdate()
    return render(
        request,
        "cms/home.html",
        {
            "page": page,
            "bg_image": HOME_HERO,
            "upcoming_events": Event.objects.filter(starts_on__gte=today).order_by("starts_on")[:3],
            "latest_news": NewsItem.objects.all()[:4],
        },
    )


def events_news(request: HttpRequest) -> HttpResponse:
    """Public "Nyheder & Begivenheder" page (F-007/F-008): upcoming + past events and the news archive."""
    today = timezone.localdate()
    return render(
        request,
        "cms/events_news.html",
        {
            "upcoming_events": Event.objects.filter(starts_on__gte=today).order_by("starts_on"),
            "past_events": Event.objects.filter(starts_on__lt=today).order_by("-starts_on")[:20],
            "news": NewsItem.objects.all()[:40],
            "bg_image": "",
        },
    )


def _redirect_preserving_query(request: HttpRequest, target: str) -> HttpResponsePermanentRedirect:
    """301 to `target`, carrying the query string over. Shared so the two redirect paths below
    cannot drift apart in whether they keep it."""
    query = request.META.get("QUERY_STRING", "")
    return HttpResponsePermanentRedirect(f"{target}?{query}" if query else target)


def page(request: HttpRequest, url_path: str) -> HttpResponse:
    """Catch-all CMS lookup by address, then former addresses, then a slash-appending fallback.

    This pattern is last in the URLconf but still matches *slashless* paths owned by a real app -
    `/optagelse`, `/nyintern`, `/intern`, `/begivenheder`. Because a pattern matched, Django's
    APPEND_SLASH never fires, so those legacy URLs 404ed instead of redirecting to their real view
    (the live PHP site served `/optagelse` with a 200, and `/intern` is how residents reach the
    portal; `/nyintern` redirects to it). Restore
    APPEND_SLASH's behaviour for exactly the paths this view swallows: if no CMS page owns the slug
    but `<path>/` resolves to some *other* view, 301 there.

    Order matters and is load-bearing:

      1. a live page always wins, so a newly created page silently reclaims an address that used to
         redirect elsewhere — no cleanup needed, and no way for a redirect to shadow real content;
      2. then a former address (cms.services.record_slug_change), so renaming a page never breaks a
         link. This sits *above* the slash fallback but can never steal from it: PageRedirect.old_path
         is validated by cms.paths, whose reserved-segment rule rejects `optagelse`, `nyintern` and
         `begivenheder` outright, and cms.checks fails the build if urls.py grows a prefix the list
         has not been told about;
      3. then the APPEND_SLASH restoration described above.
    """
    slug = url_path.strip("/")
    if found := Page.objects.filter(slug=slug).first():
        return _render(request, found)

    moved = PageRedirect.objects.filter(old_path=slug).select_related("page").first()
    if moved and moved.page.slug and moved.page.slug != slug:
        return _redirect_preserving_query(request, f"/{moved.page.slug}")

    candidate = f"/{slug}/"
    try:
        match = resolve(candidate)
    except Resolver404:
        raise Http404(f"No CMS page with slug {slug!r}") from None
    if match.func is page:  # the catch-all again - genuinely nothing here
        raise Http404(f"No CMS page with slug {slug!r}")
    return _redirect_preserving_query(request, candidate)
