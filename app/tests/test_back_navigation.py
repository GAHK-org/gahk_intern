"""What a page brought back by the Back button needs to tell it is out of date (core.back_navigation):
its own request time on the page, and the time of the last write in a cookie."""

import re
from collections.abc import Callable

from django.conf import settings
from django.test import Client
from django.urls import reverse

from residents.models import Resident

COOKIE = settings.LAST_WRITE_COOKIE_NAME


def _rendered(html: str) -> int:
    match = re.search(r'<meta name="gahk-rendered" content="(\d+)">', html)
    assert match, "no gahk-rendered meta"
    return int(match.group(1))


def test_a_page_carries_the_time_it_was_made(client: Client, make_resident: Callable[..., Resident]) -> None:
    client.force_login(make_resident(email="a@gahk.dk"))

    assert _rendered(client.get(reverse("dashboard")).content.decode()) > 0


def test_a_successful_write_sets_the_cookie_after_the_page_it_changes(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The comparison the browser makes: a page made BEFORE a write is older than the cookie."""
    client.force_login(make_resident(email="a@gahk.dk"))
    before = _rendered(client.get("/den-hurtige/").content.decode())

    response = client.post("/den-hurtige/opret", {"content": "Kaffe", "duration": "60"})

    assert int(response.cookies[COOKIE].value) >= before
    assert response.cookies[COOKIE]["samesite"] == "Lax"
    assert not response.cookies[COOKIE]["httponly"]  # the page's script reads it
    after = _rendered(client.get("/den-hurtige/").content.decode())
    assert after >= int(response.cookies[COOKIE].value)


def test_reading_is_not_a_write(client: Client, make_resident: Callable[..., Resident]) -> None:
    client.force_login(make_resident(email="a@gahk.dk"))

    assert COOKIE not in client.get(reverse("dashboard")).cookies


def test_a_refused_write_is_not_a_write(client: Client, make_resident: Callable[..., Resident]) -> None:
    """Nothing changed, so no page is out of date."""
    client.force_login(make_resident(email="a@gahk.dk"))

    response = client.post("/den-hurtige/opret", {"content": "Kaffe"}, headers={"HX-Request": "true"})

    assert response.status_code == 422
    assert COOKIE not in response.cookies


def test_requests_the_browser_sends_on_its_own_are_not_writes(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The opslagstavle preview is posted as you type; counting it would reload the page you go back
    to for nothing."""
    client.force_login(make_resident(email="a@gahk.dk"))

    response = client.post(reverse("opslagstavle:preview"), {"body": "Hej"})

    assert response.status_code == 200
    assert COOKIE not in response.cookies
