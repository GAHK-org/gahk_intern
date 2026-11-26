"""The ølkælder credit limit: each shopper's GAHK-konto account has its own (new ones get the Ølkælder
book's -200 kr), and the ledger checks the balance AFTER the purchase.

History: the till once hardcoded `balance_ore <= -10000` on the tile (the interest threshold), then a
separate off-by-default PurchasePolicy replaced that. Both are gone: the account's limit is the one
rule, enforced by the ledger for the till and every other way in, and set per account on the book page.
"""

import json
from collections.abc import Callable

import pytest
from django.test import Client
from django.test.utils import override_settings
from django.urls import reverse

from credit import ledger as credit_ledger
from credit.models import Transfer
from oelkaelder.ledger import DEFAULT_CREDIT_LIMIT_ORE, balance, book, open_shopper
from oelkaelder.models import InterestPolicy, Product, PurchaseShare, Shopper, Transaction
from oelkaelder.services import apply_interest, record_adjustment, record_deposit, record_purchase
from residents.models import Account

# The Django test client's REMOTE_ADDR is 127.0.0.1; whitelisting it opens the IP-gated kiosk.
ON_TILL = override_settings(OELKAELDER_KIOSK_IPS=["127.0.0.1"])

pytestmark = pytest.mark.django_db


def indebted(make_resident: Callable[..., Account], email: str, debt_ore: int) -> Shopper:
    """A shopper who owes `debt_ore`, by a correction (a charge: never stopped by the limit)."""
    shopper = open_shopper(make_resident(email=email))
    if debt_ore:
        record_adjustment(shopper, -debt_ore, "Gammel gæld", None)
    return shopper


def buy(shopper: Shopper, price_ore: int) -> Transaction:
    beer = Product.objects.create(name=f"Vare {price_ore}", price_ore=price_ore, active=True)
    return record_purchase([shopper.pk], [{"product": beer.pk, "mode": "fixed", "qty": 1}])


def test_a_new_account_gets_the_books_limit(make_resident: Callable[..., Account]) -> None:
    shopper = open_shopper(make_resident(email="ny@gahk.dk"))

    assert DEFAULT_CREDIT_LIMIT_ORE == -20000
    assert book().default_credit_limit_ore == -20000
    assert shopper.credit_account.credit_limit_ore == -20000
    assert shopper.credit_account.book.holder.workgroup.name == "Ølkælderen"


def test_debt_inside_the_limit_does_not_block_a_purchase(make_resident: Callable[..., Account]) -> None:
    shopper = indebted(make_resident, "skyldner@gahk.dk", 15000)

    buy(shopper, 1000)

    assert balance(shopper) == -16000


def test_the_limit_is_checked_after_the_purchase(make_resident: Callable[..., Account]) -> None:
    """-195 kr may buy for 5 kr (lands on -200) but not for 5,01 kr (would land below it)."""
    shopper = indebted(make_resident, "lige@gahk.dk", 19500)

    with pytest.raises(ValueError, match="kreditgrænsen"):
        buy(shopper, 501)
    assert balance(shopper) == -19500
    assert not Transaction.objects.exists()

    buy(shopper, 500)
    assert balance(shopper) == -20000  # exactly at the limit is allowed


def test_a_shopper_past_the_limit_is_refused_by_name(make_resident: Callable[..., Account]) -> None:
    shopper = indebted(make_resident, "skyldner@gahk.dk", 25000)  # past the limit by a charge

    with pytest.raises(ValueError, match="Test Beboer kan ikke handle"):
        buy(shopper, 100)

    assert balance(shopper) == -25000
    assert not PurchaseShare.objects.exists()


