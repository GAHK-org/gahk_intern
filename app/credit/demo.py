"""GAHK-konto's demo data: two books and every state the pages show, without clicking.

  * GAHK (the main book, Regnskabsgruppen) and Ølkælder (Ølkælderen, credit to -200 kr), each with
    its bank account; Ølkælder has its takings account "Salg".
  * Everyone's general account in GAHK; an ølkælder account for the first 20 residents.
  * Deposits from the bank, purchases, a basket two people share (two transfers, the executor
    paying the odd øre), a charge past the limit (interest), a reversed sale, money sent between
    two books (so a mellemregning), a settlement both books recorded and one only Ølkælder has
    recorded so far (the book page's "half recorded" warning), a party account held by an
    embedsgruppe, and a closed account.
  * beboer@gahk.dk sits near Ølkælder's limit; oel@gahk.dk manages Ølkælder's book.

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
    if Book.objects.exists():
        return
    people = [r for r in residents if r.person_id is not None]
    if len(people) < 4:
        return

    def someone(index: int) -> Account:
        return people[index % len(people)]

    regnskab = holder_for_workgroup(Workgroup.objects.get_or_create(name="Regnskabsgruppen")[0])
    oel = holder_for_workgroup(Workgroup.objects.get_or_create(name="Ølkælderen")[0])
    house = ledger.create_book(
        actor=None,
        name="GAHK",
        holder=regnskab,
        bank_name="Alumnekassen",
        bank_reference="5301 0001234",
        is_main=True,
    )
    cellar = ledger.create_book(
        actor=None,
        name="Ølkælder",
        holder=oel,
        bank_name="Bank",
        bank_reference="5301 0005678",
        default_credit_limit_ore=-20000,
    )
    house_bank = CreditAccount.objects.get(book=house, bank_reference__gt="")
    cellar_bank = CreditAccount.objects.get(book=cellar, bank_reference__gt="")
    takings = ledger.open_account(actor=None, book=cellar, holder=oel, name="Salg", credit_limit_ore=None)

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
    cellar_accounts = {
        p.pk: ledger.open_account(
            actor=None, book=cellar, holder=holder_for_person(p.person), name="Ølkælder"
        )
        for p in people[:20]
    }
    # Money in: most pay into Ølkælder's bank, some into the house's.
    for account in cellar_accounts.values():
        if rng.random() < 0.7:
            move(cellar_bank, account, rng.choice([10000, 20000, 30000]), into="Indbetaling, bank")
    for account in list(general.values())[:10]:
        move(house_bank, account, rng.choice([5000, 25000]), into="Indbetaling, bank")
    # Purchases: two to six each, within the limit.
    for account in cellar_accounts.values():
        for _ in range(rng.randint(2, 6)):
            try:
                move(account, takings, rng.choice([1000, 1500, 2500, 999]), out="Ølkælder: køb", into="Salg")
            except ledger.LedgerError:
                break  # at the limit: the till would refuse too
    accounts = list(cellar_accounts.values())
    # A basket two people share: 9,99 kr, the executor pays the odd øre.
    bob, lucy = accounts[0], accounts[1]
    move(bob, takings, 500, out=f"Ølkælder: 3 varer, delt med {lucy.holder.name}", into="Salg (delt)")
    move(lucy, takings, 499, out=f"Ølkælder: 3 varer, delt med {bob.holder.name}", into="Salg (delt)")
    # A sale undone, and interest charged past the limit.
    wrong = move(accounts[2], takings, 2500, out="Ølkælder: køb", into="Salg")
    ledger.reverse(actor=None, transfer=wrong, dedupe_token=_token(rng), source="demo")
    ledger.charge(
        actor=None,
        account=accounts[3],
        into=takings,
        amount_ore=25000,
        text="Rente november",
        dedupe_token=_token(rng),
        source="demo",
    )
    # beboer@gahk.dk near the limit (when they are among the first 20).
    plain = next((p for p in people[:20] if p.email == "beboer@gahk.dk"), None)
    if plain is not None:
        account = cellar_accounts[plain.pk]
        gap = ledger.balance_of(account) - (-18000)
        if gap > 0:
            move(account, takings, gap, out="Ølkælder: køb", into="Salg")
    # Money across books: from an ølkælder account to someone's general account, twice.
    rich = accounts[min(4, len(accounts) - 1)]
    move(cellar_bank, rich, 10000, into="Indbetaling, bank")
    receiver = general[someone(25).pk]
    move(rich, receiver, 2000, out="Til fællesindkøb", into="Fra en ven")
    owes = CreditAccount.objects.get(book=cellar, counterpart_book=house)
    owed = CreditAccount.objects.get(book=house, counterpart_book=cellar)
    # Settled once in full: each book recorded its side.
    move(owes, cellar_bank, 2000, out="Afregning med huset", into="Afregning med huset")
    move(house_bank, owed, 2000, out="Afregning fra Ølkælder", into="Afregning fra Ølkælder")
    # A second crossing, settled only on Ølkælder's side so far: the book page warns.
    move(rich, general[someone(26).pk], 1500)
    move(owes, cellar_bank, 1500, out="Afregning med huset")
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
