"""The access app: features, the table, the gate, and the names the rest of the site asks for."""

import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver

from access import permissions as access_permissions
from access.api import (
    ADMINISTRATOR,
    KITCHEN,
    RESIDENT,
    Feature,
    Grade,
    feature_of,
    holders,
    permissions_of,
    user_can,
)
from access.features import parse
from access.policy import POLICY, roles_with
from residents.models import Resident, Role

APP = Path(__file__).resolve().parent.parent


def _views(patterns: list[URLPattern | URLResolver], prefix: str = "") -> Iterator[tuple[str, Callable]]:
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _views(pattern.url_patterns, prefix + str(pattern.pattern))
        else:
            yield prefix + str(pattern.pattern), pattern.callback


def test_every_page_is_in_a_feature() -> None:
    """A page in no feature is refused to everyone: it must be put in one on purpose."""
    missing = [route for route, view in _views(get_resolver().url_patterns) if feature_of(view) is None]
    assert missing == []


# ---- the names asked for exist -----------------------------------------------------------------------

_ASKED = re.compile(
    r"""(?:permission_required|holders|user_can\([^,]+,|can\(request,)\s*\(?\s*["']([a-z_]+\.[a-z_]+)["']"""
)
_IN_TEMPLATES = re.compile(r"{%[^%]*\baccess\.([a-z_]+)\.([a-z_]+)")


def test_every_permission_the_code_asks_for_exists() -> None:
    """parse() refuses an unknown name, but only when the line runs: this reads them all."""
    asked = {
        name
        for path in APP.rglob("*.py")
        if "tests" not in path.parts and "migrations" not in path.parts
        for name in _ASKED.findall(path.read_text())
    }
    assert "oelkaelder.manage" in asked and "reparationer.move" in asked  # the pattern reads them
    for name in asked:
        parse(name)


def test_every_permission_a_template_asks_for_exists() -> None:
    """A template's `access.<feature>.<action>` with a typo would quietly be False."""
    asked = {
        f"{feature}.{action}"
        for path in (APP / "templates").rglob("*.html")
        for feature, action in _IN_TEMPLATES.findall(path.read_text())
    }
    assert "directory.manage" in asked
    for name in asked:
        parse(name)


def test_an_unknown_name_fails_where_it_is_written() -> None:
    with pytest.raises(ValueError):
        parse("oelkaelder.drink")
    with pytest.raises(ValueError):
        parse("bar.use")


# ---- the table -------------------------------------------------------------------------------------


def test_a_role_set_gets_the_highest_grade_any_role_gives() -> None:
    resident = permissions_of({RESIDENT})
    assert resident.grade(Feature.OELKAELDER) == Grade.USE
    assert resident.grade(Feature.ACCOUNTING) == Grade.NONE
    both = permissions_of({RESIDENT, Role.OELKAELDER})
    assert both.grade(Feature.OELKAELDER) == Grade.MANAGE
    assert both.allows("oelkaelder.view") and both.allows("oelkaelder.manage")


def test_manage_includes_the_features_extras_and_an_extra_only_itself() -> None:
    assert permissions_of({RESIDENT, Role.REPPER}).allows("reparationer.move")
    vicevaert = permissions_of({RESIDENT, Role.VICEVAERT})
    assert vicevaert.allows("reparationer.move")
    assert not vicevaert.allows("reparationer.manage")


def test_administer_is_the_administrators_alone() -> None:
    assert not permissions_of({RESIDENT, Role.FOTO}).allows("photo_album.administer")
    assert not permissions_of({RESIDENT, Role.PR}).allows("website.administer")
    administrator = permissions_of({RESIDENT, ADMINISTRATOR})
    assert all(administrator.grade(feature) == Grade.ADMINISTER for feature in Feature)


def test_the_kitchen_gets_its_four_features_and_the_account_pages() -> None:
    kitchen = permissions_of({KITCHEN})
    assert kitchen.features() == {
        Feature.ACCOUNT,
        Feature.PUBLIC,
        Feature.FILES,
        Feature.DEN_HURTIGE,
        Feature.OPSLAGSTAVLE,
        Feature.REPARATIONER,
        Feature.NOTIFICATIONS,
    }


def test_the_group_tools_are_closed_to_a_plain_resident() -> None:
    resident = permissions_of({RESIDENT})
    for tool in (Feature.ADMISSIONS, Feature.WEBSITE, Feature.ACCOUNTING, Feature.SITE_ADMIN):
        assert resident.grade(tool) == Grade.NONE


