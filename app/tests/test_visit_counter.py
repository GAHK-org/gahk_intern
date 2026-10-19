"""The front-page visit counter: which IP it counts (core.net) and the key it hashes with."""

import hashlib
import hmac

import pytest
from django.conf import settings
from django.test import Client, RequestFactory

from core.net import client_ip
from stats.models import VisitTally


def test_client_ip_trusts_only_the_last_forwarded_hop() -> None:
    rf = RequestFactory()
    spoofed = rf.get("/", HTTP_X_FORWARDED_FOR="1.2.3.4, 198.51.100.7", REMOTE_ADDR="10.0.0.2")
    assert client_ip(spoofed) == "198.51.100.7"  # what Traefik appended, not what the client claimed
    assert client_ip(rf.get("/", REMOTE_ADDR="10.0.0.2")) == "10.0.0.2"


@pytest.mark.django_db
def test_a_spoofed_first_hop_does_not_count_as_a_new_visitor() -> None:
    c = Client()
    c.get("/", HTTP_X_FORWARDED_FOR="1.1.1.1, 198.51.100.7")
    c.get("/", HTTP_X_FORWARDED_FOR="2.2.2.2, 198.51.100.7")  # same real client, new claimed IP
    assert VisitTally.objects.count() == 1
    expected = hmac.new(settings.VISIT_COUNTER_HMAC_KEY.encode(), b"198.51.100.7", hashlib.sha256).hexdigest()
    assert VisitTally.objects.get().ip_hash == expected


def test_hmac_key_fallback_is_derived_from_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset, the key used to be the public "dev-hmac-key", which makes every stored IPv4 hash
    reversible by brute force. Now it is derived from SECRET_KEY, so it is secret whenever that is."""
    import importlib

    import dotenv

    import config.settings as settings_module

    # The reload re-runs load_dotenv: a developer's app/.env must not supply the key under test.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
    monkeypatch.delenv("VISIT_COUNTER_HMAC_KEY", raising=False)
    monkeypatch.setenv("DJANGO_SECRET_KEY", "s3cret-one")
    one = importlib.reload(settings_module).VISIT_COUNTER_HMAC_KEY
    monkeypatch.setenv("DJANGO_SECRET_KEY", "s3cret-two")
    two = importlib.reload(settings_module).VISIT_COUNTER_HMAC_KEY
    monkeypatch.setenv("VISIT_COUNTER_HMAC_KEY", "explicit")
    explicit = importlib.reload(settings_module).VISIT_COUNTER_HMAC_KEY
    monkeypatch.undo()
    importlib.reload(settings_module)

    assert one not in {"dev-hmac-key", "s3cret-one"}
    assert one != two
    assert explicit == "explicit"
