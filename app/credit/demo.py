"""GAHK-konto's demo data: the main book and every state the pages show, without clicking.

  * GAHK (the main book, Regnskabsgruppen) with its bank account, and everyone's general account in it.
  * The Ølkælder book is the ølkælder seed's (oelkaelder via seed_demo, one path for it): its shoppers'
    accounts, deposits, baskets (one shared), a voided sale, interest and someone at the limit.
  * Deposits from the bank, money sent between the two books (so a mellemregning), a settlement both
    books recorded and one only Ølkælder has recorded so far (the book page's "half recorded"
    warning), a party account held by an embedsgruppe, and a closed account.

Written by the ledger itself, so the chains, balances and the nightly check hold (actor None: the
seed is nobody's action, and it runs inside trail.unaudited()).
"""

import datetime
import random
import uuid

from django.utils import timezone

from core.models import Workgroup
from residents.models import Account

from . import ledger
from .holders import holder_for_person, holder_for_workgroup
from .models import Book, CreditAccount, Transfer


def _token(rng: random.Random) -> uuid.UUID:
    return uuid.UUID(int=rng.getrandbits(128), version=4)


def seed(residents: list[Account], now: datetime.datetime, rng: random.Random) -> None:
    if Book.objects.filter(is_main=True).exists():
        return
    people = [r for r in residents if r.person_id is not None]
    if len(people) < 4:
        return

    def someone(index: int) -> Account:
        return people[index % len(people)]

    regnskab = holder_for_workgroup(Workgroup.objects.get_or_create(name="Regnskabsgruppen")[0])
    house = ledger.create_book(
        actor=None,
        name="GAHK",
        holder=regnskab,
        bank_name="Alumnekassen",
        bank_reference="5301 0001234",
        is_main=True,
    )
    house_bank = CreditAccount.objects.get(book=house, bank_reference__gt="")

    def move(
        sender: CreditAccount, receiver: CreditAccount, ore: int, out: str = "", into: str = ""
    ) -> Transfer:
        return ledger.send(
            actor=None,
            sender=sender,
            receiver=receiver,
            amount_ore=ore,
            sender_text=out,
            receiver_text=into,
            dedupe_token=_token(rng),
            source="demo",
        )

    general: dict[int, CreditAccount] = {}
    for p in people:
        opened = ledger.main_account(p.person)
        if opened is not None:  # the main book was made just above
            general[p.pk] = opened
    for account in list(general.values())[:10]:
        move(house_bank, account, rng.choice([5000, 25000]), into="Indbetaling, bank")
    cellar = Book.objects.filter(name="Ølkælder").first()
    if cellar is not None:
        _across_books(cellar, house, house_bank, general, someone, move)
    # A party account held by an embedsgruppe, in the house's book; guests pay from their general.
    party = holder_for_workgroup(Workgroup.objects.get_or_create(name="Festudvalget")[0])
    christmas = ledger.open_account(actor=None, book=house, holder=party, name="Julefrokost")
    for account in list(general.values())[:6]:
        if ledger.balance_of(account) >= 5000:
            move(account, christmas, 5000, out="Julefrokost", into="Julefrokost")
    # A closed account: emptied, then closed.
    spare = ledger.open_account(
        actor=None, book=house, holder=holder_for_person(someone(30).person), name="Opsparing"
    )
    move(house_bank, spare, 1000)
    move(spare, general[someone(30).pk], 1000)
    CreditAccount.objects.filter(pk=spare.pk).update(closed_at=timezone.now())


def _across_books(cellar, house, house_bank, general, someone, move) -> None:  # noqa: ANN001
    """Money from an ølkælder account to general accounts in the house's book, so each book has a
    mellemregning; settled once in full, and a second time only on Ølkælder's side so far."""
    cellar_bank = CreditAccount.objects.filter(book=cellar, bank_reference__gt="").order_by("pk").first()
    shoppers = ledger.with_balances(
        CreditAccount.objects.filter(book=cellar, holder__person__isnull=False, closed_at__isnull=True)
    )
    rich = max(shoppers, key=lambda a: (ledger.annotated_balance(a), -a.pk), default=None)
    if cellar_bank is None or rich is None or ledger.annotated_balance(rich) < 3500:
        return  # only money an account has may leave its book
    move(rich, general[someone(25).pk], 2000, out="Til fællesindkøb", into="Fra en ven")
    owes = CreditAccount.objects.get(book=cellar, counterpart_book=house)
    owed = CreditAccount.objects.get(book=house, counterpart_book=cellar)
    # Settled once in full: each book recorded its side.
    move(owes, cellar_bank, 2000, out="Afregning med huset", into="Afregning med huset")
    move(house_bank, owed, 2000, out="Afregning fra Ølkælder", into="Afregning fra Ølkælder")
    # A second crossing, settled only on Ølkælder's side so far: the book page warns.
    move(rich, general[someone(26).pk], 1500)
    move(owes, cellar_bank, 1500, out="Afregning med huset")


def seed_records(now: datetime.datetime) -> None:
    """Closings and anchors (PR 2): Ølkælder's period two months back closed by oel@gahk.dk, so last
    month's stands ready on the book page with its form; and today's daily anchor."""
    from . import anchors, closing, holders
    from .models import Anchor

    cellar = Book.objects.filter(name="Ølkælder").first()
    if cellar is None or cellar.closings.exists():
        return
    first = (timezone.localdate(now).replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    first = (first - datetime.timedelta(days=1)).replace(day=1)
    opened = timezone.make_aware(datetime.datetime.combine(first, datetime.time()))
    Book.objects.update(created_at=opened)
    CreditAccount.objects.update(created_at=opened)
    cellar.refresh_from_db()
    manager = Account.objects.filter(email="oel@gahk.dk").first()
    if manager is not None and holders.manages(manager, cellar):
        # The bank statements say what the ledger says (the ølkælder seed's history reaches back).
        moment = closing.ends_at(closing.next_period(cellar)[1])
        heads = {h.account_id: h.balance for h in anchors.heads_as_of(moment, book=cellar)}
        banks = {
            a.pk: -heads.get(a.pk, 0)
            for a in CreditAccount.objects.filter(book=cellar).exclude(bank_reference="")
        }
        closing.close(actor=manager, book=cellar, bank_statements=banks, now=now)
    anchors.make(Anchor.Kind.DAILY, now.replace(second=0, microsecond=0))
