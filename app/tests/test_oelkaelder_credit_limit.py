"""The ølkælder credit limit (PurchasePolicy).

The till used to hardcode `balance_ore <= -10000` on the shopper tile, which silently made anyone
more than 100 kr in debt unselectable — and told them "Forkert nummer" if they typed their alumne
number. That number is the *interest* threshold; the legacy system never refused a sale
(Oelkaelder_model::addItem has no balance check). So the limit is now an explicit, configurable,
off-by-default policy that the server enforces as well as the till.
"""

from collections.abc import Callable

import pytest
from django.test import Client
from django.test.utils import override_settings
from django.urls import reverse

from oelkaelder.models import Deposit, Product, PurchasePolicy, PurchaseShare, Shopper
from oelkaelder.services import bulk_balances, record_purchase
from residents.models import Resident

# The Django test client's REMOTE_ADDR is 127.0.0.1; whitelisting it opens the IP-gated kiosk.
ON_TILL = override_settings(OELKAELDER_KIOSK_IPS=["127.0.0.1"])

pytestmark = pytest.mark.django_db


def indebted(make_resident: Callable[..., Resident], email: str, debt_ore: int) -> Shopper:
    """A shopper who owes `debt_ore`, created the honest way — a purchase they never paid for."""
    shopper = Shopper.objects.create(resident=make_resident(email=email))
    if debt_ore:
        product = Product.objects.create(name=f"Gæld {email}", price_ore=debt_ore, active=True)
        record_purchase([shopper.pk], [{"product": product.pk, "mode": "fixed", "qty": 1}])
    return shopper


def limit(*, active: bool, kr: int = -100) -> PurchasePolicy:
    policy = PurchasePolicy.get()
    policy.active, policy.block_below_ore = active, kr * 100
    policy.save()
    return policy


# --- the reported bug ----------------------------------------------------------------------------


def test_debt_alone_does_not_block_a_purchase(make_resident: Callable[..., Resident]) -> None:
    """The reported bug: -150 kr made the till refuse to sell. Out of the box it must not — the
    policy is off, so debt means warning mails and interest, exactly as in the legacy system."""
    shopper = indebted(make_resident, "skyldner@gahk.dk", 15000)
    assert shopper.balance_ore == -15000
    beer = Product.objects.create(name="Fadøl", price_ore=1000, active=True)

    record_purchase([shopper.pk], [{"product": beer.pk, "mode": "fixed", "qty": 1}])

    assert shopper.balance_ore == -16000  # the sale went through


@ON_TILL
def test_the_till_offers_a_debtor_the_buy_buttons_by_default(
    make_resident: Callable[..., Resident],
) -> None:
    shopper = indebted(make_resident, "skyldner@gahk.dk", 15000)

    html = Client().get(reverse("oelkaelder:shop")).content.decode()

    row = html.split(f'data-rid="{shopper.resident_id}"')[1].split("</li>")[0]
    assert "inaktiv" not in html.split(f'data-rid="{shopper.resident_id}"')[0].rsplit("<li", 1)[1]
    assert 'data-act="vaelg"' in row
    assert "Spærret" not in row


# --- the limit, once ØK turns it on ----------------------------------------------------------------


def test_a_shopper_below_the_limit_is_refused_by_name(make_resident: Callable[..., Resident]) -> None:
    limit(active=True, kr=-100)
    shopper = indebted(make_resident, "skyldner@gahk.dk", 15000)
    beer = Product.objects.create(name="Fadøl", price_ore=1000, active=True)

    with pytest.raises(ValueError, match="Test Beboer"):
        record_purchase([shopper.pk], [{"product": beer.pk, "mode": "fixed", "qty": 1}])

    assert shopper.balance_ore == -15000  # unchanged: the whole transaction rolled back
    assert not PurchaseShare.objects.filter(shopper=shopper, transaction__is_valid=True).count() > 1


def test_exactly_at_the_limit_is_still_allowed(make_resident: Callable[..., Resident]) -> None:
    """`blocks` is strictly-below, so a limit of -100 kr does not refuse someone sitting on -100.00."""
    limit(active=True, kr=-100)
    shopper = indebted(make_resident, "lige@gahk.dk", 10000)
    assert shopper.balance_ore == -10000
    beer = Product.objects.create(name="Fadøl", price_ore=100, active=True)

    record_purchase([shopper.pk], [{"product": beer.pk, "mode": "fixed", "qty": 1}])

    assert shopper.balance_ore == -10100


