"""The intranet at the root of intern.gahk.dk, where it lived at gahk.dk/intern/ before.

What has to keep working across the move: old /intern/ and /nyintern/ addresses (stored notification
targets, Markdown links, bookmarks forwarded by gahk.dk) and the session (host-only now, under new
cookie names, with the old ".gahk.dk" pair deleted).
"""

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

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


def test_a_post_to_an_old_address_stays_a_post() -> None:
    """308, not 301: a script or a restored form aimed at the old path must not turn into a GET."""
    response = Client().post("/intern/fotoalbum/importer-zip/system", {"x": "1"})
    assert response.status_code == 308
    assert response["Location"] == "/fotoalbum/importer-zip/system"


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
    session = response.cookies["intern_sessionid"]
    assert session.value and not session["domain"]
    assert "intern_csrftoken" in c.cookies and not c.cookies["intern_csrftoken"]["domain"]
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
    key = logged_in.cookies["intern_sessionid"].value

    c = Client()
    c.cookies["sessionid"] = key
    response = c.get(reverse("dashboard"))
    assert response.status_code == 302 and response["Location"].startswith(reverse("login"))
    assert set(_deletions(response, "sessionid")) == {".gahk.dk", ""}


def test_the_frontend_reads_the_renamed_csrf_cookie() -> None:
    """fetch() and htmx send the token from the cookie (frontend/src/htmx-csrf.ts). Reading the old
    name would pick up nothing — or a stale ".gahk.dk" token — and every POST would fail CSRF."""
    source = (Path(settings.BASE_DIR).parent / "frontend" / "src" / "htmx-csrf.ts").read_text()
    match = re.search(r'const CSRF_COOKIE = "([^"]+)"', source)
    assert match and match.group(1) == settings.CSRF_COOKIE_NAME
