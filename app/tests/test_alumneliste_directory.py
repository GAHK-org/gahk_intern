"""Alumneliste page links that must reproduce what is on screen: the ✎ edit link's return target,
the export buttons and the room-clash banner."""

from collections.abc import Callable
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from django.test import Client

from core.models import Room
from residents.models import Residency, Resident, active_period, next_period, prev_period


def _indstilling(make_resident: Callable) -> Client:
    c = Client()
    c.force_login(make_resident(email="dir-ind@gahk.dk", roles=("indstilling",)))
    return c


def _on_list(resident: Resident, number: int, period: tuple[int, int]) -> None:
    room, _ = Room.objects.get_or_create(
        number=number, defaults={"legacy_index": number, "floor": "stuen", "side": "mod gaden"}
    )
    Residency.objects.create(resident=resident, room=room, year=period[0], month=period[1])


@pytest.mark.django_db
def test_edit_link_after_htmx_search_returns_to_the_full_page(make_resident: Callable) -> None:
    """The ✎ link's ?next= was request.get_full_path — after an htmx search/sort that is the bare
    /alumneliste/rows fragment, so saving landed Indstillingen on an unstyled table."""
    y, m = active_period()
    target = make_resident(email="dir-t@gahk.dk", first_name="Søgbar")
    _on_list(target, 301, (y, m))
    c = _indstilling(make_resident)

    html = c.get("/intern/alumneliste/rows", {"q": "Søg", "sort": "vaerelse", "dir": "desc"}).content.decode()
    nxt = unquote(html.split("?next=", 1)[1].split('"', 1)[0])
    parts = urlsplit(nxt)
    assert parts.path == "/intern/alumneliste/"
    assert parse_qs(parts.query) == {
        "period": [f"{y}-{m}"],
        "q": ["Søg"],
        "sort": ["vaerelse"],
        "dir": ["desc"],
    }


@pytest.mark.django_db
def test_export_links_carry_search_and_sort(make_resident: Callable) -> None:
    """Exports passed only format + period, so they ignored the search and sort on screen; after an
    htmx search the links (outside the swapped table) are refreshed out of band."""
    y, m = active_period()
    c = _indstilling(make_resident)
    page = c.get("/intern/alumneliste/", {"q": "abc", "sort": "studie", "dir": "desc"}).content.decode()
    assert "eksport?format=csv&amp;period=" in page
    assert "q=abc&amp;sort=studie&amp;dir=desc" in page

    frag = c.get("/intern/alumneliste/rows", {"q": "xyz", "period": f"{y}-{m}"}).content.decode()
    assert 'id="dir-export"' in frag and 'hx-swap-oob="true"' in frag
    assert "q=xyz&amp;sort=navn&amp;dir=asc" in frag


@pytest.mark.django_db
def test_clash_banner_links_to_the_month_being_viewed(make_resident: Callable) -> None:
    """The banner always linked to the editor's default (next month), even for a clash in the live
    list — which the editor can fix, but only under ?period=current. A past month can't be edited."""
    cur = active_period()
    nxt, past = next_period(cur), prev_period(cur)
    for i, period in enumerate((cur, nxt, past)):
        _on_list(make_resident(email=f"clash{i}a@gahk.dk"), 320 + i, period)
        _on_list(make_resident(email=f"clash{i}b@gahk.dk"), 320 + i, period)
    c = _indstilling(make_resident)

    def banner(period: tuple[int, int]) -> str:
        html = c.get("/intern/alumneliste/", {"period": f"{period[0]}-{period[1]}"}).content.decode()
        assert "Værelseskonflikt" in html
        return html.split("Værelseskonflikt", 1)[1].split("</p>", 1)[0]

    assert "naeste-maaned?period=current" in banner(cur)
    assert "naeste-maaned?period=next" in banner(nxt)
    assert "naeste-maaned" not in banner(past)
