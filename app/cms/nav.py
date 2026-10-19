"""Whether a CMS page is actually reachable from anywhere on the site.

This is the question nobody could answer when an editor renamed /faciliteter/kokken to
/faciliteter-kokken. The page still resolved; it had simply stopped being *linked*, because there is
no navigation table anywhere in this project:

  * the public menus are hard-coded tuple lists (core.context_processors.NAV_PUBLIC / NAV_LEGACY),
    and they only ever list top-level entries;
  * a sub-page appears solely in the section sidebar that cms.views._section_nav builds from the
    slug prefix, and that sidebar is suppressed unless the section holds at least two pages;
  * `/` is served by cms.views.home from `Page.objects.filter(id=1)`, whose slug (`velkommen`)
    appears in no menu at all — so the front page looks orphaned to any naive check. It is not.

`statuses()` is bulk on purpose. The obvious implementation — a property on the model, or a per-row
admin column — costs a query per row on the changelist, exactly the trap the CmsImage.usage
docstring documents. One pass over every page answers it for all of them at once.
"""

from collections.abc import Sequence
from typing import Literal

from django.db.models import Q

from core.context_processors import NAV_LEGACY, NAV_PUBLIC

from .models import Page

# The page id cms.views.home renders at `/`. Hard-coded there, so it is hard-coded here too rather
# than guessed at; if that view ever stops pinning id=1, this constant is the other half to change.
FRONT_PAGE_ID = 1

Status = Literal["nav", "section", "linked", "orphan", "unrouted"]

STATUS_LABELS: dict[Status, str] = {
    "nav": "I menuen",
    "section": "I sektionsmenu",
    "linked": "Linket fra en side",
    "orphan": "Ikke i nogen menu",
    "unrouted": "Ingen adresse",
}

# Only "orphan" is a problem an editor should act on; "unrouted" is a deliberate state (the
# optagelse bodies), and the middle two are ordinary ways to be reachable.
PROBLEM_STATUSES: frozenset[str] = frozenset({"orphan"})


def nav_paths() -> set[str]:
    """The slugs the hard-coded public menus link to, normalised to bare paths ("" for `/`)."""
    return {url.strip("/") for url, _label in (*NAV_PUBLIC, *NAV_LEGACY)}


def section_pages(slug: str) -> list[Page]:
    """Pages in the same top-level section as `slug`, ordered by address.

    The hierarchy is pure string prefix — there is no parent foreign key — so this is the one place
    that knowledge lives; cms.views._section_nav consumes it rather than restating the query.
    """
    section = slug.split("/", 1)[0]
    return list(Page.objects.filter(Q(slug=section) | Q(slug__startswith=section + "/")).order_by("slug"))


def statuses(pages: Sequence[Page]) -> dict[int, Status]:
    """Map page pk -> how that page is reachable, in a single pass with no per-row queries.

    `pages` must be every page, not a filtered subset: "is anything linking to this?" cannot be
    answered from a page in isolation.
    """
    menu = nav_paths()
    slugs = [page.slug for page in pages if page.slug]
    bodies = [page.body for page in pages if page.body]

    # A section renders a sidebar only when it holds 2+ pages (_section_nav's own rule), so count
    # first rather than asking "does any sibling exist" per page.
    section_sizes: dict[str, int] = {}
    for slug in slugs:
        section_sizes[slug.split("/", 1)[0]] = section_sizes.get(slug.split("/", 1)[0], 0) + 1

    result: dict[int, Status] = {}
    for page in pages:
        if not page.slug:
            result[page.pk] = "unrouted"
            continue
        if page.slug in menu or page.pk == FRONT_PAGE_ID:
            result[page.pk] = "nav"
            continue
        section = page.slug.split("/", 1)[0]
        # In a sidebar only if that sidebar is rendered AND the section itself is in a menu —
        # `faciliteter-kokken` failed the second half, which is precisely how it disappeared.
        if section in menu and section_sizes.get(section, 0) >= 2 and "/" in page.slug:
            result[page.pk] = "section"
            continue
        url = f"/{page.slug}"
        if any(url in body for body in bodies):
            result[page.pk] = "linked"
            continue
        result[page.pk] = "orphan"
    return result


def is_reachable(page: Page) -> bool:
    """Whether any menu or page on the site leads to `page`. A page with no address is not "broken"."""
    every_page = list(Page.objects.only("id", "slug", "body"))
    # `page` may be unsaved-but-just-saved with stale siblings in the list; make sure its own row is
    # the current one rather than whatever the query returned.
    every_page = [p for p in every_page if p.pk != page.pk] + [page]
    return statuses(every_page).get(page.pk, "orphan") not in PROBLEM_STATUSES
