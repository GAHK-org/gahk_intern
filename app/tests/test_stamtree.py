"""Stamtræ (F-011): fylgje cycles must neither hide residents nor be creatable."""

from collections.abc import Callable

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from residents.models import Resident


@pytest.mark.django_db
def test_residents_in_a_fylgje_cycle_still_appear(make_resident: Callable) -> None:
    """A→B→A has no root, so the tree walk from the roots never reached them (or their protégés)."""
    a = make_resident(email="ring-a@gahk.dk", first_name="Ring", last_name="Alfa")
    b = make_resident(email="ring-b@gahk.dk", first_name="Ring", last_name="Beta", sponsor=a)
    make_resident(email="ring-c@gahk.dk", first_name="Ring", last_name="Barn", sponsor=b)
    Resident.objects.filter(pk=a.pk).update(sponsor=b)  # raw write: bypasses validation, like old data
    selfie = make_resident(email="ring-s@gahk.dk", first_name="Selv", last_name="Fadder")
    Resident.objects.filter(pk=selfie.pk).update(sponsor=selfie)
    c = Client()
    c.force_login(make_resident(email="ring-v@gahk.dk"))

    html = c.get("/intern/stamtree/").content.decode()
    for name in ("Ring Alfa", "Ring Beta", "Ring Barn", "Selv Fadder"):
        assert name in html


@pytest.mark.django_db
def test_model_clean_rejects_a_fylgje_cycle(make_resident: Callable) -> None:
    """On the model, so Django admin enforces it too — not just a self-reference but any ring."""
    a = make_resident(email="cyc-a@gahk.dk")
    b = make_resident(email="cyc-b@gahk.dk", sponsor=a)
    c = make_resident(email="cyc-c@gahk.dk", sponsor=b)
    a.sponsor = c  # a → c → b → a
    with pytest.raises(ValidationError) as exc:
        a.full_clean()
    assert "sponsor" in exc.value.message_dict
    a.sponsor = a
    with pytest.raises(ValidationError):
        a.full_clean()
    a.sponsor = make_resident(email="cyc-d@gahk.dk")  # an ordinary fylgje is fine
    a.full_clean()


@pytest.mark.django_db
def test_edit_form_rejects_a_fylgje_cycle(make_resident: Callable) -> None:
    a = make_resident(email="form-a@gahk.dk", first_name="A", last_name="A")
    b = make_resident(email="form-b@gahk.dk", first_name="B", last_name="B", sponsor=a)
    ind = Client()
    ind.force_login(make_resident(email="form-ind@gahk.dk", roles=("indstilling",)))
    resp = ind.post(
        reverse("edit_resident", args=[a.id]),
        {"first_name": "A", "last_name": "A", "email": "form-a@gahk.dk", "sponsor": str(b.id)},
    )
    assert resp.status_code == 200  # re-rendered with the error
    assert "gå i ring" in resp.content.decode()
    a.refresh_from_db()
    assert a.sponsor_id is None
