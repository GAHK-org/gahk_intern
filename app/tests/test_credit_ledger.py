"""GAHK-konto's ledger: the money rules, the books, the hash chain and the database's own checks."""

import uuid

import pytest
from django.db import DatabaseError, connection, transaction

from audit.models import AuditEvent
from core.append_only import AppendOnlyError
from core.models import Workgroup
from credit import chain, ledger
from credit.holders import holder_for_person, holder_for_workgroup, members
from credit.ledger import LedgerError
from credit.models import CreditAccount, Posting, Transfer

pytestmark = pytest.mark.django_db


def token() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def world(make_resident):  # noqa: ANN001, ANN201
    """Two books, as at GAHK: the house's (GAHK, Regnskab) and Ølkælder's, each with its bank. Bob and
    Lucy live here; Olga is in the ølkælder embedsgruppe and Rita in Regnskab (both by grant)."""
    bob = make_resident("bob@gahk.dk", first_name="Bob")
    lucy = make_resident("lucy@gahk.dk", first_name="Lucy")
    olga = make_resident("olga@gahk.dk", first_name="Olga", roles=("oelkaelder",))
    rita = make_resident("rita@gahk.dk", first_name="Rita", roles=("regnskab",))
    regnskab = holder_for_workgroup(Workgroup.objects.get_or_create(name="Regnskabsgruppen")[0])
    oel = holder_for_workgroup(Workgroup.objects.get_or_create(name="Ølkælderen")[0])
    house = ledger.create_book(
        actor=None, name="GAHK", holder=regnskab, bank_name="Bank", bank_reference="1111 0001", is_main=True
    )
    cellar = ledger.create_book(
        actor=None,
        name="Ølkælder",
        holder=oel,
        bank_name="Bank",
        bank_reference="2222 0002",
        default_credit_limit_ore=-20000,
    )

    class World:
        pass

    w = World()
    w.bob, w.lucy, w.olga, w.rita = bob, lucy, olga, rita
    w.house, w.cellar = house, cellar
    w.house_bank = CreditAccount.objects.get(book=house, bank_reference__gt="")
    w.cellar_bank = CreditAccount.objects.get(book=cellar, bank_reference__gt="")
    w.takings = ledger.open_account(actor=None, book=cellar, holder=oel, name="Salg", credit_limit_ore=None)
    w.bob_g = ledger.main_account(bob.person)
    w.lucy_g = ledger.main_account(lucy.person)
    w.bob_o = ledger.open_account(
        actor=olga, book=cellar, holder=holder_for_person(bob.person), name="Ølkælder"
    )
    w.lucy_o = ledger.open_account(
        actor=olga, book=cellar, holder=holder_for_person(lucy.person), name="Ølkælder"
    )
    return w


def bal(account: CreditAccount) -> int:
    return ledger.balance_of(account)


def deposit(w, account: CreditAccount, amount: int) -> None:  # noqa: ANN001
    """Money paid into the book's bank, recorded by its managers."""
    bank = w.cellar_bank if account.book_id == w.cellar.pk else w.house_bank
    manager = w.olga if account.book_id == w.cellar.pk else w.rita
    ledger.send(actor=manager, sender=bank, receiver=account, amount_ore=amount, dedupe_token=token())


# ---- accounts and holders ---------------------------------------------------------------------


def test_every_person_gets_one_general_account_in_the_main_book(world) -> None:  # noqa: ANN001
    again = ledger.main_account(world.bob.person)
    assert again == world.bob_g
    assert world.bob_g.book == world.house and world.bob_g.name == "Generel"
    assert world.bob_g.credit_limit_ore == 0  # the house gives no credit by default


def test_an_account_starts_with_its_books_default_limit(world) -> None:  # noqa: ANN001
    assert world.bob_o.credit_limit_ore == -20000


def test_only_a_books_managers_open_accounts_in_it(world) -> None:  # noqa: ANN001
    with pytest.raises(LedgerError):
        ledger.open_account(
            actor=world.bob, book=world.cellar, holder=holder_for_person(world.bob.person), name="x"
        )


def test_a_workgroup_holder_acts_through_its_members(world) -> None:  # noqa: ANN001
    assert set(members(world.cellar.holder)) == {world.olga}
    assert set(members(world.house.holder)) == {world.rita}


# ---- send ---------------------------------------------------------------------------------------


def test_send_inside_a_book_may_use_credit_down_to_the_limit(world) -> None:  # noqa: ANN001
    ledger.send(
        actor=world.bob, sender=world.bob_o, receiver=world.takings, amount_ore=15000, dedupe_token=token()
    )
    assert bal(world.bob_o) == -15000
    with pytest.raises(LedgerError, match="kreditgrænsen"):
        ledger.send(
            actor=world.bob, sender=world.bob_o, receiver=world.takings, amount_ore=5001, dedupe_token=token()
        )
    assert bal(world.bob_o) == -15000


