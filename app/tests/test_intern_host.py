"""The intranet at the root of intern.gahk.dk, where it lived at gahk.dk/intern/ before.

What has to keep working across the move: old /intern/ and /nyintern/ addresses (stored notification
targets, Markdown links, bookmarks forwarded by gahk.dk), the session (host-only now, under new
cookie names, with the old ".gahk.dk" pair deleted), and stored data that spelled the old address.
"""

import importlib
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification
from oelkaelder.models import Warning
from residents.models import Resident

pytestmark = pytest.mark.django_db


# --- the old prefixes ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/intern/", "/"),
        ("/intern/den-hurtige/?traad=4", "/den-hurtige/?traad=4"),
        ("/intern/alumneliste/?q=Hansen&side=2", "/alumneliste/?q=Hansen&side=2"),
        ("/intern/notifikationer/aabn/abc/", "/notifikationer/aabn/abc/"),
        ("/nyintern/ak/", "/ak/"),
        ("/nyintern/stamtree/?x=1", "/stamtree/?x=1"),
        ("/intern", "/"),
        ("/nyintern", "/"),
        ("/intern?next=/ak/", "/?next=/ak/"),
    ],
)
def test_an_old_address_lands_on_the_same_page_at_the_root(old: str, new: str) -> None:
    response = Client().get(old)
    assert response.status_code == 308
    assert response["Location"] == new


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/intern/a%3Fb", "/a%3Fb"),
        ("/intern/a%23b?x=1", "/a%23b?x=1"),
        ("/intern/a%25b", "/a%25b"),
        ("/intern/a%20b/", "/a%20b/"),
        ("/intern/v%C3%A6relse/", "/v%C3%A6relse/"),
    ],
)
def test_an_escaped_character_in_the_old_path_stays_escaped(old: str, new: str) -> None:
    """The view gets the path decoded: an escaped "?" must not become a query, nor "#" a fragment,
    nor "%25" a stray "%". gahk.dk forwards such a path escaped as it came."""
    response = Client().get(old)
    assert response.status_code == 308
    assert response["Location"] == new


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/intern/fotoalbum/importer-zip/system", "/fotoalbum/importer-zip/system"),
        ("/intern/den-hurtige/?traad=4", "/den-hurtige/?traad=4"),
        ("/nyintern/ak/?x=1", "/ak/?x=1"),
        ("/intern", "/"),
    ],
)
def test_any_method_to_an_old_address_keeps_its_method(method: str, old: str, new: str) -> None:
    """308, not 301: a script or a restored form aimed at the old path must not turn into a GET.

    With CSRF checks on, as in production: the test client skips them by default, which hid that
    CsrfViewMiddleware answered every one of these with a 403 before the redirect was reached. gahk.dk
    sends old links to https://intern.gahk.dk/intern/<rest>, so a form re-sent by the browser
    arrives here with an Origin of "null" and no way to pass the check.
    """
    client = Client(enforce_csrf_checks=True)
    response = getattr(client, method)(old, {"x": "1"} if method == "post" else "", HTTP_ORIGIN="null")
    assert response.status_code == 308
    assert response["Location"] == new


def test_the_landing_view_still_checks_csrf(make_resident: Callable[..., Resident]) -> None:
    """The exemption is the redirect's alone: the POST it passes on meets the usual check."""
    client = Client(enforce_csrf_checks=True)
    client.force_login(make_resident(email="beboer@gahk.dk"))
    response = client.post("/intern/notifikationer/", follow=True)
    assert response.redirect_chain == [("/notifikationer/", 308)]
    assert response.status_code == 403


@pytest.mark.parametrize(
    "old", ["/intern//evil.example/x", "/intern/\\evil.example", "/nyintern///evil.example"]
)
def test_an_old_address_cannot_become_an_open_redirect(old: str) -> None:
    location = Client().get(old)["Location"]
    assert location.startswith("/") and not location.startswith(("//", "/\\"))


