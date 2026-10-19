"""The till kiosk (`shop`) reproduces the legacy Angular till natively — server-rendered DOM + ES5,
no Tailwind/Alpine bundle, so it runs on the physical till's iPad (iOS 10.3). Purchases post a `basket`
JSON + repeated `shopper` fields and are priced server-side (F-003)."""

import json
from collections.abc import Callable

import pytest
from django.test import Client
from django.test.utils import override_settings
from django.urls import reverse

from oelkaelder.models import Product, PurchaseShare, Shopper
from oelkaelder.services import record_purchase

# The Django test client's REMOTE_ADDR is 127.0.0.1; whitelisting it opens the IP-gated kiosk.
ON_TILL = override_settings(OELKAELDER_KIOSK_IPS=["127.0.0.1"])


@ON_TILL
@pytest.mark.django_db
def test_shop_renders_legacy_dom_without_frontend_bundle(make_resident: Callable) -> None:
    shopper = Shopper.objects.create(resident=make_resident(email="buyer@gahk.dk"))
    Product.objects.create(name="Fadøl", price_ore=1050, active=True)

    html = Client().get(reverse("oelkaelder:shop")).content.decode()

    # Legacy structure is server-rendered: product grid, keypad, active-users list.
    assert 'id="product-item-list"' in html
    assert 'id="alumneliste"' in html
    assert "Fadøl" in html
    assert f'data-rid="{shopper.resident_id}"' in html
    assert f'action="{reverse("oelkaelder:purchase")}"' in html
    # The till's iOS 10.3 Safari can't parse the Tailwind/Alpine bundle — it must not be referenced.
    assert "dist/app.js" not in html
    assert "dist/app.css" not in html
    assert "x-data" not in html


@ON_TILL
@pytest.mark.django_db
def test_purchase_basket_contract(make_resident: Callable) -> None:
    """The till posts repeated `shopper` + a `basket` JSON blob; prices come from the DB."""
    shopper = Shopper.objects.create(resident=make_resident(email="b2@gahk.dk"))
    product = Product.objects.create(name="Sodavand", price_ore=800, active=True)

    resp = Client().post(
        reverse("oelkaelder:purchase"),
        {
            "shopper": str(shopper.pk),
            "basket": json.dumps([{"product": product.pk, "mode": "fixed", "qty": 2}]),
        },
    )

    assert resp.status_code == 302  # redirect back to the till
    share = PurchaseShare.objects.get(shopper=shopper)
    assert share.share_ore == 1600  # 2 × 800, single shopper gets the whole total


@override_settings(DEBUG=False, OELKAELDER_KIOSK_IPS=[])
@pytest.mark.django_db
def test_shop_blocked_off_network() -> None:
    assert Client().get(reverse("oelkaelder:shop")).status_code == 403


@pytest.mark.django_db
def test_record_purchase_pricing_modes(make_resident: Callable) -> None:
    """fixed, betalingshop step, and weight lines are each priced server-side."""
    s = Shopper.objects.create(resident=make_resident(email="p@gahk.dk"))
    fixed = Product.objects.create(name="Øl", price_ore=1200)
    step = Product.objects.create(name="Betalingshop", price_ore=0, price_steps=[50, 100, 500, 1000])
    weight = Product.objects.create(name="Slik", price_ore=0, weight_price_ore=2000)  # 20 kr / 100 g

    txn = record_purchase(
        [s.pk],
        [
            {"product": fixed.pk, "mode": "fixed", "qty": 2},  # 2400
            {"product": step.pk, "mode": "step", "step_ore": 500, "qty": 1},  # 500
            {"product": weight.pk, "mode": "weight", "grams": 150},  # 2000*150/100 = 3000
        ],
    )

    assert PurchaseShare.objects.get(transaction=txn, shopper=s).share_ore == 2400 + 500 + 3000


@pytest.mark.django_db
def test_record_purchase_rejects_forged_step(make_resident: Callable) -> None:
    s = Shopper.objects.create(resident=make_resident(email="p2@gahk.dk"))
    step = Product.objects.create(name="Betalingshop", price_ore=0, price_steps=[50, 100])

    with pytest.raises(ValueError, match="Ugyldig pris"):
        record_purchase([s.pk], [{"product": step.pk, "mode": "step", "step_ore": 999, "qty": 1}])


@pytest.mark.django_db
def test_record_purchase_splits_evenly(make_resident: Callable) -> None:
    a = Shopper.objects.create(resident=make_resident(email="a@gahk.dk"))
    b = Shopper.objects.create(resident=make_resident(email="b@gahk.dk"))
    product = Product.objects.create(name="Øl", price_ore=1001)  # odd total -> largest-remainder

    txn = record_purchase([a.pk, b.pk], [{"product": product.pk, "mode": "fixed", "qty": 1}])

    shares = sorted(s.share_ore for s in PurchaseShare.objects.filter(transaction=txn))
    assert shares == [500, 501]  # sums exactly to 1001
