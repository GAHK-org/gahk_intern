"""Sidebar navigation + role/preview state, available to every template."""

from collections.abc import Collection

from django.conf import settings
from django.http import HttpRequest

from arkiv.access import roles_allowed as arkiv_allowed
from den_hurtige.access import roles_allowed as den_hurtige_allowed
from events.access import roles_allowed as events_allowed
from koekken.access import roles_allowed as koekken_allowed
from opslagstavle.access import roles_allowed as opslagstavle_allowed
from photo_album.access import roles_allowed as photo_album_allowed
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
    # EVERY ONE OF THESE GATES IS NOW OPEN — all four features shipped their staged rollout and
    # their ACCESS_ROLES are None, so every call below answers True for every logged-in resident.
    #
    # THE GUARDS STAY ANYWAY, and that is the point of asking rather than a leftover. Each feature's
    # access module promises that re-gating it is one edit ("TO RE-GATE IT: set ACCESS_ROLES to a
    # tuple of roles") which narrows the views AND the sidebar entry together. Inlining these
    # because they happen to be True today would quietly break the second half of that promise: the
    # sidebar would go on advertising a page that answers 403, and the next person to close a gate
    # would have no reason to look in this file. The call is a dict lookup against a role set.
    if den_hurtige_allowed(roles):
        oversigt.append(("/intern/den-hurtige/", "Den Hurtige", "flash"))
    if opslagstavle_allowed(roles):
        oversigt.append(("/intern/opslagstavle/", "Opslagstavle", "board"))
    if events_allowed(roles):
        oversigt.append(("/intern/begivenheder/", "Begivenheder", "calendar"))
    # Alumneliste stays here; Stamtræ and Statistik moved to Ressourcer (see below).
    oversigt.append(("/intern/alumneliste/", "Alumneliste", "list"))
    if photo_album_allowed(roles):
        oversigt.append(("/intern/fotoalbum/", "Fotoalbum", "photo"))
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
    if koekken_allowed(roles):
        grupper.append(("/intern/koekken/", "Køkkenvagter", "check"))
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
    # Stamtræ and Statistik sit here rather than under Oversigt, which they used to. Oversigt is
    # what is happening NOW — the dashboard, your own profile, the three feeds, who lives here — and
    # you open those to see whether anything has changed. These are reference: nothing in them
    # changes between visits, and you go to them to look something up. That is what Ressourcer is,
    # and it is why the wiki and the arkiv belong in it too.
    #
    # Internal pages FIRST, the external links last. Keeping the outbound links together at the
    # bottom stops the sidebar mixing "leaves the site" with "does not" halfway down a list. Note
    # _active_nav_url skips anything with a scheme, so only the internal entries here can light up.
    #
    # The two UNCONDITIONAL entries lead and the gated one follows, so the top of the section does
    # not move depending on who is reading it.
    ressourcer: list[NavItem] = [
        ("/intern/stamtree/", "Stamtræ", "tree"),
        ("/intern/statistik/", "Statistik", "chart"),
    ]
    # Conditional on the rollout gate, like Den Hurtige / Opslagstavlen / Begivenheder above — open
    # today, and kept conditional for the reason given up there.
    if arkiv_allowed(roles):
        ressourcer.append(("/intern/arkiv/", "Arkiv", "archive"))
    ressourcer += [
        (settings.WIKI_URL, "Wiki", "book"),
        (settings.FEEDBACK_URL, "Fejl & ønsker", "bug"),
    ]
    # kokkengruppe: dedicated screens shipped in P2 -- see the koekken_allowed() entry under
    # "Grupper & konti" above, which is the ONE sidebar link (the resident index). Køkkengruppen's
    # queue and Regnskab's balance export have no separate sidebar entries of their own (F10) -- they
    # are reached from that same resident index page, as `can_manage`/`can_view_balance_export`-gated
    # links (koekken.views._resident_context), matching this house's existing preference for surfacing
    # an officer view from within a feature's own page rather than growing the sidebar per role. Those
    # two officer-only checks are separate from koekken.access's ACCESS_ROLES (now open to the whole
    # house, P2 design doc §13 phase 3) and are unaffected by it -- see koekken.access's docstring.
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
    # Køkkenvagter preference-window banner -- P2 design doc §8: a site-wide banner during the ~1-
    # week window, extending this same base.html mechanism to a RESIDENT-facing case for the first
    # time (the two blocks above are administrator/dev-only). Gated on koekken_allowed too, not just
    # the window itself, so nobody sees a banner for a feature they cannot yet open (see
    # koekken.access's module docstring for the current rollout state).
    #
    # §12's open item, resolved: the banner persists PER RESIDENT until they've personally declared or
    # the window closes -- not just "is a window open" for everyone. The extra per-resident reads
    # (`preference_target_periode`, `resident_has_declared_for`) only run once `in_preference_window()`
    # has already found a window (still `None`, no query, on every other day of the year) -- see all
    # three functions' docstrings in koekken.services for why that ordering is load-bearing (F6).
    #
    # The periode named here is `preference_target_periode`'s result -- the SAME shared, pure resolver
    # `koekken.views.praeferencer`'s form page also calls (F3), so the two can never show a resident a
    # different periode name. Since the A1.3 supplement (2026-10-01, "an open preference window wins
    # over everything else"), that result now always EQUALS `in_preference_window()`'s own periode
    # whenever a window is open -- the two used to routinely diverge (a first-time declarer's write
    # landed on their CURRENT periode, not the window's; a deadline-day submission could be pushed one
    # periode further still), which is why this went through a dedicated resolver rather than the
    # window's own periode in the first place. That divergence is gone now, but the shared call stays:
    # it is what keeps the banner and the form page unable to drift apart again if either side's
    # resolution logic ever changes.
    if authed and koekken_allowed(roles):
        from koekken.services import (
            in_preference_window,
            preference_target_periode,
            resident_has_declared_for,
        )
        from residents.permissions import current_resident

        if in_preference_window() is not None:
            resident = current_resident(request)
            target_periode = preference_target_periode(resident)
            if not resident_has_declared_for(resident, target_periode):
                ctx["koekken_preference_window_periode"] = target_periode
    return ctx
