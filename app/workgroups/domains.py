"""The intranet pages each embedsgruppe looks after, for its page (views.workgroup_page).

Keyed by the role a group grants (residents.models.WORKGROUP_ROLE), because the role is what opens
these pages: a link is shown only to someone who holds it, so nobody is offered a page that answers
403. A group without a role has no pages of its own, and that part of its page is left out.

Every link here is a page its role is required for, or one the group runs for everyone (the
Reparationer board, the photo album). Add a link when a group gets a new page; the sidebar keeps its
own, shorter list.
"""

from collections.abc import Callable, Collection
from dataclasses import dataclass

from django.urls import reverse

from drive.access import roles_allowed as drive_allowed
from gahkflix.access import roles_allowed as gahkflix_allowed
from residents.models import Role


@dataclass(frozen=True)
class Link:
    url_name: str
    label: str
    note: str
    icon: str  # a symbol in base.html's sprite
    # A rollout gate the page sits behind besides the role, if any.
    gate: Callable[[Collection[str]], bool] | None = None

    @property
    def url(self) -> str:
        return reverse(self.url_name)


DOMAINS: dict[str, tuple[Link, ...]] = {
    Role.INDSTILLING: (
        Link("next_month_list", "Næste måneds alumneliste", "Værelser, embedsgrupper og rengøring", "list"),
        Link("admissions:list", "Ansøgninger", "Ansøgninger om optagelse", "inbox"),
        Link("soegvaerelse:admin", "Værelsesudbud", "Udbud, ansøgere og runder", "offer"),
        Link("cms:overview", "Hjemmeside", "Sider, nyheder og begivenheder på gahk.dk", "globe"),
    ),
    Role.INSPEKTION: (
        Link("reparationer:board", "Reparationer", "Alle opgaver, status og ansvarlig", "wrench"),
        Link("drive:home", "Drive", "Overblik over embedsgruppernes mapper", "folder", drive_allowed),
        Link("cms:overview", "Hjemmeside", "Sider, nyheder og begivenheder på gahk.dk", "globe"),
    ),
    Role.AK: (
        Link("ak:overview", "AK-oversigt", "Alles krydser og månedens opkrævning", "check"),
        Link(
            "vaerelsestjek:akoverview", "Værelsestjek-oversigt", "Alle værelsers svar, med eksport", "inspect"
        ),
    ),
    Role.OELKAELDER: (
        Link("oelkaelder:admin", "Ølkælder-admin", "Konti, indbetalinger, rente og købsgrænse", "beer"),
        Link("oelkaelder:all_sales", "Salgsoverblik", "Alle salg, og annullering", "sales"),
        Link("oelkaelder:person_history", "Personoversigt", "Én beboers køb og indbetalinger", "users"),
        Link("oelkaelder:products", "Produkter", "Varer og priser i baren", "beer"),
    ),
    Role.REGNSKAB: (
        Link("regnskab:overview", "Regnskab", "Fraflyttede, beboeropslag og alumneregninger", "receipt"),
        Link("regnskab:alumneregning_settings", "Alumneregningens tekster", "Mailtekst og følgebrev", "gear"),
    ),
    Role.PR: (
        Link("cms:overview", "Hjemmeside", "Overblik over gahk.dk", "globe"),
        Link("cms:news_list", "Nyheder", "Nyheder på gahk.dk", "edit"),
        Link("cms:event_list", "Begivenheder på gahk.dk", "Den offentlige kalender", "calendar"),
        Link("cms:page_list", "Sider", "Sider på gahk.dk", "file"),
    ),
    Role.REPPER: (
        Link("reparationer:board", "Reparationer", "Tag opgaver, sæt status og ansvarlig", "wrench"),
        Link("reparationer:archive_list", "Arkiverede reparationer", "Afsluttede opgaver", "archive"),
    ),
    Role.VICEVAERT: (
        Link("reparationer:board", "Reparationer", "Visitér nye opgaver og send dem videre", "wrench"),
    ),
    Role.FOTO: (
        Link("photo_album:index", "Fotoalbum", "Godkend uploads og ryd op i albummerne", "photo"),
        Link("gahkflix:home", "GAHKFlix", "Film og premierer", "film", gahkflix_allowed),
    ),
}


def links_for(role: str | None, roles: Collection[str]) -> list[Link]:
    """The pages of the group granting `role` that someone holding `roles` may open."""
    if role is None or role not in roles:
        return []
    return [link for link in DOMAINS.get(role, ()) if link.gate is None or link.gate(roles)]