def test_to_another_book_only_money_the_account_has(world) -> None:  # noqa: ANN001
    with pytest.raises(LedgerError, match="kun"):
        ledger.send(
            actor=world.bob, sender=world.bob_o, receiver=world.lucy_g, amount_ore=1, dedupe_token=token()
        )
    deposit(world, world.bob_o, 3000)
    ledger.send(
        actor=world.bob, sender=world.bob_o, receiver=world.lucy_g, amount_ore=3000, dedupe_token=token()
    )
    assert bal(world.bob_o) == 0 and bal(world.lucy_g) == 3000


def test_only_ones_own_accounts_send(world) -> None:  # noqa: ANN001
    deposit(world, world.lucy_g, 1000)
    with pytest.raises(LedgerError, match="egne"):
        ledger.send(
            actor=world.bob, sender=world.lucy_g, receiver=world.bob_g, amount_ore=100, dedupe_token=token()
        )


def test_the_same_token_is_the_same_transfer_once(world) -> None:  # noqa: ANN001
    deposit(world, world.bob_g, 1000)
    sent = token()
    first = ledger.send(
        actor=world.bob, sender=world.bob_g, receiver=world.lucy_g, amount_ore=500, dedupe_token=sent
    )
    second = ledger.send(
        actor=world.bob, sender=world.bob_g, receiver=world.lucy_g, amount_ore=500, dedupe_token=sent
    )
    assert first == second
    assert bal(world.bob_g) == 500


def test_each_side_has_its_own_text_and_the_other_side_as_counterparty(world) -> None:  # noqa: ANN001
    deposit(world, world.lucy_g, 1000)
    ledger.send(
        actor=world.lucy,
        sender=world.lucy_g,
        receiver=world.bob_g,
        amount_ore=1000,
        sender_text="Gave til Bob",
        receiver_text="Gave fra din ven",
        dedupe_token=token(),
    )
    lucys = ledger.statement(world.lucy_g)[0]
    bobs = ledger.statement(world.bob_g)[0]
    assert (lucys.posting.text, lucys.posting.amount_ore, lucys.counterparty) == (
        "Gave til Bob",
        -1000,
        world.bob_g,
    )
    assert (bobs.posting.text, bobs.posting.amount_ore, bobs.counterparty) == (
        "Gave fra din ven",
        1000,
        world.lucy_g,
    )


# ---- books, mellemregning and settlement --------------------------------------------------------


def test_a_transfer_between_books_keeps_every_book_at_zero(world) -> None:  # noqa: ANN001
    deposit(world, world.bob_o, 30000)
    ledger.send(
        actor=world.bob, sender=world.bob_o, receiver=world.lucy_g, amount_ore=5000, dedupe_token=token()
    )
    assert ledger.book_total(world.cellar) == 0
    assert ledger.book_total(world.house) == 0
    owes = CreditAccount.objects.get(book=world.cellar, counterpart_book=world.house)
    owed = CreditAccount.objects.get(book=world.house, counterpart_book=world.cellar)
    assert (bal(owes), bal(owed)) == (5000, -5000)  # Ølkælder owes the house 50 kr
    companion = Transfer.objects.get(kind=Transfer.Kind.COMPANION)
    assert companion.companion_of.kind == Transfer.Kind.TRANSFER


def test_settling_is_each_book_recording_its_own_side(world) -> None:  # noqa: ANN001
    deposit(world, world.bob_o, 5000)
    ledger.send(
        actor=world.bob, sender=world.bob_o, receiver=world.lucy_g, amount_ore=5000, dedupe_token=token()
    )
    owes = CreditAccount.objects.get(book=world.cellar, counterpart_book=world.house)
    owed = CreditAccount.objects.get(book=world.house, counterpart_book=world.cellar)
    # Ølkælder pays the house 50 kr: each treasurer records it in their own book.
    ledger.send(
        actor=world.olga, sender=owes, receiver=world.cellar_bank, amount_ore=5000, dedupe_token=token()
    )
    assert bal(owes) + bal(owed) != 0  # half recorded: money in transit
    ledger.send(
        actor=world.rita, sender=world.house_bank, receiver=owed, amount_ore=5000, dedupe_token=token()
    )
    assert (bal(owes), bal(owed)) == (0, 0)
    assert ledger.book_total(world.cellar) == ledger.book_total(world.house) == 0


def test_a_mellemregning_moves_only_inside_its_own_book(world) -> None:  # noqa: ANN001
    deposit(world, world.bob_o, 5000)
    ledger.send(
        actor=world.bob, sender=world.bob_o, receiver=world.lucy_g, amount_ore=5000, dedupe_token=token()
    )
    owes = CreditAccount.objects.get(book=world.cellar, counterpart_book=world.house)
    with pytest.raises(LedgerError, match="mellemregning"):
        ledger.send(
            actor=world.olga, sender=owes, receiver=world.house_bank, amount_ore=100, dedupe_token=token()
        )