def test_a_split_purchase_cannot_smuggle_in_a_blocked_shopper(
    make_resident: Callable[..., Resident],
) -> None:
    """Whole sale refused, not a partial charge — half a basket paid for is worse than none."""
    limit(active=True, kr=-100)
    solvent = indebted(make_resident, "rig@gahk.dk", 0)
    Deposit.objects.create(shopper=solvent, amount_ore=50000)
    blocked = indebted(make_resident, "skyldner@gahk.dk", 20000)
    beer = Product.objects.create(name="Fadøl", price_ore=1000, active=True)

    with pytest.raises(ValueError, match="kan ikke handle"):
        record_purchase([solvent.pk, blocked.pk], [{"product": beer.pk, "mode": "fixed", "qty": 1}])

    assert solvent.balance_ore == 50000
    assert blocked.balance_ore == -20000


def test_a_deposit_reopens_the_account(make_resident: Callable[..., Resident]) -> None:
    limit(active=True, kr=-100)
    shopper = indebted(make_resident, "skyldner@gahk.dk", 15000)
    beer = Product.objects.create(name="Fadøl", price_ore=1000, active=True)

    Deposit.objects.create(shopper=shopper, amount_ore=10000)  # now -50 kr
    record_purchase([shopper.pk], [{"product": beer.pk, "mode": "fixed", "qty": 1}])

    assert shopper.balance_ore == -6000


@ON_TILL
def test_the_till_explains_a_block_instead_of_just_greying_it_out(
    make_resident: Callable[..., Resident],
) -> None:
    """The old UI greyed the row and swallowed taps with no reason given, and the alumne-number path
    reported 'Forkert nummer' — sending people to check their ID rather than their balance."""
    limit(active=True, kr=-100)
    shopper = indebted(make_resident, "skyldner@gahk.dk", 15000)

    html = Client().get(reverse("oelkaelder:shop")).content.decode()

    row = html.split(f'data-rid="{shopper.resident_id}"')[1].split("</li>")[0]
    assert "Spærret" in row
    assert 'data-act="vaelg"' not in row  # nothing tappable that would silently do nothing
    assert 'id="blocked"' in html  # the dedicated modal, distinct from wrongnumber
    assert "Kontoen er spærret" in html


@ON_TILL
def test_the_till_shows_the_configured_limit_not_a_hardcoded_one(
    make_resident: Callable[..., Resident],
) -> None:
    limit(active=True, kr=-250)
    indebted(make_resident, "skyldner@gahk.dk", 30000)

    html = Client().get(reverse("oelkaelder:shop")).content.decode()

    assert "mere end -250 kr" in html


# --- supporting machinery ---------------------------------------------------------------------------


def test_bulk_balances_matches_the_per_shopper_property(make_resident: Callable[..., Resident]) -> None:
    """The till reads every balance; bulk_balances replaces three aggregate queries per shopper with
    three in total, so it has to agree with Shopper.balance_ore exactly — including for a shopper
    with no rows at all."""
    owing = indebted(make_resident, "a@gahk.dk", 12345)
    paid = indebted(make_resident, "b@gahk.dk", 0)
    Deposit.objects.create(shopper=paid, amount_ore=7700)
    untouched = Shopper.objects.create(resident=make_resident(email="c@gahk.dk"))

    shoppers = [owing, paid, untouched]

    assert bulk_balances(shoppers) == {s.pk: s.balance_ore for s in shoppers}
    assert bulk_balances([]) == {}


def test_officers_can_change_the_limit(client: Client, make_resident: Callable[..., Resident]) -> None:
    client.force_login(make_resident(email="oek@gahk.dk", roles=("oelkaelder",)))

    response = client.post(
        reverse("oelkaelder:update_purchase_policy"), {"active": "on", "block_below_kr": "-250"}
    )

    assert response.status_code == 302
    policy = PurchasePolicy.get()
    assert policy.active is True
    assert policy.block_below_ore == -25000


def test_the_limit_is_not_editable_without_the_oelkaelder_role(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    client.force_login(make_resident(email="beboer@gahk.dk"))

    response = client.post(
        reverse("oelkaelder:update_purchase_policy"), {"active": "on", "block_below_kr": "-1"}
    )

    assert response.status_code == 403
    assert PurchasePolicy.get().active is False
