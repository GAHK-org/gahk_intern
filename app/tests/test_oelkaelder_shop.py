"""The till kiosk (`shop`) reproduces the legacy Angular till natively — server-rendered DOM + ES5,
no Tailwind/Alpine bundle, so it runs on the physical till's iPad (iOS 10.3). Purchases post a `basket`
JSON + repeated `shopper` fields and are priced server-side (F-003)."""

import json
from collections.abc import Callable

import pytest
from django.test import Client
from django.test.utils import override_settings
from django.urls import reverse

from credit.models import Posting
from oelkaelder.ledger import accounts, balance, open_shopper
from oelkaelder.models import LogEntry, Product, PurchaseShare, Transaction
from oelkaelder.services import record_deposit, record_purchase

# The Django test client's REMOTE_ADDR is 127.0.0.1; whitelisting it opens the IP-gated kiosk.
ON_TILL = override_settings(OELKAELDER_KIOSK_IPS=["127.0.0.1"])


@ON_TILL
@pytest.mark.django_db
def test_shop_renders_legacy_dom_without_frontend_bundle(make_resident: Callable) -> None:
    shopper = open_shopper(make_resident(email="buyer@gahk.dk"))
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
    shopper = open_shopper(make_resident(email="b2@gahk.dk"))
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
    s = open_shopper(make_resident(email="p@gahk.dk"))
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
    s = open_shopper(make_resident(email="p2@gahk.dk"))
    step = Product.objects.create(name="Betalingshop", price_ore=0, price_steps=[50, 100])

    with pytest.raises(ValueError, match="Ugyldig pris"):
        record_purchase([s.pk], [{"product": step.pk, "mode": "step", "step_ore": 999, "qty": 1}])


@pytest.mark.django_db
def test_the_executor_pays_the_remainder(make_resident: Callable) -> None:
    """Everyone pays total // n; the first one picked (the executor) also pays what is left over."""
    a, b, c = (open_shopper(make_resident(email=f"{n}@gahk.dk")) for n in "abc")
    product = Product.objects.create(name="Øl", price_ore=1001)

    txn = record_purchase([b.pk, a.pk, c.pk], [{"product": product.pk, "mode": "fixed", "qty": 1}])

    shares = {s.shopper_id: s.share_ore for s in PurchaseShare.objects.filter(transaction=txn)}
    assert shares == {b.pk: 335, a.pk: 333, c.pk: 333}  # 1001 = 3 × 333 + 2, the 2 øre on b
    assert [balance(s) for s in (a, b, c)] == [-333, -335, -333]


@pytest.mark.django_db
def test_a_basket_is_one_transfer_per_sharer_into_salg(make_resident: Callable) -> None:
    a, b = (open_shopper(make_resident(email=f"{n}@gahk.dk")) for n in "ab")
    product = Product.objects.create(name="Øl", price_ore=1000)

    txn = record_purchase([a.pk, b.pk], [{"product": product.pk, "mode": "fixed", "qty": 2}])

    shares = list(PurchaseShare.objects.filter(transaction=txn).select_related("transfer"))
    assert len({s.transfer_id for s in shares}) == 2
    for share in shares:
        assert share.transfer.source == f"oelkaelder.basket:{txn.pk}"
        into = Posting.objects.get(transfer=share.transfer, amount_ore__gt=0)
        assert (into.account, into.amount_ore) == (accounts().sales, 1000)
    assert balance(a) + balance(b) == -2000


@ON_TILL
@pytest.mark.django_db
def test_the_till_posts_the_buyers_in_the_order_they_were_picked(make_resident: Callable) -> None:
    """The executor is whoever was picked first, so the order the till posts is the order that counts."""
    first = open_shopper(make_resident(email="first@gahk.dk", first_name="Zelma"))
    second = open_shopper(make_resident(email="second@gahk.dk", first_name="Adam"))
    product = Product.objects.create(name="Øl", price_ore=1001, active=True)

    Client().post(
        reverse("oelkaelder:purchase"),
        {
            "shopper": [str(first.pk), str(second.pk)],
            "basket": json.dumps([{"product": product.pk, "mode": "fixed", "qty": 1}]),
        },
    )

    assert balance(first) == -501  # first picked, though the list sorts them last: pays the extra øre
    assert balance(second) == -500
    # The till keeps the pick order: `chosen` is appended to as people are tapped, and posted as is.
    html = Client().get(reverse("oelkaelder:shop")).content.decode()
    assert "chosen.push(s)" in html
    assert (
        'for (var i = 0; i < chosen.length; i++) { hf.appendChild(makeInput("shopper", chosen[i].pk)); }'
        in html
    )


@ON_TILL
@pytest.mark.django_db
def test_the_same_basket_posted_twice_is_one_sale(make_resident: Callable) -> None:
    shopper = open_shopper(make_resident(email="b@gahk.dk"))
    product = Product.objects.create(name="Øl", price_ore=800, active=True)
    html = Client().get(reverse("oelkaelder:shop")).content.decode()
    token = html.split('name="token" value="')[1].split('"')[0]
    post = {
        "token": token,
        "shopper": str(shopper.pk),
        "basket": json.dumps([{"product": product.pk, "mode": "fixed", "qty": 1}]),
    }

    Client().post(reverse("oelkaelder:purchase"), post)
    Client().post(reverse("oelkaelder:purchase"), post)  # a double tap

    assert Transaction.objects.count() == 1
    assert balance(shopper) == -800


@pytest.mark.django_db
def test_a_refused_sharer_refuses_the_whole_basket_by_name(make_resident: Callable) -> None:
    """All or nothing: half a basket paid for is worse than none, and the till says who and why."""
    rich = open_shopper(make_resident(email="rich@gahk.dk", first_name="Rig", last_name="Rasmussen"))
    record_deposit(rich, 50000, actor=None)
    poor = open_shopper(make_resident(email="poor@gahk.dk", first_name="Fattig", last_name="Frandsen"))
    product = Product.objects.create(name="Champagne", price_ore=50000)  # 250 kr each: past -200 kr

    with pytest.raises(ValueError, match=r"Fattig Frandsen kan ikke handle: .*kreditgrænsen") as refused:
        record_purchase([rich.pk, poor.pk], [{"product": product.pk, "mode": "fixed", "qty": 1}])

    assert "Rig Rasmussen" not in str(refused.value)
    assert not Transaction.objects.exists()
    assert not PurchaseShare.objects.exists()
    assert not LogEntry.objects.filter(message__startswith="Køb").exists()
    assert (balance(rich), balance(poor)) == (50000, 0)
