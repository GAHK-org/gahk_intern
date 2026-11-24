"""The access app: features, the table, the gate, and the names the rest of the site asks for."""

import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from django.contrib.auth.models import AnonymousUser, Group
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver

from access import permissions as access_permissions
from access.api import ADMINISTRATOR, KITCHEN, RESIDENT, Feature, check, feature_of, holders
from access.permissions import codenames_of, names_of
from access.policy import POLICY, roles_with
from access.roles import sync, sync_group_permissions
from residents.models import Account, Role

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

_ASKED = re.compile(r"""(?:has_perm|permission_required|holders|with_perm)\(\s*f?["'](access\.[a-z_]+)["']""")
_IN_TEMPLATES = re.compile(r"\bperms\.(access\.[a-z_]+)")


def test_every_permission_the_code_asks_for_exists() -> None:
    """check() refuses an unknown name, but only when the line runs: this reads them all."""
    asked = {
        found
        for path in APP.rglob("*.py")
        if "tests" not in path.parts and "migrations" not in path.parts
        for found in _ASKED.findall(path.read_text())
        if "{" not in found
    }
    assert "access.oelkaelder_manage" in asked and "access.reparationer_move" in asked
    for permission in asked:
        check(permission)


def test_every_permission_a_template_asks_for_exists() -> None:
    """A template's `perms.access.<codename>` with a typo would quietly be False."""
    asked = {
        found
        for path in (APP / "templates").rglob("*.html")
        for found in _IN_TEMPLATES.findall(path.read_text())
    }
    assert "access.directory_manage" in asked
    for permission in asked:
        check(permission)


def test_an_unknown_name_fails_where_it_is_written() -> None:
    with pytest.raises(ValueError):
        check("access.oelkaelder_drink")


# ---- the table -------------------------------------------------------------------------------------


def test_a_role_set_gets_the_highest_grade_any_role_gives() -> None:
    resident = codenames_of({RESIDENT})
    assert {"oelkaelder_view", "oelkaelder_use"} <= resident and "oelkaelder_manage" not in resident
    assert not {c for c in resident if c.startswith("accounting_")}
    both = codenames_of({RESIDENT, Role.OELKAELDER})
    assert {"oelkaelder_view", "oelkaelder_use", "oelkaelder_manage"} <= both


def test_manage_includes_the_features_extras_and_an_extra_only_itself() -> None:
    assert "reparationer_move" in codenames_of({RESIDENT, Role.REPPER})
    vicevaert = codenames_of({RESIDENT, Role.VICEVAERT})
    assert "reparationer_move" in vicevaert and "reparationer_manage" not in vicevaert


def test_administer_is_the_administrators_alone() -> None:
    assert "photo_album_administer" not in codenames_of({RESIDENT, Role.FOTO})
    assert "website_administer" not in codenames_of({RESIDENT, Role.PR})
    administrator = codenames_of({RESIDENT, ADMINISTRATOR})
    assert all(f"{feature}_administer" in administrator for feature in Feature)


def test_the_kitchen_gets_its_four_features_and_the_account_pages() -> None:
    kitchen = {c.rsplit("_", 1)[0] for c in codenames_of({KITCHEN}) if c.endswith("_view")}
    assert kitchen == {
        "account",
        "public",
        "files",
        "den_hurtige",
        "opslagstavle",
        "reparationer",
        "notifications",
    }


def test_the_group_tools_are_closed_to_a_plain_resident() -> None:
    resident = names_of({RESIDENT})
    for tool in (Feature.ADMISSIONS, Feature.WEBSITE, Feature.ACCOUNTING, Feature.SITE_ADMIN):
        assert f"access.{tool}_view" not in resident


def test_who_has_a_permission_is_read_from_the_table() -> None:
    assert roles_with(Feature.REPARATIONER, "move") >= {Role.VICEVAERT, Role.REPPER, Role.INSPEKTION}
    assert RESIDENT not in roles_with(Feature.REPARATIONER, "move")
    assert roles_with(Feature.DEN_HURTIGE, "use") >= {RESIDENT, KITCHEN}


