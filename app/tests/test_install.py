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