# ---- charge and reverse -------------------------------------------------------------------------


def test_a_charge_goes_through_whatever_the_limit(world) -> None:  # noqa: ANN001
    ledger.charge(
        actor=world.olga,
        account=world.bob_o,
        into=world.takings,
        amount_ore=50000,
        text="Rente",
        dedupe_token=token(),
    )
    assert bal(world.bob_o) == -50000


def test_a_charge_goes_only_into_the_books_own_accounts(world) -> None:  # noqa: ANN001
    with pytest.raises(LedgerError, match="egne konti"):
        ledger.charge(
            actor=world.olga,
            account=world.bob_o,
            into=world.lucy_o,
            amount_ore=100,
            text="x",
            dedupe_token=token(),
        )


def test_only_the_books_managers_charge(world) -> None:  # noqa: ANN001
    with pytest.raises(LedgerError, match="ansvarlige"):
        ledger.charge(
            actor=world.rita,
            account=world.bob_o,
            into=world.takings,
            amount_ore=100,
            text="x",
            dedupe_token=token(),
        )


def test_a_reversal_puts_back_exactly_what_moved_once(world) -> None:  # noqa: ANN001
    sale = ledger.send(
        actor=world.bob, sender=world.bob_o, receiver=world.takings, amount_ore=999, dedupe_token=token()
    )
    back = ledger.reverse(actor=world.olga, transfer=sale, dedupe_token=token())
    assert back.reverses == sale
    assert bal(world.bob_o) == 0 and bal(world.takings) == 0
    assert ledger.statement(world.bob_o)[0].posting.text.startswith("Tilbageført: ")
    with pytest.raises(LedgerError, match="allerede"):
        ledger.reverse(actor=world.olga, transfer=sale, dedupe_token=token())
    with pytest.raises(LedgerError):
        ledger.reverse(actor=world.olga, transfer=back, dedupe_token=token())


def test_reversing_a_transfer_between_books_settles_the_mellemregning_too(world) -> None:  # noqa: ANN001
    deposit(world, world.bob_o, 5000)
    gift = ledger.send(
        actor=world.bob, sender=world.bob_o, receiver=world.lucy_g, amount_ore=5000, dedupe_token=token()
    )
    ledger.reverse(actor=world.rita, transfer=gift, dedupe_token=token())
    owes = CreditAccount.objects.get(book=world.cellar, counterpart_book=world.house)
    assert bal(owes) == 0 and bal(world.bob_o) == 5000


def test_only_the_receiving_books_managers_reverse(world) -> None:  # noqa: ANN001
    sale = ledger.send(
        actor=world.bob, sender=world.bob_o, receiver=world.takings, amount_ore=100, dedupe_token=token()
    )
    with pytest.raises(LedgerError):
        ledger.reverse(actor=world.bob, transfer=sale, dedupe_token=token())


# ---- closing ------------------------------------------------------------------------------------


def test_an_account_closes_only_at_zero_and_then_takes_nothing(world) -> None:  # noqa: ANN001
    deposit(world, world.bob_g, 100)
    with pytest.raises(LedgerError, match="0,00"):
        ledger.close_account(actor=world.bob, account=world.bob_g)
    ledger.send(
        actor=world.bob, sender=world.bob_g, receiver=world.lucy_g, amount_ore=100, dedupe_token=token()
    )
    ledger.close_account(actor=world.bob, account=world.bob_g)
    world.bob_g.refresh_from_db()
    with pytest.raises(LedgerError, match="lukket"):
        ledger.send(
            actor=world.lucy, sender=world.lucy_g, receiver=world.bob_g, amount_ore=100, dedupe_token=token()
        )


# ---- the hash chain -----------------------------------------------------------------------------


def test_each_accounts_postings_chain_from_genesis(world) -> None:  # noqa: ANN001
    deposit(world, world.bob_g, 1000)
    ledger.send(
        actor=world.bob, sender=world.bob_g, receiver=world.lucy_g, amount_ore=400, dedupe_token=token()
    )
    postings = list(Posting.objects.filter(account=world.bob_g).order_by("id"))
    assert bytes(postings[0].prev_hash) == chain.GENESIS
    assert bytes(postings[1].prev_hash) == bytes(postings[0].row_hash)
    for posting in postings:
        other = Posting.objects.exclude(pk=posting.pk).get(transfer=posting.transfer)
        assert ledger.posting_hash(posting, other.account_id) == bytes(posting.row_hash)