@pytest.mark.django_db
def test_each_roles_group_holds_what_the_table_gives_it() -> None:
    """The groups are written from the table at every migrate (access.apps)."""
    for role in POLICY:
        held = set(Group.objects.get(name=role).permissions.values_list("codename", flat=True))
        assert held == set(codenames_of({role})), role


@pytest.mark.django_db
def test_an_accounts_groups_follow_its_roles(make_resident: Callable[..., Account]) -> None:
    anna = make_resident(email="anna@gahk.dk", roles=(Role.AK,))
    assert set(anna.groups.values_list("name", flat=True)) == {RESIDENT, Role.AK}
    anna.role_assignments.all().delete()
    assert set(Account.objects.get(pk=anna.pk).groups.values_list("name", flat=True)) == {RESIDENT}
    Account.objects.filter(pk=anna.pk).update(is_kitchen=True)
    sync([anna.pk])
    assert set(anna.groups.values_list("name", flat=True)) == {KITCHEN}


# ---- the gate --------------------------------------------------------------------------------------


@pytest.fixture
def ak_read_only(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A resident whose AK is view only, as a former resident's will be."""
    from access.features import Grade

    monkeypatch.setitem(POLICY, RESIDENT, {**POLICY[RESIDENT], Feature.AK: Grade.VIEW})
    access_permissions.forget()
    sync_group_permissions()
    yield
    monkeypatch.undo()
    access_permissions.forget()
    sync_group_permissions()


@pytest.mark.django_db
@pytest.mark.usefixtures("ak_read_only")
def test_view_lets_a_page_open_but_nothing_be_sent(make_resident: Callable[..., Account]) -> None:
    client = Client()
    client.force_login(make_resident())
    assert client.get("/ak/").status_code == 200
    assert client.post("/ak/tilfoej", {}).status_code == 403


@pytest.mark.django_db
def test_a_page_of_a_feature_one_lacks_answers_403(make_resident: Callable[..., Account]) -> None:
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
def test_a_role_tool_is_its_groups(make_resident: Callable[..., Account]) -> None:
    client = Client()
    client.force_login(make_resident(roles=(Role.REGNSKAB,)))
    assert client.get("/regnskab/").status_code == 200
    assert client.get("/admin/").status_code == 403


# ---- asking ----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_holders_are_the_accounts_the_table_gives_it(make_resident: Callable[..., Account]) -> None:
    plain = make_resident(email="plain@gahk.dk")
    foto = make_resident(email="foto@gahk.dk", roles=(Role.FOTO,))
    admin = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    superuser = make_resident(email="super@gahk.dk", is_superuser=True)
    kitchen = make_resident(email="kitchen@gahk.dk", is_kitchen=True)
    gone = make_resident(email="gone@gahk.dk", roles=(Role.FOTO,), is_active=False)

    managers = set(holders("access.photo_album_manage"))
    assert managers == {foto, admin, superuser}
    assert set(holders("access.den_hurtige_use")) == {plain, foto, admin, superuser, kitchen}
    assert gone not in set(holders("access.photo_album_view"))
    assert set(holders("access.photo_album_administer")) == {admin, superuser}


@pytest.mark.django_db
def test_has_perm_reads_the_accounts_own_groups(make_resident: Callable[..., Account]) -> None:
    assert make_resident(email="pr@gahk.dk", roles=(Role.PR,)).has_perm("access.website_manage")
    assert not make_resident(email="b@gahk.dk").has_perm("access.website_manage")
    assert not AnonymousUser().has_perm("access.dashboard_view")


@pytest.mark.django_db
def test_a_template_asks_access(make_resident: Callable[..., Account]) -> None:
    client = Client()
    client.force_login(make_resident(roles=(Role.AK,)))
    html = client.get("/ak/").content.decode()
    assert "/ak/admin" in html  # perms.access.ak_manage
    client.force_login(make_resident(email="plain@gahk.dk"))
    assert "/ak/admin" not in client.get("/ak/").content.decode()
