"""Ølkælder's demo data: the till, the admin pages and GAHK-konto's Ølkælder book, through the real code.

  * Products, and about two thirds of the residents as shoppers, each with their ølkælder account in
    the one Ølkælder book (oelkaelder.ledger, the same one the till uses).
  * The last three months as the old till left them (deposits, baskets, one voided deposit), put into
    the ledger dated when they happened by the history import that brought the real history over: so
    statements, reports and last month's GAHK-konto statement have something in them.
  * Today, through the till and the admin as they work now: deposits, a basket three people share
    (the executor pays the odd øre), a voided sale, a share moved to the executor ("var ikke med"),
    an interest run with one account exempt, and beboer@gahk.dk exactly at the limit.

Runs inside trail.unaudited() (seed_demo); `actor=None` is the system.
"""

import datetime
import random

from django.db import transaction
from django.utils import timezone

from residents.models import Account

from . import history, ledger, services
from .models import Deposit, InterestPolicy, Product, PurchaseShare, Shopper, Transaction, TransactionItem

PRODUCTS = [
    ("Grøn Tuborg", 700, True),
    ("Carlsberg", 700, False),
    ("Sodavand", 500, False),
    ("Fadøl", 1000, True),
    ("Vand", 300, False),
    ("Snacks", 1200, False),
]


def seed(residents: list[Account], now: datetime.datetime, rng: random.Random) -> None:
    products = [
        Product.objects.create(name=n, price_ore=p, active=True, highlighted=h) for n, p, h in PRODUCTS
    ]
    people = [r for r in residents if r.person_id is not None]
    # Roughly 2/3 of residents shop in the cellar; beboer@gahk.dk always, to sit at the limit.
    shoppers = [ledger.open_shopper(r) for r in people if rng.random() < 0.66 or r.email == "beboer@gahk.dk"]
    if len(shoppers) < 4:
        return
    _old_history(shoppers, products, now, rng)
    _today(shoppers, products, rng)


def _old_history(
    shoppers: list[Shopper], products: list[Product], now: datetime.datetime, rng: random.Random
) -> None:
    """Records as the old till kept them, then imported: what a real database had before the ledger."""
    deposits = []
    for s in shoppers:
        for _ in range(rng.randint(2, 4)):
            deposits.append(
                Deposit.objects.create(
                    shopper=s,
                    amount_ore=rng.choice([10000, 20000, 30000]),
                    created_at=now - datetime.timedelta(days=rng.randint(5, 90), minutes=rng.randint(0, 600)),
                )
            )
    for _ in range(80):
        buyers = rng.sample(shoppers, k=rng.randint(1, min(3, len(shoppers))))
        txn = Transaction.objects.create(
            created_at=now - datetime.timedelta(days=rng.randint(2, 60), minutes=rng.randint(0, 600))
        )
        total = 0
        for _ in range(rng.randint(1, 3)):
            product = rng.choice(products)
            qty = rng.randint(1, 3)
            TransactionItem.objects.create(
                transaction=txn, product=product, quantity=qty, price_ore=product.price_ore * qty
            )
            total += product.price_ore * qty
        for buyer, share in zip(buyers, services.split(total, len(buyers)), strict=True):
            PurchaseShare.objects.create(transaction=txn, shopper=buyer, share_ore=share)
    # One deposit typed in twice by mistake back then, and voided: it shows with its reversal.
    history.import_history(voided={"deposit": {deposits[0].pk}, "transaction": set(), "adjustment": set()})


def _today(shoppers: list[Shopper], products: list[Product], rng: random.Random) -> None:
    beer, snacks = products[3], products[5]

    def buy(buyers: list[Shopper], lines: list[dict]) -> Transaction | None:
        try:
            return services.record_purchase([s.pk for s in buyers], lines)
        except ValueError:
            return None  # the limit refused it, as the till would

    for s in shoppers[: len(shoppers) // 2]:
        services.record_deposit(s, rng.choice([10000, 20000]), actor=None)
    rich = sorted(shoppers, key=lambda s: -ledger.balance(s))
    # A basket three people share: 2 Fadøl + 1 Snacks = 32,00 kr, so 10,66 each and the executor
    # (the first picked) 10,68.
    buy(
        rich[:3],
        [{"product": beer.pk, "mode": "fixed", "qty": 2}, {"product": snacks.pk, "mode": "fixed", "qty": 1}],
    )
    # A sale rung up on the wrong person, voided.
    wrong = buy(rich[3:4], [{"product": snacks.pk, "mode": "fixed", "qty": 2}])
    if wrong is not None:
        services.void_purchase(wrong.pk, None)
    # Two at the bar, but only the one who paid was really there: the other's share moved to them.
    pair = buy([rich[0], rich[-1]], [{"product": beer.pk, "mode": "fixed", "qty": 2}])
    if pair is not None:
        other = PurchaseShare.objects.filter(transaction=pair).order_by("pk").last()
        if other is not None and other.transfer_id is not None:
            services.move_share_to_executor(other.pk, None)
    # Interest this month on the two deepest in debt, one of them exempt (away on exchange).
    debtors = sorted((s for s in shoppers if s.resident.email != "beboer@gahk.dk"), key=ledger.balance)
    for debtor in debtors[:2]:
        if ledger.balance(debtor) > -15000:
            services.record_adjustment(
                debtor, -15000 - ledger.balance(debtor), "Manglende køb fra overflytningen", None
            )
    today = timezone.localdate()
    services.add_exemption(
        debtors[0], "Udveksling i udlandet", today - datetime.timedelta(days=10), None, None
    )
    policy = InterestPolicy.get()
    policy.active, policy.rate_percent, policy.threshold_ore = True, 5, -10000
    policy.save()
    services.apply_interest(None)
    # beboer@gahk.dk exactly at the limit: the till shows them blocked.
    plain = next((s for s in shoppers if s.resident.email == "beboer@gahk.dk"), None)
    if plain is not None:
        with transaction.atomic():
            gap = ledger.balance(plain) - (plain.credit_account.credit_limit_ore or 0)
            if gap > 0:
                services.record_adjustment(plain, -gap, "Manglende køb fra overflytningen", None)
