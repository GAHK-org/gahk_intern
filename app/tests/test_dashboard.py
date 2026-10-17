"""The internal dashboard (residents.views.dashboard, /intern/)."""

from collections.abc import Callable

import pytest
from django.test import Client

from residents.models import Resident, Role

pytestmark = pytest.mark.django_db


def test_dashboard_shows_role_names_not_codes(make_resident: Callable[..., Resident]) -> None:
    """ "Dine roller" listed the raw codes (`oelkaelder`); residents should read "Ølkælderen"."""
    client = Client()
    client.force_login(make_resident(email="oel@gahk.dk", roles=[Role.OELKAELDER]))

    html = client.get("/intern/").content.decode()

    assert '<span class="chip">Ølkælderen</span>' in html
    assert '<span class="chip">oelkaelder</span>' not in html
    assert "Ølkælder-admin" in html, "the role-gated links still key off the raw code"