def test_a_split_purchase_cannot_smuggle_in_a_blocked_shopper(make_resident: Callable[..., Account]) -> None:
    solvent = open_shopper(make_resident(email="rig@gahk.dk"))
    record_deposit(solvent, 50000, actor=None)
    blocked = indebted(make_resident, "skyldner@gahk.dk", 19900)
    beer = Product.objects.create(name="Fadøl", price_ore=1000, active=True)

    with pytest.raises(ValueError, match="kan ikke handle"):
        record_purchase([solvent.pk, blocked.pk], [{"product": beer.pk, "mode": "fixed", "qty": 1}])

    assert balance(solvent) == 50000
    assert balance(blocked) == -19900


def test_a_deposit_reopens_the_account(make_resident: Callable[..., Account]) -> None:
    shopper = indebted(make_resident, "skyldner@gahk.dk", 20000)

    record_deposit(shopper, 10000, actor=None)  # now -100 kr
    buy(shopper, 1000)

    assert balance(shopper) == -11000


def test_a_limit_set_on_the_account_is_the_one_that_counts(make_resident: Callable[..., Account]) -> None:
    """Set per account on GAHK-konto's book page, by the book's managers (Ølkælderen)."""
    shopper = indebted(make_resident, "lav@gahk.dk", 0)
    manager = make_resident(email="oek@gahk.dk", roles=("oelkaelder",))
    credit_ledger.set_credit_limit(actor=manager, account=shopper.credit_account, credit_limit_ore=-5000)
    shopper.refresh_from_db()

    with pytest.raises(ValueError, match="kreditgrænsen"):
        buy(shopper, 5001)
    buy(shopper, 5000)
    assert balance(shopper) == -5000


def test_interest_is_never_stopped_by_the_limit(make_resident: Callable[..., Account]) -> None:
    InterestPolicy.objects.update_or_create(
        id=1, defaults={"active": True, "rate_percent": 5, "threshold_ore": -10000}
    )
    shopper = indebted(make_resident, "skyldner@gahk.dk", 20000)  # at the limit

    assert apply_interest() == 1

    assert balance(shopper) == -21000
    assert Transfer.objects.filter(kind=Transfer.Kind.CHARGE).count() == 2


@ON_TILL
def test_the_till_offers_a_debtor_inside_the_limit_the_buy_buttons(
    make_resident: Callable[..., Account],
) -> None:
    shopper = indebted(make_resident, "skyldner@gahk.dk", 15000)

    html = Client().get(reverse("oelkaelder:shop")).content.decode()

    row = html.split(f'data-rid="{shopper.resident_id}"')[1].split("</li>")[0]
    assert "inaktiv" not in html.split(f'data-rid="{shopper.resident_id}"')[0].rsplit("<li", 1)[1]
    assert 'data-act="vaelg"' in row
    assert "Spærret" not in row


@ON_TILL
def test_the_till_explains_a_block_instead_of_just_greying_it_out(
    make_resident: Callable[..., Account],
) -> None:
    """At the limit nothing can be bought: the tile says so rather than offer a button that fails."""
    shopper = indebted(make_resident, "skyldner@gahk.dk", 20000)

    html = Client().get(reverse("oelkaelder:shop")).content.decode()

    row = html.split(f'data-rid="{shopper.resident_id}"')[1].split("</li>")[0]
    assert "Spærret" in row
    assert 'data-act="vaelg"' not in row
    assert 'id="blocked"' in html
    assert "Kontoen er spærret" in html


@ON_TILL
def test_the_till_names_who_was_refused(make_resident: Callable[..., Account]) -> None:
    shopper = indebted(make_resident, "skyldner@gahk.dk", 19900)
    beer = Product.objects.create(name="Fadøl", price_ore=1000, active=True)
    client = Client()

    client.post(
        reverse("oelkaelder:purchase"),
        {"shopper": str(shopper.pk), "basket": json.dumps([{"product": beer.pk, "mode": "fixed", "qty": 1}])},
    )

    html = client.get(reverse("oelkaelder:shop")).content.decode()
    assert "Test Beboer kan ikke handle" in html
    assert balance(shopper) == -19900