def test_the_redirect_is_followed_to_the_real_page(make_resident: Callable[..., Resident]) -> None:
    c = Client()
    c.force_login(make_resident(email="beboer@gahk.dk"))
    response = c.get("/intern/alumneliste/", follow=True)
    assert response.status_code == 200
    assert response.redirect_chain == [(reverse("directory"), 308)]


def test_the_front_page_button_leaves_for_the_public_site(
    make_resident: Callable[..., Resident], settings: object
) -> None:
    """The dashboard is at "/" here; "Til GAHK-forsiden" means gahk.dk, the public site's own host."""
    settings.PUBLIC_SITE_URL = "https://gahk.dk"  # type: ignore[attr-defined]
    c = Client()
    c.force_login(make_resident(email="beboer@gahk.dk"))
    html = c.get(reverse("dashboard")).content.decode()
    assert '<a href="https://gahk.dk/" class="btn-top" title="Til GAHK-forsiden">' in html


# --- cookies -------------------------------------------------------------------------------------


def test_logging_in_sets_the_renamed_host_only_cookies(make_resident: Callable[..., Resident]) -> None:
    make_resident(email="beboer@gahk.dk", password="hemmelig")
    c = Client()
    c.get(reverse("login"))
    response = c.post(reverse("login"), {"username": "beboer@gahk.dk", "password": "hemmelig"})

    assert response.status_code == 302
    session = response.cookies[settings.SESSION_COOKIE_NAME]
    assert session.value and not session["domain"]
    csrf = settings.CSRF_COOKIE_NAME
    assert csrf in c.cookies and not c.cookies[csrf]["domain"]
    assert "sessionid" not in response.cookies and "csrftoken" not in response.cookies


def _deletions(response: object, name: str) -> dict[str, dict[str, str]]:
    """The Set-Cookie deletions for `name`, keyed by Domain ("" = host-only)."""
    morsels = response.cookies.values()  # type: ignore[attr-defined]
    return {m["domain"]: dict(m) for m in morsels if m.key == name and m["max-age"] == 0}


def test_the_old_cookies_are_deleted_from_both_domains() -> None:
    c = Client()
    c.cookies["sessionid"] = "from-gahk.dk"
    c.cookies["csrftoken"] = "from-gahk.dk"
    response = c.get(reverse("login"))

    assert response.status_code == 200
    for name in ("sessionid", "csrftoken"):
        deletions = _deletions(response, name)
        assert set(deletions) == {".gahk.dk", ""}, name
        assert all(
            d["path"] == "/" and d["expires"].startswith("Thu, 01 Jan 1970") for d in deletions.values()
        )
    # Each deletion is a header of its own, under the cookie's real name.
    headers = [m.output(header="Set-Cookie:") for m in response.cookies.values()]
    assert any(h.startswith("Set-Cookie: sessionid=") and "Domain=.gahk.dk" in h for h in headers)


def test_a_redirect_deletes_the_old_cookies_too() -> None:
    """The first request after the move is often gahk.dk's forwarded /intern/… link."""
    c = Client()
    c.cookies["sessionid"] = "from-gahk.dk"
    response = c.get("/intern/den-hurtige/")
    assert response.status_code == 308
    assert set(_deletions(response, "sessionid")) == {".gahk.dk", ""}


def test_a_request_without_the_old_cookies_deletes_nothing() -> None:
    response = Client().get(reverse("login"))
    assert not _deletions(response, "sessionid") and not _deletions(response, "csrftoken")


def test_an_old_session_cookie_does_not_log_anyone_in(make_resident: Callable[..., Resident]) -> None:
    """The old cookie's value is a real session key on this database. Under the new name nothing
    reads it, so it is the login page, not the dashboard — and the cookie is deleted."""
    logged_in = Client()
    logged_in.force_login(make_resident(email="beboer@gahk.dk"))
    key = logged_in.cookies[settings.SESSION_COOKIE_NAME].value

    c = Client()
    c.cookies["sessionid"] = key
    response = c.get(reverse("dashboard"))
    assert response.status_code == 302 and response["Location"].startswith(reverse("login"))
    assert set(_deletions(response, "sessionid")) == {".gahk.dk", ""}