def test_the_merkle_root_changes_with_any_head() -> None:
    a = chain.merkle_root([(1, b"a" * 32), (2, b"b" * 32), (3, b"c" * 32)])
    assert a != chain.merkle_root([(1, b"a" * 32), (2, b"b" * 32), (3, b"d" * 32)])
    assert a != chain.merkle_root([(1, b"b" * 32), (2, b"a" * 32), (3, b"c" * 32)])
    assert a == chain.merkle_root([(3, b"c" * 32), (1, b"a" * 32), (2, b"b" * 32)])


# ---- the database's own rules -------------------------------------------------------------------


def test_postings_and_transfers_are_never_changed(world) -> None:  # noqa: ANN001
    deposit(world, world.bob_g, 1000)
    posting = Posting.objects.filter(account=world.bob_g).first()
    with pytest.raises(AppendOnlyError):
        posting.save()
    with pytest.raises(AppendOnlyError):
        Posting.objects.all().update(text="x")
    with (
        pytest.raises(DatabaseError, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("UPDATE credit_posting SET amount_ore = 1 WHERE id = %s", [posting.pk])
    with (
        pytest.raises(DatabaseError, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM credit_transfer")


def test_an_accounts_book_never_changes_and_it_is_never_deleted(world) -> None:  # noqa: ANN001
    with (
        pytest.raises(DatabaseError, match="never change"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE credit_creditaccount SET book_id = %s WHERE id = %s", [world.cellar.pk, world.bob_g.pk]
        )
    with (
        pytest.raises(DatabaseError, match="never deleted"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM credit_creditaccount WHERE id = %s", [world.bob_g.pk])


def test_a_transfer_with_one_posting_is_refused(world) -> None:  # noqa: ANN001
    made = Transfer(kind=Transfer.Kind.TRANSFER, dedupe_token=token())
    with pytest.raises(DatabaseError, match="exactly two postings"), transaction.atomic():
        made.save()
        Posting(
            transfer=made,
            account=world.bob_g,
            amount_ore=100,
            balance_after=100,
            text="x",
            prev_hash=chain.GENESIS,
            row_hash=b"x" * 32,
        ).save()
        ledger._check_now()


def test_a_posting_that_skips_its_chain_is_refused(world) -> None:  # noqa: ANN001
    deposit(world, world.bob_g, 1000)
    made = Transfer(kind=Transfer.Kind.TRANSFER, dedupe_token=token())
    with pytest.raises(DatabaseError, match="1000 to 5000"), transaction.atomic():
        made.save()
        head = Posting.objects.filter(account=world.bob_g).first()
        Posting(
            transfer=made,
            account=world.bob_g,
            amount_ore=100,
            balance_after=5000,
            text="x",
            prev_hash=bytes(head.row_hash),
            row_hash=b"x" * 32,
        ).save()


# ---- the audit trail ----------------------------------------------------------------------------


def test_every_transfer_is_in_the_audit_trail(world) -> None:  # noqa: ANN001
    deposit(world, world.bob_g, 1000)
    sent = ledger.send(
        actor=world.bob, sender=world.bob_g, receiver=world.lucy_g, amount_ore=100, dedupe_token=token()
    )
    event = AuditEvent.objects.filter(action="credit.transfer.sent", actor_id=world.bob.pk).get()
    kinds = {(e.kind, e.entity_id) for e in event.entities.all()}
    assert ("credit.transfer", sent.pk) in kinds
    assert sum(1 for kind, _ in kinds if kind == "credit.posting") == 2


# ---- the nightly check --------------------------------------------------------------------------


def test_the_check_passes_a_ledger_written_by_the_rules(world) -> None:  # noqa: ANN001
    from credit import checks

    deposit(world, world.bob_o, 5000)
    ledger.send(
        actor=world.bob, sender=world.bob_o, receiver=world.lucy_g, amount_ore=2000, dedupe_token=token()
    )
    report = checks.run()
    assert report.ok, report.faults
    assert report.postings == Posting.objects.count()


def test_the_check_finds_a_row_changed_behind_the_triggers(world) -> None:  # noqa: ANN001
    """Someone with the database's password can switch the trigger off; the chain still tells."""
    from credit import checks

    deposit(world, world.bob_g, 1000)
    posting = Posting.objects.get(account=world.bob_g)
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")  # Django's deferred foreign keys, checked now
        cursor.execute("ALTER TABLE credit_posting DISABLE TRIGGER credit_posting_append_only")
        cursor.execute("UPDATE credit_posting SET text = 'Ingenting' WHERE id = %s", [posting.pk])
        cursor.execute("ALTER TABLE credit_posting ENABLE TRIGGER credit_posting_append_only")
    report = checks.run_and_report()
    assert any("hash" in fault for fault in report.faults)
    assert AuditEvent.objects.filter(action="credit.check.failed").exists()