def test_who_has_a_permission_is_read_from_the_table() -> None:
    assert roles_with(Feature.REPARATIONER, "move") >= {Role.VICEVAERT, Role.REPPER, Role.INSPEKTION}
    assert RESIDENT not in roles_with(Feature.REPARATIONER, "move")
    assert roles_with(Feature.DEN_HURTIGE, "use") >= {RESIDENT, KITCHEN}


# ---- the gate --------------------------------------------------------------------------------------


@pytest.fixture
def ak_read_only(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A resident whose AK is view only, as a former resident's will be."""
    monkeypatch.setitem(POLICY, RESIDENT, {**POLICY[RESIDENT], Feature.AK: Grade.VIEW})
    access_permissions._of.cache_clear()
    yield
    access_permissions._of.cache_clear()


@pytest.mark.django_db
@pytest.mark.usefixtures("ak_read_only")
def test_view_lets_a_page_open_but_nothing_be_sent(make_resident: Callable[..., Resident]) -> None:
    client = Client()
    client.force_login(make_resident())
    assert client.get("/ak/").status_code == 200
    assert client.post("/ak/tilfoej", {}).status_code == 403


@pytest.mark.django_db
def test_a_page_of_a_feature_one_lacks_answers_403(make_resident: Callable[..., Resident]) -> None:
    client = Client()
    client.force_login(make_resident())
    assert client.get("/regnskab/").status_code == 403
    assert client.get("/optagelse/listansoegninger").status_code == 403


@pytest.mark.django_db
def test_nobody_logged_in_is_sent_to_log_in(client: Client) -> None:
    response = client.get("/alumneliste/")
    assert response.status_code == 302 and response["Location"].startswith("/admin/login?next=")


@pytest.mark.django_db
def test_a_poll_says_nothing_to_someone_the_gate_refuses(client: Client) -> None:
    assert client.get("/den-hurtige/opslag").status_code == 204
    assert client.get("/alumneliste/rows", HTTP_HX_REQUEST="true").status_code == 204


@pytest.mark.django_db
def test_the_account_pages_and_public_ones_need_no_login(client: Client) -> None:
    assert client.get("/admin/login").status_code == 200
    assert client.get("/install").status_code == 200


@pytest.mark.django_db
def test_a_role_tool_is_its_groups(make_resident: Callable[..., Resident]) -> None:
    client = Client()
    client.force_login(make_resident(roles=(Role.REGNSKAB,)))
    assert client.get("/regnskab/").status_code == 200
    assert client.get("/admin/").status_code == 403


# ---- asking ----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_holders_are_the_accounts_the_table_gives_it(make_resident: Callable[..., Resident]) -> None:
    plain = make_resident(email="plain@gahk.dk")
    foto = make_resident(email="foto@gahk.dk", roles=(Role.FOTO,))
    admin = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    superuser = make_resident(email="super@gahk.dk", is_superuser=True)
    kitchen = make_resident(email="kitchen@gahk.dk", is_kitchen=True)
    gone = make_resident(email="gone@gahk.dk", roles=(Role.FOTO,), is_active=False)

    managers = set(holders("photo_album.manage"))
    assert managers == {foto, admin, superuser}
    assert set(holders("den_hurtige.use")) == {plain, foto, admin, superuser, kitchen}
    assert gone not in set(holders("photo_album.view"))
    assert set(holders("photo_album.administer")) == {admin, superuser}


@pytest.mark.django_db
def test_user_can_reads_the_accounts_own_roles(make_resident: Callable[..., Resident]) -> None:
    assert user_can(make_resident(email="pr@gahk.dk", roles=(Role.PR,)), "website.manage")
    assert not user_can(make_resident(email="b@gahk.dk"), "website.manage")
    assert not user_can(AnonymousUser(), "dashboard.view")


@pytest.mark.django_db
def test_a_template_asks_access(make_resident: Callable[..., Resident]) -> None:
    client = Client()
    client.force_login(make_resident(roles=(Role.AK,)))
    html = client.get("/ak/").content.decode()
    assert "/ak/admin" in html  # access.ak.manage
    client.force_login(make_resident(email="plain@gahk.dk"))
    assert "/ak/admin" not in client.get("/ak/").content.decode()
