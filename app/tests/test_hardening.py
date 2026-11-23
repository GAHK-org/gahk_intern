"""App-wide hardening: the production secret key, security headers, and the login/reset limits."""

from collections.abc import Callable
from runpy import run_path

import pytest
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import Client, RequestFactory

from config import settings as config_settings
from core.security_headers import SecurityHeaders
from residents import throttle
from residents.models import Resident

LOGIN = "/admin/login"
RESET = "/admin/password-reset"


# ---------------------------------------------------------------- settings
@pytest.mark.parametrize("key", [None, "", "dev-insecure-change-me"])
def test_production_refuses_to_start_without_its_own_secret_key(
    monkeypatch: pytest.MonkeyPatch, key: str | None
) -> None:
    monkeypatch.setenv("DJANGO_DEBUG", "0")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    if key is None:
        monkeypatch.delenv("DJANGO_SECRET_KEY", raising=False)
        # load_dotenv must not supply one from a developer's app/.env either.
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    else:
        monkeypatch.setenv("DJANGO_SECRET_KEY", key)

    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY"):
        run_path(config_settings.__file__)


def test_production_sends_hsts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DJANGO_DEBUG", "0")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("DJANGO_SECRET_KEY", "test-secret")
    configured = run_path(config_settings.__file__)

    assert configured["SECURE_HSTS_SECONDS"] >= 60 * 60 * 24 * 365
    assert configured["SECURE_HSTS_INCLUDE_SUBDOMAINS"] is True


# ---------------------------------------------------------------- headers
@pytest.mark.django_db
def test_every_page_gets_the_policy_headers() -> None:
    response = Client().get(LOGIN)

    csp = response.headers["Content-Security-Policy"]
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "frame-ancestors 'none'" in csp  # X-Frame-Options is DENY
    assert "camera=()" in response.headers["Permissions-Policy"]


@pytest.mark.parametrize(
    ("x_frame_options", "expected"),
    [("DENY", "frame-ancestors 'none'"), ("SAMEORIGIN", "frame-ancestors 'self'"), (None, None)],
)
def test_frame_ancestors_follows_x_frame_options(x_frame_options: str | None, expected: str | None) -> None:
    def view(request: object) -> HttpResponse:
        response = HttpResponse()
        if x_frame_options:
            response["X-Frame-Options"] = x_frame_options
        return response

    csp = SecurityHeaders(view)(RequestFactory().get("/"))["Content-Security-Policy"]

    if expected:
        assert expected in csp
    else:
        assert "frame-ancestors" not in csp  # @xframe_options_exempt: may be framed anywhere


def test_a_views_own_policy_is_kept() -> None:
    def view(request: object) -> HttpResponse:
        response = HttpResponse()
        response["Content-Security-Policy"] = "sandbox"
        return response

    assert SecurityHeaders(view)(RequestFactory().get("/"))["Content-Security-Policy"] == "sandbox"


# ---------------------------------------------------------------- login limit
def _fail(client: Client, email: str, times: int, ip: str = "203.0.113.7") -> None:
    for _ in range(times):
        client.post(LOGIN, {"username": email, "password": "forkert"}, REMOTE_ADDR=ip)


@pytest.mark.django_db
def test_too_many_wrong_passwords_refuse_even_the_right_one(make_resident: Callable[..., Resident]) -> None:
    make_resident(email="a@gahk.dk", password="rigtig")
    client = Client()
    _fail(client, "a@gahk.dk", throttle.LOGIN_FAILURES_PER_ADDRESS)

    response = client.post(LOGIN, {"username": "A@gahk.dk", "password": "rigtig"}, REMOTE_ADDR="203.0.113.7")

    assert response.status_code == 200  # the form again, not a redirect into the site
    assert "For mange forkerte forsøg" in response.content.decode()
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_the_limit_is_per_ip_so_nobody_can_lock_a_resident_out_from_elsewhere(
    make_resident: Callable[..., Resident],
) -> None:
    make_resident(email="a@gahk.dk", password="rigtig")
    _fail(Client(), "a@gahk.dk", throttle.LOGIN_FAILURES_PER_ADDRESS, ip="198.51.100.1")

    client = Client()
    response = client.post(LOGIN, {"username": "a@gahk.dk", "password": "rigtig"}, REMOTE_ADDR="203.0.113.7")

    assert response.status_code == 302
    assert "_auth_user_id" in client.session


