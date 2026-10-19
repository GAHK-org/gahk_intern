"""Residents can log their own AK labour: positive krydser + required description."""

from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse

from ak.models import AkEntry


@pytest.mark.django_db
def test_resident_adds_own_krydser_with_description(make_resident: Callable) -> None:
    r = make_resident(email="r@gahk.dk")
    c = Client()
    c.force_login(r)
    c.post(reverse("ak:add_self"), {"krydser": "3", "reason": "Ryddede op i baren"})

    entry = AkEntry.objects.get(resident=r)
    assert entry.delta == 3
    assert entry.kind == AkEntry.Kind.LABOUR
    assert entry.reason == "Ryddede op i baren"
    assert entry.created_by_id == r.id  # self-reported (audit)
    assert AkEntry.balance_for(r) == 3


@pytest.mark.django_db
def test_krydser_must_be_positive_and_reason_required(make_resident: Callable) -> None:
    r = make_resident(email="r2@gahk.dk")
    c = Client()
    c.force_login(r)

    c.post(reverse("ak:add_self"), {"krydser": "0", "reason": "noget"})  # zero rejected
    c.post(reverse("ak:add_self"), {"krydser": "-2", "reason": "noget"})  # negative rejected
    c.post(reverse("ak:add_self"), {"krydser": "2", "reason": "   "})  # blank reason rejected
    assert not AkEntry.objects.filter(resident=r).exists()


@pytest.mark.django_db
def test_add_self_requires_login() -> None:
    resp = Client().post(reverse("ak:add_self"), {"krydser": "1", "reason": "x"})
    assert resp.status_code == 302  # @login_required → redirect to login
    assert not AkEntry.objects.exists()
