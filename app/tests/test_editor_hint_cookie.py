"""The `gahk_editor` hint cookie (cms.middleware), by gahk_web's protocol ("The edit link"): set for
CMS editors at login and whenever it is missing, deleted at logout and once the user stops
qualifying, untouched otherwise, never on static files or media."""

from collections.abc import Callable
from http.cookies import Morsel
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils.functional import SimpleLazyObject

from cms.middleware import EDITOR_HINT_COOKIE, EditorHintCookie
from residents.models import Resident, RoleAssignment
from residents.permissions import PREVIEW_SESSION_KEY

PASSWORD = "hemmelig"


@pytest.fixture(autouse=True)
def _production_cookie(settings: Any) -> None:  # noqa: ANN401
    settings.DEBUG = False
    settings.EDITOR_HINT_COOKIE_DOMAIN = ".gahk.dk"


@pytest.fixture
def editor(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="pr@gahk.dk", password=PASSWORD, roles=("pr",))


@pytest.fixture
def resident(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="beboer@gahk.dk", password=PASSWORD)


def _signed_in(who: Resident, hint: str | None = None) -> Client:
    client = Client()
    client.force_login(who)
    if hint is not None:
        client.cookies[EDITOR_HINT_COOKIE] = hint
    return client


def _hint(response: HttpResponse) -> Morsel[str] | None:
    return response.cookies.get(EDITOR_HINT_COOKIE)


def _assert_set(morsel: Morsel[str] | None) -> None:
    assert morsel is not None and morsel.value == "1"
    assert morsel.key == "gahk_editor"  # the name gahk.dk's script reads
    assert morsel["domain"] == ".gahk.dk" and morsel["path"] == "/"
    assert morsel["secure"] is True and morsel["samesite"] == "Lax"
    assert not morsel["httponly"]  # gahk.dk's script reads it
    assert int(morsel["max-age"]) > 0


def _assert_deleted(morsel: Morsel[str] | None) -> None:
    assert morsel is not None and morsel.value == ""
    assert int(morsel["max-age"]) == 0 and morsel["expires"] == "Thu, 01 Jan 1970 00:00:00 GMT"
    assert morsel["domain"] == ".gahk.dk" and morsel["path"] == "/"  # the same cookie, so it goes
    assert morsel["secure"] is True


@pytest.mark.django_db
def test_an_editor_gets_it_at_login(editor: Resident) -> None:
    response = Client().post(reverse("login"), {"username": editor.email, "password": PASSWORD})

    assert response.status_code == 302
    _assert_set(_hint(response))


@pytest.mark.django_db
def test_a_resident_without_an_editor_role_does_not(resident: Resident) -> None:
    response = Client().post(reverse("login"), {"username": resident.email, "password": PASSWORD})

    assert response.status_code == 302 and _hint(response) is None
    assert _hint(_signed_in(resident).get("/")) is None


@pytest.mark.django_db
def test_any_page_sets_it_when_the_browser_lacks_it_and_is_quiet_when_it_has_it(editor: Resident) -> None:
    _assert_set(_hint(_signed_in(editor).get("/")))  # not only Hjemmeside's own screens

    response = _signed_in(editor, hint="1").get(reverse("cms:overview"))
    assert response.status_code == 200 and _hint(response) is None

    _assert_set(_hint(_signed_in(editor, hint="0").get("/")))  # no other value counts


@pytest.mark.django_db
def test_a_superuser_qualifies(make_resident: Callable[..., Resident]) -> None:
    superuser = make_resident(email="su@gahk.dk", is_superuser=True, is_staff=True)
    _assert_set(_hint(_signed_in(superuser).get("/")))


@pytest.mark.django_db
def test_it_follows_real_roles_not_a_role_preview(make_resident: Callable[..., Resident]) -> None:
    admin = make_resident(email="admin@gahk.dk", roles=("administrator",))
    client = _signed_in(admin, hint="1")
    session = client.session
    session[PREVIEW_SESSION_KEY] = []  # the administrator looks at the site as a plain beboer
    session.save()

    response = client.get("/")
    assert response.status_code == 200 and _hint(response) is None  # kept: still an editor really

    resident = make_resident(email="beboer@gahk.dk")
    client = _signed_in(resident)
    session = client.session
    session[PREVIEW_SESSION_KEY] = ["pr"]  # a preview a beboer may not use, and it grants nothing
    session.save()
    assert _hint(client.get("/")) is None


@pytest.mark.django_db
def test_it_is_deleted_on_the_first_response_after_the_role_is_gone(editor: Resident) -> None:
    client = _signed_in(editor, hint="1")
    RoleAssignment.objects.filter(resident=editor).delete()

    _assert_deleted(_hint(client.get("/")))

    del client.cookies[EDITOR_HINT_COOKIE]  # the browser dropped it
    assert _hint(client.get("/")) is None


@pytest.mark.django_db
def test_it_is_deleted_at_logout(editor: Resident) -> None:
    response = _signed_in(editor, hint="1").post(reverse("logout"))

    assert response.status_code == 302
    _assert_deleted(_hint(response))


@pytest.mark.django_db
def test_a_browser_without_a_session_loses_a_leftover_one() -> None:
    client = Client()
    client.cookies[EDITOR_HINT_COOKIE] = "1"
    _assert_deleted(_hint(client.get(reverse("login"))))


@pytest.mark.django_db
def test_static_files_and_media_never_carry_it(editor: Resident) -> None:
    client = _signed_in(editor)
    for path in ("/static/nothing-here.css", "/media/cms/2026/11/nothing-here.jpg"):
        assert _hint(client.get(path)) is None
    client.cookies[EDITOR_HINT_COOKIE] = "1"
    RoleAssignment.objects.filter(resident=editor).delete()
    assert _hint(client.get("/media/cms/2026/11/nothing-here.jpg")) is None


def test_an_anonymous_request_leaves_the_session_and_the_user_alone() -> None:
    """No session cookie, no hint: nothing to decide, so neither the session (which would add
    Vary: Cookie) nor the user (a database read) is touched."""

    def never() -> AnonymousUser:
        raise AssertionError("the user was looked up")

    request = RequestFactory().get("/kalender/abc.ics")
    request.session = SessionStore()
    request.user = SimpleLazyObject(never)  # type: ignore[assignment]

    def view(request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    response = EditorHintCookie(view)(request)

    assert not response.cookies and not request.session.accessed


@pytest.mark.django_db
def test_in_development_it_is_host_only_and_not_secure(editor: Resident, settings: Any) -> None:  # noqa: ANN401
    settings.DEBUG = True
    settings.EDITOR_HINT_COOKIE_DOMAIN = None

    morsel = _hint(_signed_in(editor).get("/"))

    assert morsel is not None and morsel.value == "1"
    assert morsel["domain"] == "" and not morsel["secure"]


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/hjemmeside/", "/hjemmeside/sider/1/"])
def test_the_edit_links_gahk_dk_uses_exist(editor: Resident, path: str) -> None:
    from cms.models import Page

    Page.put_front_page(slug="velkommen", header="Velkommen")
    assert _signed_in(editor).get(path).status_code == 200
