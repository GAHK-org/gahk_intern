"""Ølkælder's history into GAHK-konto (oelkaelder.history, run by migration 0009): every old record
becomes ledger history dated when it happened, and every balance comes out as the old records said."""

import datetime
from collections.abc import Callable

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from credit import anchors, checks
from credit import ledger as credit_ledger
from credit.models import Anchor, Posting, Transfer
from oelkaelder import history, ledger
from oelkaelder.models import Deposit, PurchaseShare, Shopper, Transaction, TransactionItem
from residents.models import Account

BEFORE = [("oelkaelder", "0008_ledger_links")]


def _latest() -> list[tuple[str, str]]:
    executor = MigrationExecutor(connection)
    return [key for key in executor.loader.graph.leaf_nodes() if key[0] == "oelkaelder"]


@pytest.mark.django_db(transaction=True)
def test_the_migration_turns_the_legacy_rows_into_ledger_history_with_the_same_balances(
    make_resident: Callable[..., Account],
) -> None:
    anna = make_resident(email="anna@gahk.dk", first_name="Anna", last_name="Berg")
    bo = make_resident(email="bo@gahk.dk", first_name="Bo", last_name="Dam")
    cille = make_resident(email="cille@gahk.dk", first_name="Cille", last_name="Ejby")
    latest = _latest()
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    try:
        old = executor.loader.project_state(BEFORE).apps
        S, P, T, Item, Sh, D, A = (  # the historical models
            old.get_model("oelkaelder", name)
            for name in (
                "Shopper",
                "Product",
                "Transaction",
                "TransactionItem",
                "PurchaseShare",
                "Deposit",
                "Adjustment",
            )
        )
        day = timezone.make_aware(datetime.datetime(2024, 3, 1, 12, 0))
        a, b, c = (S.objects.create(resident_id=r.pk) for r in (anna, bo, cille))
        beer = P.objects.create(name="Fadøl", price_ore=1001)
        D.objects.create(shopper=a, amount_ore=5000, created_at=day)
        D.objects.create(shopper=a, amount_ore=9900, created_at=day, is_valid=False)  # voided
        D.objects.create(shopper=b, amount_ore=0, created_at=day)  # nothing moved
        shared = T.objects.create(created_at=day + datetime.timedelta(hours=1))
        Item.objects.create(transaction=shared, product=beer, quantity=1, price_ore=1001)
        Sh.objects.create(transaction=shared, shopper=a, share_ore=501)
        Sh.objects.create(transaction=shared, shopper=b, share_ore=500)
        voided = T.objects.create(created_at=day + datetime.timedelta(hours=2), is_valid=False)
        Item.objects.create(transaction=voided, product=beer, quantity=2, price_ore=2002)
        Sh.objects.create(transaction=voided, shopper=b, share_ore=2002)
        orphan = T.objects.create(created_at=day)  # buyers never migrated: no money
        Item.objects.create(transaction=orphan, product=beer, quantity=1, price_ore=1001)
        A.objects.create(shopper=b, amount_ore=-150, kind="interest", reason="Rente", created_at=day)
        A.objects.create(shopper=c, amount_ore=700, kind="manual", reason="Betalte for meget", created_at=day)
        A.objects.create(
            shopper=c, amount_ore=-300, kind="manual", reason="Tastefejl", is_valid=False, created_at=day
        )

        def legacy(shopper: object) -> int:
            deposits = sum(
                D.objects.filter(shopper=shopper, is_valid=True).values_list("amount_ore", flat=True)
            )
            spent = sum(
                Sh.objects.filter(shopper=shopper, transaction__is_valid=True).values_list(
                    "share_ore", flat=True
                )
            )
            adjusted = sum(
                A.objects.filter(shopper=shopper, is_valid=True).values_list("amount_ore", flat=True)
            )
            return deposits - spent + adjusted

        expected = {s.pk: legacy(s) for s in (a, b, c)}
        assert expected == {a.pk: 5000 - 501, b.pk: -500 - 150, c.pk: 700}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(latest)

    shoppers = Shopper.objects.select_related("credit_account")
    assert {s.pk: ledger.balance(s) for s in shoppers} == expected
    # Each kind of row as its transfer, dated when it happened, and voided ones reversed right after.
    first = Deposit.objects.filter(amount_ore=5000).get()
    assert first.transfer is not None and first.transfer.created_at == day
    gone = Deposit.objects.get(amount_ore=9900)
    assert Transfer.objects.filter(reverses=gone.transfer).exists()
    assert Deposit.objects.get(amount_ore=0).transfer is None
    shares = list(PurchaseShare.objects.filter(transaction_id=shared.pk).order_by("pk"))
    assert [s.transfer.postings.get(amount_ore__gt=0).amount_ore for s in shares] == [501, 500]
    assert Transfer.objects.filter(kind=Transfer.Kind.CHARGE).count() == 2  # interest, the voided -3 kr
    own = ledger.accounts()
    assert credit_ledger.balance_of(own.interest) == 150
    assert credit_ledger.balance_of(own.sales) == 1001
    assert credit_ledger.balance_of(own.bank) == -5000  # money in the bank is negative in the ledger
    assert credit_ledger.book_total(own.book) == 0
    assert TransactionItem.objects.filter(transaction_id=orphan.pk).exists()
    assert checks.run().ok
    # Ids follow the dates: the replay was oldest first.
    dates = [p.transfer.created_at for p in Posting.objects.select_related("transfer").order_by("pk")]
    assert dates == sorted(dates)
    # Run again: everything is linked, nothing is written twice.
    count = Transfer.objects.count()
    assert history.import_history() == {"deposits": 0, "purchases": 0, "adjustments": 0, "reversals": 0}
    assert Transfer.objects.count() == count


