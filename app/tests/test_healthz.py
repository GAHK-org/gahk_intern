"""The container healthcheck endpoint: anonymous, cheap, and honest about the database."""

from unittest import mock

import pytest
from django.test import Client


@pytest.mark.django_db
def test_healthz_ok_without_login() -> None:
    response = Client().get("/healthz")
    assert response.status_code == 200
    assert response.content == b"ok"


@pytest.mark.django_db
def test_healthz_503_when_db_is_down() -> None:
    with mock.patch("core.health.connection.cursor", side_effect=RuntimeError("down")):
        response = Client().get("/healthz")
    assert response.status_code == 503


@pytest.mark.django_db
def test_healthz_answers_whatever_the_host_header() -> None:
    """A checker inside the container may send Host: localhost or an IP that ALLOWED_HOSTS rejects."""
    response = Client().get("/healthz", HTTP_HOST="not-an-allowed-host.example")
    assert response.status_code == 200


@pytest.mark.django_db
def test_other_paths_still_check_the_host() -> None:
    response = Client().get("/", HTTP_HOST="not-an-allowed-host.example")
    assert response.status_code == 400
