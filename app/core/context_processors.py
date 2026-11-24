"""Sidebar navigation, the template's `access` and the preview state, available to every template."""

from django.conf import settings
from django.http import HttpRequest
from django.urls import reverse

from access.api import (
    PREVIEW_SESSION_KEY,
    RESIDENT,
    ROLE_LABELS,
    Feature,
    can_preview,
    previewed_roles,
    request_roles,
)
from den_hurtige import channels
from den_hurtige.access import roles_allowed as den_hurtige_allowed
from events.access import roles_allowed as events_allowed
from gahkflix.access import roles_allowed as gahkflix_allowed
from opslagstavle.access import roles_allowed as opslagstavle_allowed
from photo_album.access import roles_allowed as photo_album_allowed

NavItem = tuple[str, str, str]  # (url, label, icon-key from the base.html sprite)
NavSection = tuple[str, list[NavItem]]


def _nav_intern(request: HttpRequest, roles: frozenset[str], user_pk: int) -> list[NavSection]:
    """Internal menu grouped into sidebar sections: what this request's permissions open, each feature
    also behind its rollout gate where it has one. Each item is (url, label, icon); empty sections
    are dropped. Honours the preview (request.user.has_perm, residents.Account.has_perm)."""

    def opens(feature: Feature, action: str = "view") -> bool:
        return request.user.has_perm(f"access.{feature}_{action}")

    oversigt: list[NavItem] = []
    if opens(Feature.DASHBOARD):
        oversigt.append((reverse("dashboard"), "Dashboard", "dashboard"))
    if opens(Feature.PROFILES):
        oversigt.append((reverse("resident_profile", args=[user_pk]), "Min profil", "users"))
    # No "Notifikationer" here: the bell in the topbar is the one place for them, and their settings
    # are in the avatar menu beside it (base.html). The sidebar is for features.
    #
    # Each feature with a rollout gate asks it as well (core.rollout): re-gating one is one edit that
    # narrows the views and this entry together.
    if opens(Feature.DEN_HURTIGE) and den_hurtige_allowed(roles):
        oversigt.append((channels.home(roles), "Den Hurtige", "flash"))
    if opens(Feature.OPSLAGSTAVLE) and opslagstavle_allowed(roles):
        oversigt.append((reverse("opslagstavle:board"), "Opslagstavle", "board"))
    if opens(Feature.EVENTS) and events_allowed(roles):
        oversigt.append((reverse("events:index"), "Begivenheder", "calendar"))
    # Alumneliste stays here; Stamtræ and Statistik moved to Ressourcer (see below).
    if opens(Feature.DIRECTORY):
        oversigt.append((reverse("directory"), "Alumneliste", "list"))
    if opens(Feature.PHOTO_ALBUM) and photo_album_allowed(roles):
        oversigt.append((reverse("photo_album:index"), "Fotoalbum", "photo"))
    if opens(Feature.GAHKFLIX) and gahkflix_allowed(roles):
        oversigt.append((reverse("gahkflix:home"), "GAHKFlix", "film"))
    vaerelser: list[NavItem] = []
    if opens(Feature.SOEGVAERELSE):
        vaerelser.append((reverse("soegvaerelse:soeg"), "Søg værelse", "house"))
    if opens(Feature.VAERELSESTJEK):
        vaerelser.append((reverse("vaerelsestjek:overview"), "Værelsestjek", "inspect"))
    if opens(Feature.SOEGVAERELSE, "manage"):
        vaerelser.append((reverse("soegvaerelse:admin"), "Værelsesudbud", "offer"))
    grupper: list[NavItem] = []
    if opens(Feature.AK):
        grupper.append((reverse("ak:index"), "AK-krydser", "check"))
    if opens(Feature.OELKAELDER):
        grupper.append((reverse("oelkaelder:my"), "Ølkælder", "beer"))
    if opens(Feature.AK, "manage"):
        grupper.append((reverse("ak:overview"), "AK-oversigt", "check"))
    if opens(Feature.OELKAELDER, "manage"):
        # Salgsoverblik / Personoversigt are sub-pages reached from Ølkælder-admin, not separate nav items.
        grupper.append((reverse("oelkaelder:admin"), "Ølkælder-admin", "beer"))
    if opens(Feature.ACCOUNTING):
        grupper.append((reverse("regnskab:overview"), "Regnskab", "receipt"))
    # Reporting a repair needs no group; Reppergruppen manages the board once inside.
    vaerktoejer: list[NavItem] = []
    if opens(Feature.REPARATIONER):
        vaerktoejer.append((reverse("reparationer:board"), "Reparationer", "wrench"))
    administration: list[NavItem] = []
    if opens(Feature.ADMISSIONS):
        administration.append((reverse("admissions:list"), "Ansøgninger", "inbox"))
    if opens(Feature.ACCESS):
        administration.append((reverse("access:index"), "Adgang", "users"))
    if opens(Feature.WEBSITE):
        administration.append((reverse("cms:overview"), "Hjemmeside", "globe"))
    if opens(Feature.SITE_ADMIN):
        administration.append((reverse("siteadmin:home"), "Site-admin", "gear"))
        administration.append((reverse("siteadmin:roles"), "Roller", "users"))
    # Stamtræ and Statistik sit here rather than under Oversigt, which they used to. Oversigt is
    # what is happening NOW — the dashboard, your own profile, the three feeds, who lives here — and
    # you open those to see whether anything has changed. These are reference: nothing in them
    # changes between visits, and you go to them to look something up. That is what Ressourcer is,
    # and it is why the wiki belongs in it too.
    #
    # Internal pages FIRST, the external links last. Keeping the outbound links together at the
    # bottom stops the sidebar mixing "leaves the site" with "does not" halfway down a list. Note
    # _active_nav_url skips anything with a scheme, so only the internal entries here can light up.
    #
    ressourcer: list[NavItem] = []
    if opens(Feature.STAMTREE):
        ressourcer.append((reverse("stamtree"), "Stamtræ", "tree"))
    if opens(Feature.STATS):
        ressourcer.append((reverse("stats:index"), "Statistik", "chart"))
    if opens(Feature.WIKI):
        ressourcer.append((settings.WIKI_URL, "Wiki", "book"))
    if opens(Feature.DASHBOARD):
        ressourcer.append((settings.FEEDBACK_URL, "Fejl & ønsker", "bug"))
    # kokkengruppe: no dedicated screen today (documented gap; no menu item).
    sections: list[NavSection] = [
        ("Oversigt", oversigt),
        ("Værelser", vaerelser),
        ("Grupper & konti", grupper),
        ("Værktøjer", vaerktoejer),
        ("Administration", administration),
        ("Ressourcer", ressourcer),
    ]
    return [s for s in sections if s[1]]


