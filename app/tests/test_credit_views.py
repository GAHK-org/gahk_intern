"""GAHK-konto's pages: who sees what, and that the forms reach the ledger."""

import uuid

import pytest
from django.urls import reverse

from core.models import Workgroup
from credit import ledger
from credit.holders import holder_for_person, holder_for_workgroup
from credit.models import Book, CreditAccount, Transfer

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(make_resident):  # noqa: ANN001, ANN201
    bob = make_resident("bob@gahk.dk", first_name="Bob")
    lucy = make_resident("lucy@gahk.dk", first_name="Lucy")
    olga = make_resident("olga@gahk.dk", first_name="Olga", roles=("oelkaelder",))
    admin = make_resident("admin@gahk.dk", first_name="Ada", roles=("administrator",))
    regnskab = holder_for_workgroup(Workgroup.objects.get_or_create(name="Regnskabsgruppen")[0])
    oel = holder_for_workgroup(Workgroup.objects.get_or_create(name="Ølkælderen")[0])
    house = ledger.create_book(
        actor=None, name="GAHK", holder=regnskab, bank_name="Bank", bank_reference="1 1", is_main=True
    )
    cellar = ledger.create_book(
        actor=None,
        name="Ølkælder",
        holder=oel,
        bank_name="Bank",
        bank_reference="2 2",
        default_credit_limit_ore=-20000,
    )
    takings = ledger.open_account(actor=None, book=cellar, holder=oel, name="Salg", credit_limit_ore=None)
    bob_o = ledger.open_account(
        actor=None, book=cellar, holder=holder_for_person(bob.person), name="Ølkælder"
    )
    return {
        "bob": bob,
        "lucy": lucy,
        "olga": olga,
        "admin": admin,
        "house": house,
        "cellar": cellar,
        "takings": takings,
        "bob_o": bob_o,
    }


def test_the_front_page_opens_a_general_account_and_lists_it(client, setup) -> None:  # noqa: ANN001
    client.force_login(setup["lucy"])
    page = client.get(reverse("credit:index"))
    assert page.status_code == 200
    assert CreditAccount.objects.filter(holder__person=setup["lucy"].person, book=setup["house"]).exists()
    assert "Generel" in page.content.decode()


def test_someone_elses_account_is_not_found(client, setup) -> None:  # noqa: ANN001
    client.force_login(setup["lucy"])
    assert client.get(reverse("credit:account", args=[setup["bob_o"].pk])).status_code == 404


def test_a_books_managers_see_its_accounts_and_book_page(client, setup) -> None:  # noqa: ANN001
    client.force_login(setup["olga"])
    assert client.get(reverse("credit:account", args=[setup["bob_o"].pk])).status_code == 200
    assert client.get(reverse("credit:book", args=[setup["cellar"].pk])).status_code == 200
    assert client.get(reverse("credit:book", args=[setup["house"].pk])).status_code == 404


def test_sending_from_the_form_and_sending_twice_counts_once(client, setup) -> None:  # noqa: ANN001
    client.force_login(setup["bob"])
    lucy_g = ledger.main_account(setup["lucy"].person)
    token = str(uuid.uuid4())
    data = {
        "token": token,
        "sender": setup["bob_o"].pk,
        "receiver": setup["takings"].pk,
        "amount": "12,50",
        "sender_text": "Øl",
        "receiver_text": "",
    }
    assert client.post(reverse("credit:transfer"), data).status_code == 302
    assert client.post(reverse("credit:transfer"), data).status_code == 302
    assert ledger.balance_of(setup["bob_o"]) == -1250
    refused = client.post(
        reverse("credit:transfer"), {**data, "token": str(uuid.uuid4()), "receiver": lucy_g.pk}
    )
    assert refused.status_code == 200 and "kun" in refused.content.decode()


def test_the_manager_reverses_from_the_statement(client, setup) -> None:  # noqa: ANN001
    sale = ledger.send(
        actor=setup["bob"],
        sender=setup["bob_o"],
        receiver=setup["takings"],
        amount_ore=500,
        dedupe_token=uuid.uuid4(),
    )
    client.force_login(setup["olga"])
    page = client.get(reverse("credit:account", args=[setup["bob_o"].pk]))
    assert "Tilbagefør" in page.content.decode()
    client.post(
        reverse("credit:reverse", args=[sale.pk]), {"token": str(uuid.uuid4()), "back": setup["bob_o"].pk}
    )
    assert Transfer.objects.filter(reverses=sale).exists()
    assert ledger.balance_of(setup["bob_o"]) == 0


def test_the_manager_charges_and_opens_accounts(client, setup) -> None:  # noqa: ANN001
    client.force_login(setup["olga"])
    url = reverse("credit:charge", args=[setup["cellar"].pk])
    client.post(
        url,
        {
            "token": str(uuid.uuid4()),
            "account": setup["bob_o"].pk,
            "into": setup["takings"].pk,
            "amount": "300",
            "text": "Rente",
        },
    )
    assert ledger.balance_of(setup["bob_o"]) == -30000  # past the limit: a charge is the keeper's call
    client.post(
        reverse("credit:open_account", args=[setup["cellar"].pk]),
        {"person": setup["lucy"].person.pk, "name": "Ølkælder", "credit_limit": ""},
    )
    made = CreditAccount.objects.get(holder__person=setup["lucy"].person, book=setup["cellar"])
    assert made.credit_limit_ore == -20000


def test_only_administrators_make_books(client, setup) -> None:  # noqa: ANN001
    form = {
        "name": "Fest",
        "workgroup": Workgroup.objects.get_or_create(name="Festudvalget")[0].pk,
        "bank_name": "Bank",
        "bank_reference": "3 3",
        "default_credit_limit": "0",
    }
    client.force_login(setup["olga"])
    assert client.post(reverse("credit:books"), form).status_code == 403
    client.force_login(setup["admin"])
    assert client.post(reverse("credit:books"), form).status_code == 302
    assert Book.objects.filter(name="Fest").exists()
