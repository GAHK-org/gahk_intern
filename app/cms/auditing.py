"""What the audit trail keeps of the website's content (the Hjemmeside editor).

Every write goes through cms.services, which names it ("cms.page.updated") and lets the trail capture
the fields. The Hjemmeside's own change log reads the same events back (cms.views.change_log).
"""

from audit import trail

from .models import (
    CmsImage,
    Event,
    GahkWebSync,
    MenuItem,
    NewsItem,
    Page,
    PageRedirect,
    PageVersion,
    Publication,
    PylonEvent,
    SiteSettings,
)

# The kinds whose changes alter what gahk.dk shows (an image only does so through content using it).
PUBLISHED_KINDS = (
    "cms.page",
    "cms.news",
    "cms.event",
    "cms.menu",
    "cms.menu_item",
    "cms.redirect",
    "cms.settings",
)

trail.register(
    Page,
    "cms.page",
    name=lambda page: page.header,
    label="Side",
    # Who saved it last and when: the event says so already. The menu category is the old site's,
    # kept for the record and never edited.
    ignore={"updated_at", "updated_by", "menu_category"},
    labels={
        "slug": "Adresse",
        "header": "Titel",
        "body": "Tekst",
        "background_image": "Baggrundsbillede",
        "description": "Beskrivelse",
    },
)
trail.register(
    NewsItem,
    "cms.news",
    name=lambda item: item.title,
    label="Nyhed",
    labels={"title": "Titel", "body": "Tekst", "published_at": "Udgivet"},
)
trail.register(
    Event,
    "cms.event",
    name=lambda item: item.title,
    label="Forsidebegivenhed",
    labels={"title": "Titel", "description": "Beskrivelse", "starts_on": "Dato"},
)
trail.register(
    CmsImage,
    "cms.image",
    name=str,
    label="Billede",
    # Facts about the file, worked out from it.
    ignore={"uploaded_at", "sha256", "content_type", "size", "legacy_path"},
    labels={"file": "Fil", "caption": "Beskrivelse", "uploaded_by": "Uploadet af"},
)
trail.register(
    PageRedirect,
    "cms.redirect",
    name=lambda redirect: f"/{redirect.old_path}",
    label="Omdirigering",
    ignore={"created_at", "created_by"},
    labels={"old_path": "Gammel adresse", "page": "Side"},
)
# The editor replaces the whole menu at once and records it as one change of "cms.menu" (the list
# before and after); a single item is recorded only when changed some other way (the Django admin).
trail.register(
    MenuItem,
    "cms.menu_item",
    name=lambda item: item.label,
    label="Menupunkt",
    labels={"label": "Tekst", "url": "Link", "order": "Rækkefølge"},
)
trail.register(
    SiteSettings,
    "cms.settings",
    name=lambda _settings: "Indstillinger",
    label="Indstillinger for hjemmesiden",
    labels={"default_background": "Standardbaggrund"},
)
trail.exempt(PageVersion, "a page's restorable copies, written by the same events that change the page")
trail.exempt(Publication, "the publish log of its own; asking to publish is an event")
trail.exempt(GahkWebSync, "what gahk.dk last reported, kept up to date by a worker")
trail.exempt(PylonEvent, "an archive of the old calendar, never written")

trail.kind_labels({"cms.menu": "Menu", "cms.site": "gahk.dk"})

trail.actions(
    {
        "cms.page.created": "Oprettede siden",
        "cms.page.updated": "Rettede siden",
        "cms.page.deleted": "Slettede siden",
        "cms.page.restored": "Gendannede siden",
        "cms.news.created": "Oprettede nyheden",
        "cms.news.updated": "Rettede nyheden",
        "cms.news.deleted": "Slettede nyheden",
        "cms.event.created": "Oprettede forsidebegivenheden",
        "cms.event.updated": "Rettede forsidebegivenheden",
        "cms.event.deleted": "Slettede forsidebegivenheden",
        "cms.image.uploaded": "Uploadede billedet",
        "cms.image.updated": "Rettede billedet",
        "cms.image.deleted": "Slettede billedet",
        "cms.menu.updated": "Ændrede menuen",
        "cms.redirect.created": "Oprettede omdirigeringen",
        "cms.redirect.updated": "Rettede omdirigeringen",
        "cms.redirect.deleted": "Slettede omdirigeringen",
        "cms.settings.updated": "Ændrede indstillingerne",
        "cms.site.published": "Bad om at udgive gahk.dk",
        "cms.site.update_requested": "Bad gahk.dk om at opdatere sin kode",
    }
)
