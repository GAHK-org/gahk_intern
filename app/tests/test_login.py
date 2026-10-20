"""The login form on phones, the log of why a login failed, and stale forms (residents.auth)."""

import logging
from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse

from residents.models import Resident

LOGIN = "/intern/admin/login"


@pytest.mark.django_db
def test_the_email_field_is_an_email_field_phones_leave_alone() -> None:
    body = Client().get(LOGIN).content.decode()
    field = body[body.index('name="username"') - 200 : body.index('name="username"') + 300]

    assert 'type="email"' in field
    assert 'autocorrect="off"' in field
    assert 'spellcheck="false"' in field
    assert 'autocapitalize="none"' in field
    assert "novalidate" in body  # the browser's own check must not block an address the server accepts
    assert "event.persisted" in body  # restored from the back-forward cache: reload
    assert "visibilitychange" in body  # a PWA resumed after minutes in the background: reload


@pytest.mark.django_db
def test_a_login_for_an_unknown_address_logs_the_address_as_received(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="residents.auth"):
        Client().post(LOGIN, {"username": "ana.løfberg@gahk.dk", "password": "whatever"})

    assert "no account for 'ana.l\\xf8fberg@gahk.dk'" in caplog.text  # the ø shows as \xf8
    assert "whatever" not in caplog.text


@pytest.mark.django_db
def test_a_wrong_password_logs_the_account_not_the_address(
    make_resident: Callable[..., Resident], caplog: pytest.LogCaptureFixture
) -> None:
    resident = make_resident(email="known@gahk.dk", password="rigtig")
    with caplog.at_level(logging.WARNING, logger="residents.auth"):
        response = Client().post(LOGIN, {"username": "Known@gahk.dk", "password": "forkert"})

    assert response.status_code == 200
    assert f"Login failed for account {resident.pk}: wrong password" in caplog.text
    assert "known@gahk.dk" not in caplog.text.lower()
    assert "forkert" not in caplog.text


@pytest.mark.django_db
def test_an_inactive_account_is_logged_as_such(
    make_resident: Callable[..., Resident], caplog: pytest.LogCaptureFixture
) -> None:
    resident = make_resident(email="gone@gahk.dk", password="rigtig")
    Resident.objects.filter(pk=resident.pk).update(is_active=False)
    with caplog.at_level(logging.WARNING, logger="residents.auth"):
        Client().post(LOGIN, {"username": "gone@gahk.dk", "password": "rigtig"})

    assert f"Login failed for account {resident.pk}: account inactive" in caplog.text


@pytest.mark.django_db
def test_a_stale_login_form_goes_back_to_a_fresh_one(caplog: pytest.LogCaptureFixture) -> None:
    c = Client(enforce_csrf_checks=True)
    with caplog.at_level(logging.WARNING, logger="residents.auth"):
        response = c.post(f"{LOGIN}?next=/intern/den-hurtige/", {"username": "a@gahk.dk", "password": "x"})

    assert response.status_code == 302
    assert response["Location"] == f"{LOGIN}?next=%2Fintern%2Fden-hurtige%2F"
    assert "CSRF refused POST /intern/admin/login" in caplog.text
    assert "Siden var udløbet" in c.get(response["Location"]).content.decode()


@pytest.mark.django_db
def test_another_stale_form_explains_itself_instead_of_a_bare_403(
    make_resident: Callable[..., Resident],
) -> None:
    c = Client(enforce_csrf_checks=True)
    c.force_login(make_resident(email="stale@gahk.dk"))

    response = c.post(reverse("notifications:mark_all_seen"))

    assert response.status_code == 403
    assert "Siden var udløbet" in response.content.decode()
