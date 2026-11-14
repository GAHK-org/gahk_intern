"""Installing the intranet as the home-screen app "GAHK Intern".

It used to be installed from gahk.dk/intern/ as "Den Hurtige". An installed web app cannot follow its
site to another origin, so everyone installs it again from intern.gahk.dk.
"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client

from residents.models import Resident

pytestmark = pytest.mark.django_db

FEED_URL = "/den-hurtige/"


def _manifest() -> dict:
    return json.loads((Path(settings.BASE_DIR) / "static" / "manifest.json").read_text(encoding="utf-8"))


def test_the_app_is_called_gahk_intern() -> None:
    """It opens on Den Hurtige but is the whole intranet."""
    manifest = _manifest()
    assert manifest["name"] == manifest["short_name"] == "GAHK Intern"


def test_the_app_keeps_its_id() -> None:
    """A changed id makes every installed copy a different app, so renaming it leaves the id alone."""
    assert _manifest()["id"] == FEED_URL


def test_ios_gets_the_name_from_the_page(client: Client, make_resident: Callable[..., Resident]) -> None:
    """iOS names the home-screen icon after this meta tag, not after the manifest."""
    client.force_login(make_resident())
    html = client.get(FEED_URL).content.decode()
    assert '<meta name="apple-mobile-web-app-title" content="GAHK Intern">' in html


# --- /install ------------------------------------------------------------------------------------

INSTALL_URL = "/install"
STEPS = ("Føj til hjemmeskærm", "Installér app", "Når appen er installeret", "Fjern app")


def test_the_install_page_needs_no_login() -> None:
    """The old app on gahk.dk links here, and its session stayed behind on that origin."""
    response = Client().get(INSTALL_URL)
    assert response.status_code == 200
    html = response.content.decode()
    for step in STEPS:
        assert step in html
    # No menu and no resident search without a login.
    assert "app-no-nav" in html
    assert "Søg beboer" not in html


def test_the_install_page_links_the_manifest() -> None:
    """Browsers install the app described by the manifest of the page you are on."""
    html = Client().get(INSTALL_URL).content.decode()
    assert '<link rel="manifest" href="/static/manifest.json">' in html
    assert '<meta name="apple-mobile-web-app-title" content="GAHK Intern">' in html


def test_the_install_page_for_a_resident(client: Client, make_resident: Callable[..., Resident]) -> None:
    client.force_login(make_resident())
    response = client.get(INSTALL_URL)
    assert response.status_code == 200
    html = response.content.decode()
    for step in STEPS:
        assert step in html
    assert "app-no-nav" not in html
    assert "/notifikationer/indstillinger" in html


def test_the_kitchen_account_may_read_the_install_page(make_resident: Callable[..., Resident]) -> None:
    """The page is public; the kitchen gate guards residents' data, and the kitchen tablet may carry the
    old app itself."""
    from core.middleware import kitchen_may_open

    client = Client()
    client.force_login(make_resident(email="koekken@gahk.dk", is_kitchen=True))
    assert client.get(INSTALL_URL).status_code == 200
    assert not kitchen_may_open(INSTALL_URL + "x")
