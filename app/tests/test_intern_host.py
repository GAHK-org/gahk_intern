"""The intranet at the root of intern.gahk.dk, where it lived at gahk.dk/intern/ before.

What has to keep working across the move: old /intern/ and /nyintern/ addresses (stored notification
targets, Markdown links, bookmarks forwarded by gahk.dk).
"""

from collections.abc import Callable

import pytest
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
