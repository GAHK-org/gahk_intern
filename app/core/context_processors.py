"""Sidebar navigation + role/preview state, available to every template."""

from collections.abc import Collection

from django.conf import settings
from django.http import HttpRequest

from den_hurtige.access import roles_allowed as den_hurtige_allowed
from events.access import roles_allowed as events_allowed
from opslagstavle.access import roles_allowed as opslagstavle_allowed
from residents.permissions import CMS_EDITOR_ROLES, can_preview, effective_roles

# Public site nav, matching the legacy gahk.dk menu (labels + order)
NAV_LEGACY = [
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

NAV_PUBLIC = [
    ("/", "Forside"),
    ("/faciliteter", "Faciliteter"),
    ("/kollegielivet", "Kollegielivet"),
    ("/vision", "Vision"),
    ("/legater", "Legater"),
    ("/begivenheder/", "Begivenheder"),
    ("/kontakt", "Kontakt"),
    ("/optagelse/", "Optagelse"),
]


NavItem = tuple[str, str, str]  # (url, label, icon-key from the base.html sprite)
NavSection = tuple[str, list[NavItem]]


def _nav_intern(roles: Collection[str], user_pk: int) -> list[NavSection]:
    """Internal menu grouped into sidebar sections, built from the *effective* role set: base items for
    every resident plus each embedsgruppe's admin tools. Each item is (url, label, icon); empty sections
    are dropped. Honors the preview override (roles come from effective_roles)."""
    oversigt: list[NavItem] = [
        ("/intern/", "Dashboard", "dashboard"),
        (f"/intern/beboer/{user_pk}/profil", "Min profil", "users"),
    ]
    # Den Hurtige is limited to the administrator group during its trial; the single switch is
    # den_hurtige.access.ACCESS_ROLES. Asking it here keeps the sidebar from advertising a page that
    # would answer 403, and means opening the rollout needs no change in this file.
    if den_hurtige_allowed(roles):
        oversigt.append(("/intern/den-hurtige/", "Den Hurtige", "flash"))
    # Gated the same way while Ankebogen is being tried out (opslagstavle.access.ACCESS_ROLES).
    # Asking here keeps the sidebar from advertising a page that would answer 403, and means opening
    # the rollout needs no change in this file.
    if opslagstavle_allowed(roles):
        oversigt.append(("/intern/ankebogen/", "Ankebogen", "board"))
    if events_allowed(roles):
        oversigt.append(("/intern/begivenheder/", "Begivenheder", "calendar"))
    oversigt += [
        ("/intern/alumneliste/", "Alumneliste", "list"),
        ("/intern/stamtree/", "Stamtræ", "tree"),
        ("/intern/statistik/", "Statistik", "chart"),
    ]
    vaerelser: list[NavItem] = [
        ("/intern/soegvaerelse/", "Søg værelse", "house"),
        ("/intern/vaerelsestjek/", "Værelsestjek", "inspect"),  # open to every resident
    ]
    if "indstilling" in roles:
        vaerelser.append(("/intern/soegvaerelse/admin", "Værelsesudbud", "offer"))
    grupper: list[NavItem] = [
        ("/intern/ak/", "AK-krydser", "check"),
        ("/intern/oelkaelder/min-saldo", "Ølkælder", "beer"),
    ]
    if "ak" in roles:
        grupper.append(("/intern/ak/admin", "AK-oversigt", "check"))
    if "oelkaelder" in roles:
        # Salgsoverblik / Personoversigt are sub-pages reached from Ølkælder-admin, not separate nav items.
        grupper.append(("/intern/oelkaelder/admin", "Ølkælder-admin", "beer"))
    if "regnskab" in roles:
        grupper.append(("/intern/regnskab/", "Regnskab", "receipt"))
    # Open to every resident, same as Opslagstavlen: reporting a repair needs no role. Reppergruppen
    # manages the board once inside — see reparationer.views.MANAGE_ROLES.
    vaerktoejer: list[NavItem] = [
        ("/intern/reparationer/", "Reparationer", "wrench"),
    ]
    administration: list[NavItem] = []
    if "indstilling" in roles:
        administration.append(("/optagelse/listansoegninger", "Ansøgninger", "inbox"))
    if not set(roles).isdisjoint(CMS_EDITOR_ROLES):  # administrator/indstilling/inspektion/pr
        administration.append(("/django-admin/cms/", "Rediger indhold", "edit"))
    if "administrator" in roles:
        administration.append(("/admin/", "Site-admin", "gear"))
        administration.append(("/admin/roles", "Roller", "users"))
    ressourcer: list[NavItem] = [
        (settings.WIKI_URL, "Wiki", "book"),
        (settings.FEEDBACK_URL, "Fejl & ønsker", "bug"),
    ]
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
    sub-pages: /intern/den-hurtige/i-byen/ is a real page under the "Den Hurtige" item but is not
    that item's URL, so nothing lit up. Longest-prefix keeps the more specific item winning where
    two nest (/intern/ak/ vs /intern/ak/admin) — the behaviour exact matching already had.

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


def navigation(request: HttpRequest) -> dict[str, object]:
    authed = request.user.is_authenticated
    roles = effective_roles(request) if authed else set()
    previewer = can_preview(request.user) if authed else False
    nav_intern = _nav_intern(roles, request.user.pk or 0) if authed else []
    ctx: dict[str, object] = {
        "nav_legacy": NAV_LEGACY,
        "nav_public": NAV_PUBLIC,
        "nav_intern": nav_intern,
        "active_nav_url": _active_nav_url(nav_intern, request.path),
        "effective_roles": sorted(roles),
        "preview_active": bool(previewer and "preview_roles" in request.session),
        "can_preview": previewer,
        "wiki_url": settings.WIKI_URL,
    }
    # DEV-ONLY simulated clock bar (F-004 local testing). Only when DEBUG and logged in; inert in prod.
    if settings.DEBUG and authed:
        from core.clock import current_date

        ctx["dev_clock_debug"] = True
        ctx["dev_clock_date"] = current_date()
    return ctx