@pytest.mark.django_db
def test_the_import_refuses_a_book_an_anchor_already_fixes(make_resident: Callable[..., Account]) -> None:
    shopper = ledger.open_shopper(make_resident(email="a@gahk.dk"))
    Deposit.objects.create(
        shopper=shopper, amount_ore=5000, created_at=timezone.now() - datetime.timedelta(days=40)
    )
    anchors.make(Anchor.Kind.DAILY, timezone.now() + datetime.timedelta(minutes=1))

    with pytest.raises(history.HistoryError, match="anchor"):
        history.import_history()

    assert not Transfer.objects.exists()


@pytest.mark.django_db
def test_history_is_never_written_into_a_closed_period(make_resident: Callable[..., Account]) -> None:
    from credit import closing
    from credit.models import Book, CreditAccount

    shopper = ledger.open_shopper(make_resident(email="a@gahk.dk"))
    cellar = ledger.book()
    opened = timezone.now() - datetime.timedelta(days=70)
    Book.objects.filter(pk=cellar.pk).update(created_at=opened)
    CreditAccount.objects.filter(book=cellar).update(created_at=opened)
    cellar.refresh_from_db()
    manager = make_resident(email="oek@gahk.dk", roles=("oelkaelder",))
    closing.close(actor=manager, book=cellar, bank_statements={ledger.accounts().bank.pk: 0})
    Deposit.objects.create(shopper=shopper, amount_ore=5000, created_at=opened)

    with pytest.raises(history.HistoryError, match="closed a period"):
        history.import_history()
    with pytest.raises(credit_ledger.LedgerError, match="lukket en periode"):
        credit_ledger.record_history(
            kind=Transfer.Kind.TRANSFER,
            sender=ledger.accounts().bank,
            receiver=shopper.credit_account,
            amount_ore=100,
            sender_text="Indbetaling fra A",
            receiver_text="Indbetaling",
            at=timezone.now(),
            dedupe_token=history.token("test", 1),
            source="test",
        )


@pytest.mark.django_db
def test_new_records_and_the_import_meet_in_one_book(make_resident: Callable[..., Account]) -> None:
    """The till's shoppers and the imported history share the one Ølkælder book."""
    shopper = ledger.open_shopper(make_resident(email="a@gahk.dk"))
    Deposit.objects.create(
        shopper=shopper, amount_ore=2500, created_at=timezone.now() - datetime.timedelta(days=3)
    )
    txn = Transaction.objects.create(created_at=timezone.now() - datetime.timedelta(days=2))
    PurchaseShare.objects.create(transaction=txn, shopper=shopper, share_ore=1000)

    assert history.import_history() == {"deposits": 1, "purchases": 1, "adjustments": 0, "reversals": 0}

    assert ledger.balance(shopper) == 1500
    assert shopper.credit_account.book == ledger.book()