def _active_nav_url(sections: list[NavSection], path: str) -> str:
    """Which sidebar item to highlight for `path` — the longest one that is a prefix of it.

    base.html used to compare `request.path == url`, which broke as soon as a nav destination grew
    sub-pages: /den-hurtige/i-byen/ is a real page under the "Den Hurtige" item but is not that
    item's URL, so nothing lit up. Longest-prefix keeps the more specific item winning where two
    nest (/ak/ vs /ak/admin) — the behaviour exact matching already had. The Dashboard, at "/", is
    a prefix of everything and so only wins where nothing more specific does.

    External links (the wiki, the feedback form) are skipped: they are never the current page, and
    an https:// prefix could not match a path anyway.
    """
    best = ""
    for _section, items in sections:
        for url, _label, _icon in items:
            if "://" in url:
                continue
            if path.startswith(url) and len(url) > len(best):
                best = url
    return best


def _preview_label(roles: frozenset[str]) -> str:
    """How the preview banner names what the site is shown as."""
    if not roles:
        return "aldrig på alumnelisten (ingen adgang)"
    named = sorted(ROLE_LABELS[role] for role in roles - {RESIDENT})
    return ", ".join(named) if named else "beboer (ingen rolle)"


def navigation(request: HttpRequest) -> dict[str, object]:
    authed = request.user.is_authenticated
    roles = request_roles(request) if authed else frozenset()
    previewer = can_preview(request.user) if authed else False
    nav_intern = _nav_intern(request, roles, request.user.pk or 0) if authed else []
    previewed = previewed_roles(request) if previewer else None
    ctx: dict[str, object] = {
        "public_site_url": settings.PUBLIC_SITE_URL,
        "nav_intern": nav_intern,
        "active_nav_url": _active_nav_url(nav_intern, request.path),
        "preview_active": previewed is not None and PREVIEW_SESSION_KEY in request.session,
        "preview_label": _preview_label(previewed) if previewed is not None else "",
        "can_preview": previewer,
        "wiki_url": settings.WIKI_URL,
    }
    # DEV-ONLY simulated clock bar (F-004 local testing). Only when DEBUG and logged in; inert in prod.
    if settings.DEBUG and authed:
        from core.clock import current_date

        ctx["dev_clock_debug"] = True
        ctx["dev_clock_date"] = current_date()
    return ctx