def test_the_frontend_reads_the_renamed_csrf_cookie() -> None:
    """fetch() and htmx send the token from the cookie (frontend/src/htmx-csrf.ts). Reading the old
    name would pick up nothing — or a stale ".gahk.dk" token — and every POST would fail CSRF."""
    source = (Path(settings.BASE_DIR).parent / "frontend" / "src" / "htmx-csrf.ts").read_text()
    match = re.search(r"const CSRF_COOKIES = \[([^\]]+)\]", source)
    assert match
    names = re.findall(r'"([^"]+)"', match.group(1))
    # Production's name first: the __Host- one cannot be planted from gahk.dk, the bare one can.
    assert names == ["__Host-intern_csrftoken", "intern_csrftoken"]
    assert settings.CSRF_COOKIE_NAME in names


# --- stored data ---------------------------------------------------------------------------------


def _migration(app: str, name: str) -> object:
    return importlib.import_module(f"{app}.migrations.{name}")


def test_stored_notification_targets_lose_the_intern_prefix(make_resident: Callable[..., Resident]) -> None:
    resident = make_resident(email="beboer@gahk.dk")
    urls = ["/intern/den-hurtige/?traad=4", "/intern/", "/notifikationer/", "https://example.org/intern/x"]
    for url in urls:
        Notification.objects.create(recipient=resident, title=url, url=url, expires_at=timezone.now())
    migration = _migration("notifications", "0003_urls_without_intern_prefix")

    migration.strip_intern_prefix(apps, None)  # type: ignore[attr-defined]
    stored = dict(Notification.objects.values_list("title", "url"))
    assert [stored[u] for u in urls] == [
        "/den-hurtige/?traad=4",
        "/",
        "/notifikationer/",
        "https://example.org/intern/x",
    ]

    migration.add_intern_prefix(apps, None)  # type: ignore[attr-defined]
    stored = dict(Notification.objects.values_list("title", "url"))
    assert [stored[u] for u in urls] == [
        "/intern/den-hurtige/?traad=4",
        "/intern/",
        "/intern/notifikationer/",
        "https://example.org/intern/x",
    ]


def test_a_session_from_before_the_move_does_not_log_anyone_in(
    make_resident: Callable[..., Resident],
) -> None:
    """Renaming the cookie does not end a session: its key would still work under the new name. The
    old ".gahk.dk" cookie reaches gahk.dk's webhost, so the deploy ends every session there was."""
    before_the_move = Client()
    before_the_move.force_login(make_resident(email="beboer@gahk.dk"))
    key = before_the_move.cookies[settings.SESSION_COOKIE_NAME].value

    _migration("core", "0017_end_sessions_from_before_intern_host").end_every_session(apps, None)  # type: ignore[attr-defined]

    c = Client()
    c.cookies[settings.SESSION_COOKIE_NAME] = key
    response = c.get(reverse("dashboard"))
    assert response.status_code == 302 and response["Location"].startswith(reverse("login"))


def test_the_debt_warnings_name_the_intranet_host() -> None:
    Warning.objects.all().delete()
    warning = Warning.objects.create(
        threshold_ore=0,
        message="Saldoen ses på Gahk.dk/intern. Eller www.GAHK.dk/intern/oelkaelder. Ikke gahk.dk/internat.",
    )
    migration = _migration("oelkaelder", "0007_warning_messages_name_intern_host")

    migration.to_intern_host(apps, None)  # type: ignore[attr-defined]
    warning.refresh_from_db()
    assert warning.message == (
        "Saldoen ses på intern.gahk.dk. Eller intern.gahk.dk/oelkaelder. Ikke gahk.dk/internat."
    )

    migration.to_intern_path(apps, None)  # type: ignore[attr-defined]
    warning.refresh_from_db()
    assert warning.message == (
        "Saldoen ses på gahk.dk/intern. Eller gahk.dk/intern/oelkaelder. Ikke gahk.dk/internat."
    )
