"""Writes that must happen together whenever a CMS page changes: keep the old address alive, and
keep a restorable copy of what the page used to say.

Both are called **explicitly** from cms.admin rather than hung off a `post_save` signal, and that is
a decision worth not undoing. A signal would also fire for `etl_cms` (fifteen `update_or_create`s
per run), `seed_demo`, `sync_cms_media` and every `save()` typed into a shell — producing a pile of
author-less snapshots and doubling ETL write volume, with no way to tell a real edit from an import.

The cost of choosing explicit calls is that an ETL or shell overwrite leaves no version of its own.
That is acceptable, because the admin always snapshots the *pre-edit database state* before saving:
the content immediately before any human edit is therefore always captured, including content an
ETL run had already clobbered. ETL and seeding are deliberate developer operations against a legacy
database; a resident clicking "gem" is not, and that is the one this protects.
"""

from residents.models import Resident

from .models import Page, PageRedirect, PageVersion

# Keeps history bounded by construction, so there is no pruning command and no cron to forget. At a
# handful of edits a year across ~20 pages this will never be reached; it exists so that a script
# looping over `save()` cannot quietly grow the table without limit.
VERSIONS_PER_PAGE_CAP = 100


def record_slug_change(page: Page, old_slug: str | None, author: Resident | None) -> None:
    """Point `old_slug` at `page` so its former address keeps working. Idempotent.

    Safe to call when nothing changed, when the page never had an address, or when it has just lost
    one — each of those simply does nothing.
    """
    if not page.slug or not old_slug or old_slug == page.slug:
        return

    # The page has been renamed *back* onto a path that previously redirected away. Left alone, that
    # row would now send the new address to itself.
    PageRedirect.objects.filter(old_path=page.slug).delete()

    # update_or_create, not create: an address may have been abandoned by another page earlier, and
    # the most recent claimant is the one that should own the redirect.
    PageRedirect.objects.update_or_create(old_path=old_slug, defaults={"page": page, "created_by": author})


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
        created_by=author,
        note=note,
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
