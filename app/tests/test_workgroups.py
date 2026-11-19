"""Din embedsgruppe: members, Drive folders, the wiki link and the pages each group looks after.

Every test runs on 19 November 2026, in Drive's `2026-27 Vinter` period.
"""

from collections.abc import Callable
from datetime import date

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from core.models import Room, Workgroup
from residents.models import Residency, Resident, Role
from workgroups.domains import DOMAINS, links_for

TODAY = date(2026, 11, 19)
URL = "/workgroup/"


@pytest.fixture(autouse=True)
def _today(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.clock
    import drive.access
    import drive.structure

    for module in (core.clock, drive.access, drive.structure):
        monkeypatch.setattr(module, "current_date", lambda: TODAY)
    # drive.structure.ensure_structure_today remembers the day in the cache, which outlives each
    # test's database.
    cache.clear()


@pytest.fixture
def rooms(db: None) -> list[Room]:
    return [
        Room.objects.create(legacy_index=n, number=n, floor="stuen", side="mod gaden") for n in range(1, 6)
    ]


@pytest.fixture
def regnskab(db: None) -> Workgroup:
    return Workgroup.objects.get_or_create(name="Regnskabsgruppen")[0]


@pytest.fixture
def badgruppen(db: None) -> Workgroup:
    return Workgroup.objects.get_or_create(name="Badgruppen")[0]


@pytest.fixture
def place(rooms: list[Room]) -> Callable[..., None]:
    def _place(resident: Resident, workgroup: Workgroup | None, month: tuple[int, int] = (2026, 11)) -> None:
        Residency.objects.create(
            resident=resident,
            room=rooms[resident.pk % len(rooms)],
            workgroup=workgroup,
            year=month[0],
            month=month[1],
        )

    return _place


def _client(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def test_the_page_shows_your_group_and_its_members(
    make_resident: Callable[..., Resident],
    place: Callable[..., None],
    regnskab: Workgroup,
    badgruppen: Workgroup,
) -> None:
    me = make_resident(email="me@gahk.dk", first_name="Mette", phone="12345678")
    colleague = make_resident(email="c@gahk.dk", first_name="Carl")
    stranger = make_resident(email="s@gahk.dk", first_name="Sofus")
    place(me, regnskab)
    place(colleague, regnskab)
    place(stranger, badgruppen)

    page = _client(me).get(URL).content.decode()

    assert "Din embedsgruppe" in page
    assert "<h1>Regnskabsgruppen</h1>" in page
    assert reverse("resident_profile", args=[colleague.pk]) in page
    assert "12345678" in page
    assert ">Dig<" in page
    assert reverse("resident_profile", args=[stranger.pk]) not in page
    # The other groups this month, to look at.
    assert reverse("workgroups:detail", args=[badgruppen.pk]) in page


def test_without_a_group_the_page_says_so_and_lists_the_groups(
    make_resident: Callable[..., Resident], place: Callable[..., None], badgruppen: Workgroup
) -> None:
    me = make_resident(email="me@gahk.dk")
    place(me, None)
    place(make_resident(email="b@gahk.dk"), badgruppen)

    page = _client(me).get(URL).content.decode()

    assert "Du er ikke i en embedsgruppe i november 2026" in page
    assert reverse("workgroups:detail", args=[badgruppen.pk]) in page


def test_another_group_is_shown_without_calling_it_yours(
    make_resident: Callable[..., Resident],
    place: Callable[..., None],
    regnskab: Workgroup,
    badgruppen: Workgroup,
) -> None:
    me = make_resident(email="me@gahk.dk")
    member = make_resident(email="b@gahk.dk", first_name="Bodil")
    place(me, regnskab)
    place(member, badgruppen)

    page = _client(me).get(reverse("workgroups:detail", args=[badgruppen.pk])).content.decode()

    assert '<div class="eyebrow">Embedsgruppe</div>' in page
    assert "<h1>Badgruppen</h1>" in page
    assert reverse("resident_profile", args=[member.pk]) in page


def test_next_months_changes_are_shown_once_the_list_exists(
    make_resident: Callable[..., Resident],
    place: Callable[..., None],
    regnskab: Workgroup,
    badgruppen: Workgroup,
) -> None:
    me = make_resident(email="me@gahk.dk")
    leaving = make_resident(email="l@gahk.dk", first_name="Lars", last_name="Går")
    joining = make_resident(email="j@gahk.dk", first_name="Jens", last_name="Kommer")
    place(me, regnskab)
    place(leaving, regnskab)
    place(joining, badgruppen)
    client = _client(me)

    assert "Fra december" not in client.get(URL).content.decode()

    place(me, regnskab, (2026, 12))
    place(leaving, badgruppen, (2026, 12))
    place(joining, regnskab, (2026, 12))
    page = client.get(URL).content.decode()

    assert "Fra december" in page
    assert "Kommer med: <a" in page
    assert "Jens Kommer" in page.split("Kommer med:")[1].split("</div>")[0]
    assert "Lars Går" in page.split("Stopper:")[1].split("</div>")[0]


def test_the_wiki_link_searches_for_the_groups_name(
    make_resident: Callable[..., Resident], place: Callable[..., None], regnskab: Workgroup, settings: object
) -> None:
    settings.WIKI_URL = "https://wiki.example/"  # type: ignore[attr-defined]
    me = make_resident(email="me@gahk.dk")
    place(me, regnskab)

    page = _client(me).get(URL).content.decode()

    assert "https://wiki.example/wiki/Special:Search?search=Regnskabsgruppen&amp;go=G%C3%A5" in page


def test_the_groups_pages_are_linked_for_its_members(
    make_resident: Callable[..., Resident], place: Callable[..., None], regnskab: Workgroup
) -> None:
    member = make_resident(email="me@gahk.dk", roles=(Role.REGNSKAB,))
    outsider = make_resident(email="o@gahk.dk")
    place(member, regnskab)
    place(outsider, None)

    mine = _client(member).get(URL).content.decode()
    theirs = _client(outsider).get(reverse("workgroups:detail", args=[regnskab.pk])).content.decode()

    assert "Gruppens sider" in mine
    assert reverse("regnskab:overview") in mine
    # Someone without the role would only get a 403 from these pages.
    assert "Gruppens sider" not in theirs
    assert reverse("regnskab:overview") not in theirs


def test_a_link_behind_a_rollout_gate_is_left_out_while_the_gate_is_shut(
    monkeypatch: pytest.MonkeyPatch, settings: object
) -> None:
    import gahkflix.access

    settings.DEBUG = False  # type: ignore[attr-defined]
    monkeypatch.setattr(gahkflix.access, "ACCESS_ROLES", (Role.ADMINISTRATOR,))
    names = [link.url_name for link in links_for(Role.FOTO, {Role.FOTO})]
    assert "photo_album:index" in names
    assert "gahkflix:home" not in names
    assert links_for(None, {Role.FOTO}) == []
    assert links_for(Role.FOTO, set()) == []


def test_every_domain_link_resolves() -> None:
    for links in DOMAINS.values():
        for link in links:
            assert link.url.startswith("/")


def test_drive_links_the_current_period_with_what_you_may_do(
    make_resident: Callable[..., Resident],
    place: Callable[..., None],
    regnskab: Workgroup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import drive.access
    from drive.models import Folder

    monkeypatch.setattr(drive.access, "ACCESS_ROLES", None)
    me = make_resident(email="me@gahk.dk")
    place(me, regnskab)

    page = _client(me).get(URL).content.decode()

    current = Folder.objects.get(workgroup=regnskab, kind=Folder.Kind.PERIOD, period_start=date(2026, 10, 1))
    timeless = Folder.objects.get(workgroup=regnskab, kind=Folder.Kind.TIMELESS)
    assert reverse("drive:folder", args=[current.pk]) in page
    assert reverse("drive:folder", args=[timeless.pk]) in page
    assert page.index("2026-27 Vinter") < page.index("Uden periode")
    assert "Du kan redigere" in page


def test_drive_is_left_out_while_its_gate_is_shut(
    make_resident: Callable[..., Resident], place: Callable[..., None], regnskab: Workgroup, settings: object
) -> None:
    settings.DEBUG = False  # type: ignore[attr-defined]
    me = make_resident(email="me@gahk.dk")
    place(me, regnskab)

    page = _client(me).get(URL).content.decode()

    assert "<h2>Drive</h2>" not in page


def test_drive_offers_only_what_an_outsider_may_open(
    make_resident: Callable[..., Resident],
    place: Callable[..., None],
    regnskab: Workgroup,
    badgruppen: Workgroup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import drive.access

    monkeypatch.setattr(drive.access, "ACCESS_ROLES", None)
    me = make_resident(email="me@gahk.dk")
    place(me, badgruppen)
    place(make_resident(email="r@gahk.dk"), regnskab)

    page = _client(me).get(reverse("workgroups:detail", args=[regnskab.pk])).content.decode()

    assert "Du har ikke adgang til gruppens mapper." in page


def test_the_sidebar_links_the_page(
    make_resident: Callable[..., Resident], place: Callable[..., None]
) -> None:
    me = make_resident(email="me@gahk.dk")
    place(me, None)
    assert f'href="{URL}"' in _client(me).get(reverse("dashboard")).content.decode()


def test_the_kitchen_account_cannot_open_it(make_resident: Callable[..., Resident]) -> None:
    kitchen = make_resident(email="k@gahk.dk", is_kitchen=True)
    assert _client(kitchen).get(URL).status_code == 403
