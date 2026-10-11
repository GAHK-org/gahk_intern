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
    with mock.patch("core.views.connection.cursor", side_effect=RuntimeError("down")):
        response = Client().get("/healthz")
    assert response.status_code == 503