@pytest.mark.django_db
def test_the_client_ip_comes_from_the_proxy(make_resident: Callable[..., Resident]) -> None:
    """Behind Traefik every request has the proxy's REMOTE_ADDR; the client is the last X-Forwarded-For hop."""
    make_resident(email="a@gahk.dk", password="rigtig")
    for _ in range(throttle.LOGIN_FAILURES_PER_ADDRESS):
        Client().post(
            LOGIN,
            {"username": "a@gahk.dk", "password": "forkert"},
            REMOTE_ADDR="10.0.0.2",
            HTTP_X_FORWARDED_FOR="198.51.100.1",
        )

    client = Client()
    response = client.post(
        LOGIN,
        {"username": "a@gahk.dk", "password": "rigtig"},
        REMOTE_ADDR="10.0.0.2",
        HTTP_X_FORWARDED_FOR="203.0.113.7",
    )

    assert response.status_code == 302


@pytest.mark.django_db
def test_a_successful_login_clears_the_count(make_resident: Callable[..., Resident]) -> None:
    make_resident(email="a@gahk.dk", password="rigtig")
    client = Client()
    _fail(client, "a@gahk.dk", throttle.LOGIN_FAILURES_PER_ADDRESS - 1)
    client.post(LOGIN, {"username": "a@gahk.dk", "password": "rigtig"}, REMOTE_ADDR="203.0.113.7")
    client.logout()

    _fail(client, "a@gahk.dk", throttle.LOGIN_FAILURES_PER_ADDRESS - 1)
    response = client.post(LOGIN, {"username": "a@gahk.dk", "password": "rigtig"}, REMOTE_ADDR="203.0.113.7")

    assert response.status_code == 302


@pytest.mark.django_db
def test_one_ip_guessing_across_many_addresses_is_stopped(make_resident: Callable[..., Resident]) -> None:
    make_resident(email="target@gahk.dk", password="rigtig")
    client = Client()
    for n in range(throttle.LOGIN_FAILURES_PER_IP):
        _fail(client, f"guess{n}@gahk.dk", 1)

    response = client.post(
        LOGIN, {"username": "target@gahk.dk", "password": "rigtig"}, REMOTE_ADDR="203.0.113.7"
    )

    assert "For mange forkerte forsøg" in response.content.decode()


@pytest.mark.django_db
def test_django_admins_login_has_the_same_limit(make_resident: Callable[..., Resident]) -> None:
    resident = make_resident(email="admin@gahk.dk", password="rigtig")
    Resident.objects.filter(pk=resident.pk).update(is_staff=True, is_superuser=True)
    client = Client()
    _fail(client, "admin@gahk.dk", throttle.LOGIN_FAILURES_PER_ADDRESS)

    client.post(
        "/django-admin/login/",
        {"username": "admin@gahk.dk", "password": "rigtig", "next": "/django-admin/"},
        REMOTE_ADDR="203.0.113.7",
    )

    assert "_auth_user_id" not in client.session


# ---------------------------------------------------------------- password reset limit
@pytest.mark.django_db
def test_password_reset_mails_an_address_only_a_few_times_an_hour(
    make_resident: Callable[..., Resident],
) -> None:
    make_resident(email="a@gahk.dk", password="rigtig")
    client = Client()
    for _ in range(throttle.RESETS_PER_ADDRESS + 2):
        response = client.post(RESET, {"email": "a@gahk.dk"}, REMOTE_ADDR="203.0.113.7")
        assert response.status_code == 302  # the same "done" page every time

    assert len(mail.outbox) == throttle.RESETS_PER_ADDRESS


@pytest.mark.django_db
def test_password_reset_is_limited_per_ip(make_resident: Callable[..., Resident]) -> None:
    for n in range(throttle.RESETS_PER_IP + 1):
        make_resident(email=f"r{n}@gahk.dk", password="rigtig")
    client = Client()
    for n in range(throttle.RESETS_PER_IP + 1):
        client.post(RESET, {"email": f"r{n}@gahk.dk"}, REMOTE_ADDR="203.0.113.7")

    assert len(mail.outbox) == throttle.RESETS_PER_IP


# ---------------------------------------------------------------- names inside inline handlers
@pytest.mark.django_db
def test_a_name_in_a_confirm_handler_cannot_end_the_string(make_resident: Callable[..., Resident]) -> None:
    """HTML escaping is undone before an onsubmit runs, so a name with a quote needs escapejs too."""
    from oelkaelder.models import Shopper

    oek = make_resident(email="oek@gahk.dk", roles=("oelkaelder",))
    shopper = make_resident(email="s@gahk.dk")
    Resident.objects.filter(pk=shopper.pk).update(last_name="x');alert(1);('")
    Shopper.objects.create(resident=Resident.objects.get(pk=shopper.pk))
    client = Client()
    client.force_login(oek)

    body = client.get("/oelkaelder/admin").content.decode()

    handler = body[body.index("confirm('Deaktivér") :][:120]
    assert "Test x\\u0027)" in handler
    assert "&#x27;);alert(1)" not in handler
