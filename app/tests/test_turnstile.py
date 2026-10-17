"""Turnstile on the public application forms is on only with BOTH keys (admissions.turnstile_enabled).

With only the secret set, the form rendered no widget (that needs the site key), no token was ever
posted, and every application was rejected as a failed captcha.
"""

import pytest
from django.test import Client, override_settings

from admissions.checks import check_turnstile_keys
from admissions.models import Application

APPLICATION = {
    "full_name": "Ny",
    "email": "ny@x.dk",
    "gender": "male",
    "age": "22",
    "study_year": "1",
    "year_left": "3",
    "university": "DTU",
    "field_of_study": "Fysik",
    "heard_about_us": "plakat",
    "motivation": "Gerne",
}


@pytest.mark.django_db
@override_settings(TURNSTILE_SITE_KEY="", TURNSTILE_SECRET_KEY="secret")
def test_half_configured_turnstile_is_off_not_a_dead_form() -> None:
    assert "cf-turnstile" not in Client().get("/optagelse/ansoeg").content.decode()
    response = Client().post("/optagelse/send_rundvisning", APPLICATION)
    assert response.status_code == 302
    assert Application.objects.count() == 1
    assert [w.id for w in check_turnstile_keys(None)] == ["admissions.W001"]


@override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET_KEY="")
def test_site_key_alone_is_flagged_and_renders_no_widget() -> None:
    assert "cf-turnstile" not in Client().get("/optagelse/ansoeg").content.decode()
    assert [w.id for w in check_turnstile_keys(None)] == ["admissions.W001"]


@pytest.mark.django_db
@override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET_KEY="secret")
def test_fully_configured_turnstile_renders_and_enforces() -> None:
    assert 'data-sitekey="site"' in Client().get("/optagelse/ansoeg").content.decode()
    response = Client().post("/optagelse/send_rundvisning", APPLICATION)  # no token posted
    assert response.status_code == 200
    assert Application.objects.count() == 0
    assert check_turnstile_keys(None) == []
