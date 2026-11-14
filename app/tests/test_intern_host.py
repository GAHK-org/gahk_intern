"""The intranet at the root of intern.gahk.dk, where it lived at gahk.dk/intern/ before.

What has to keep working across the move: the session (host-only now, under new cookie names, with
the old ".gahk.dk" pair deleted), and stored data that spelled the old address. The old /intern/ and
/nyintern/ prefixes mean nothing here: gahk.dk forwards its own old /intern/<rest> links straight to
/<rest> on this host, so a prefixed path is a 404 like any unknown one.
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
    "old",
    [
        "/intern/",
        "/intern/den-hurtige/?traad=4",
        "/intern/alumneliste/",
        "/nyintern/ak/",
        "/intern",
        "/nyintern",
        "/intern?next=/ak/",
    ],
)
def test_an_old_prefix_is_not_found(old: str) -> None:
    assert Client().get(old).status_code == 404


def test_an_old_prefix_is_not_found_for_a_resident_or_a_post(
    make_resident: Callable[..., Resident],
) -> None:
    c = Client()
    c.force_login(make_resident(email="beboer@gahk.dk"))
    assert c.get("/intern/alumneliste/").status_code == 404
    assert Client().post("/intern/fotoalbum/importer-zip/system", {"x": "1"}).status_code == 404


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
    """The first request after the move is often gahk.dk's forwarded link, which lands on a page that
    asks for a login first."""
    c = Client()
    c.cookies["sessionid"] = "from-gahk.dk"
    response = c.get("/den-hurtige/")
    assert response.status_code == 302
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
