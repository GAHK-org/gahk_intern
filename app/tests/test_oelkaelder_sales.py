"""Salgsoverblik + per-person history (F-003, the legacy `allsales` screen)."""

from collections.abc import Callable
from datetime import datetime

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from oelkaelder import views
from oelkaelder.models import (
    Adjustment,
    LogEntry,
    Product,
    PurchaseShare,
    Shopper,
    Transaction,
    TransactionItem,
)
from oelkaelder.services import record_purchase

SALES = "oelkaelder:all_sales"
WIDE = {"start": "2000-01-01", "end": "2100-01-01"}


def _oek(make_resident: Callable) -> Client:
    c = Client()
    c.force_login(make_resident(email="oek-sales@gahk.dk", roles=("oelkaelder",)))
    return c


def _sale(make_resident: Callable, *people: tuple[str, str], qty: int = 2) -> Transaction:
    """A real purchase through the service, so the shares are genuine largest-remainder splits."""
    product = Product.objects.filter(name="Fadøl").first() or Product.objects.create(
        name="Fadøl", price_ore=1050, active=True
    )
    shoppers = [
        Shopper.objects.create(
            resident=make_resident(email=f"{first}.{last}@gahk.dk".lower(), first_name=first, last_name=last)
        )
        for first, last in people
    ]
    return record_purchase([s.pk for s in shoppers], [{"product": product.pk, "mode": "fixed", "qty": qty}])


@pytest.mark.django_db
def test_shows_one_row_per_sale_with_buyers_total_and_items(make_resident: Callable) -> None:
    txn = _sale(make_resident, ("Anders", "Bo"), ("Cecilie", "Dam"))
    html = _oek(make_resident).get(reverse(SALES), WIDE).content.decode()

    assert "Anders Bo, Cecilie Dam" in html
    assert "21,00 kr" in html  # basket total, 2 × 10,50
    assert "10,50" in html  # per person
    assert "2× Fadøl" in html
    assert html.count(reverse("oelkaelder:void_sale", args=[txn.pk])) == 1  # one row, not one per buyer


@pytest.mark.django_db
def test_filters_by_date_range(make_resident: Callable) -> None:
    txn = _sale(make_resident, ("Anders", "Bo"))
    Transaction.objects.filter(pk=txn.pk).update(created_at=timezone.make_aware(datetime(2019, 5, 1, 12, 0)))
    c = _oek(make_resident)
    void_url = reverse("oelkaelder:void_sale", args=[txn.pk])

    assert (
        void_url not in c.get(reverse(SALES), {"start": "2020-01-01", "end": "2020-12-31"}).content.decode()
    )
    assert void_url in c.get(reverse(SALES), {"start": "2019-01-01", "end": "2019-12-31"}).content.decode()


@pytest.mark.django_db
def test_person_search_matches_full_name_and_does_not_duplicate_rows(make_resident: Callable) -> None:
    """Two matching buyers on one basket must still be ONE row — the Exists() vs join regression."""
    txn = _sale(make_resident, ("Anders", "Hansen"), ("Cecilie", "Hansen"))
    c = _oek(make_resident)
    void_url = reverse("oelkaelder:void_sale", args=[txn.pk])

    assert c.get(reverse(SALES), {**WIDE, "q": "Hansen"}).content.decode().count(void_url) == 1
    assert void_url in c.get(reverse(SALES), {**WIDE, "q": "Anders Hansen"}).content.decode()  # full name
    assert void_url in c.get(reverse(SALES), {**WIDE, "q": "anders.hansen@gahk.dk"}).content.decode()
    assert "Ingen handler" in c.get(reverse(SALES), {**WIDE, "q": "findesikke"}).content.decode()


@pytest.mark.django_db
def test_sale_without_buyers_renders_instead_of_dividing_by_zero(make_resident: Callable) -> None:
    """The ETL left historic transactions with items but no shares (buyers never migrated). No fixture
    produces these, and total/len(shares) would be a 500 on every one of them."""
    product = Product.objects.create(name="Sodavand", price_ore=1200)
    txn = Transaction.objects.create()
    TransactionItem.objects.create(transaction=txn, product=product, quantity=1, price_ore=1200)

    html = _oek(make_resident).get(reverse(SALES), WIDE).content.decode()

    assert views.NO_BUYERS in html
    assert "12,00 kr" in html  # total comes from the items, not the (empty) shares
    assert reverse("oelkaelder:void_sale", args=[txn.pk]) not in html  # nothing to refund


@pytest.mark.django_db
def test_buyers_filter_isolates_orphaned_sales(make_resident: Callable) -> None:
    with_buyers = _sale(make_resident, ("Anders", "Bo"))
    product = Product.objects.create(name="Sodavand", price_ore=1200)
    orphan = Transaction.objects.create()
    TransactionItem.objects.create(transaction=orphan, product=product, quantity=1, price_ore=1200)
    c = _oek(make_resident)

    none_html = c.get(reverse(SALES), {**WIDE, "buyers": "none"}).content.decode()
    assert views.NO_BUYERS in none_html
    assert "Anders Bo" not in none_html

    any_html = c.get(reverse(SALES), {**WIDE, "buyers": "any"}).content.decode()
    assert "Anders Bo" in any_html
    assert views.NO_BUYERS not in any_html
    assert reverse("oelkaelder:void_sale", args=[with_buyers.pk]) in any_html


@pytest.mark.django_db
def test_page_does_not_n_plus_one(make_resident: Callable, django_assert_num_queries: Callable) -> None:
    _sale(make_resident, ("Anders", "Bo"))
    c = _oek(make_resident)
    c.get(reverse(SALES), WIDE)  # warm any per-session queries

    with CaptureQueriesContext(connection) as ctx:
        c.get(reverse(SALES), WIDE)
    baseline = len(ctx)

    for n in range(9):
        _sale(make_resident, (f"Extra{n}", "Køber"))
    with django_assert_num_queries(baseline):
        c.get(reverse(SALES), WIDE)


@pytest.mark.django_db
def test_export_covers_whole_filtered_set_not_just_the_page(
    make_resident: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "PAGE_SIZE", 1)
    for n in range(3):
        _sale(make_resident, (f"Køber{n}", "Test"))
    c = _oek(make_resident)

    assert "Side 1 / 3" in c.get(reverse(SALES), WIDE).content.decode()  # one row per page

    csv_resp = c.get(reverse(SALES), {**WIDE, "format": "csv"})
    assert csv_resp["Content-Type"].startswith("text/csv")
    body = csv_resp.content.decode("utf-8-sig").strip().splitlines()
    assert len(body) == 4  # header + all three sales, not just the page

    xlsx = c.get(reverse(SALES), {**WIDE, "format": "xlsx"})
    assert "spreadsheetml.sheet" in xlsx["Content-Type"]
    assert "salgsoverblik-" in xlsx["Content-Disposition"]


@pytest.mark.django_db
def test_export_over_the_cap_is_refused(make_resident: Callable, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(views, "EXPORT_MAX_ROWS", 1)
    _sale(make_resident, ("Anders", "Bo"))
    _sale(make_resident, ("Cecilie", "Dam"))

    resp = _oek(make_resident).get(reverse(SALES), {**WIDE, "format": "csv"})

    assert resp.status_code == 302
    assert "Content-Disposition" not in resp
    assert "start=" in resp["Location"]  # filters preserved on the way back


@pytest.mark.django_db
def test_void_refunds_every_buyer_and_logs(make_resident: Callable) -> None:
    txn = _sale(make_resident, ("Anders", "Bo"), ("Cecilie", "Dam"))
    shoppers = [s.shopper for s in PurchaseShare.objects.filter(transaction=txn)]
    assert [s.balance_ore for s in shoppers] == [-1050, -1050]

    resp = _oek(make_resident).post(reverse("oelkaelder:void_sale", args=[txn.pk]), WIDE)

    assert resp.status_code == 302
    txn.refresh_from_db()
    assert txn.is_valid is False
    assert [s.balance_ore for s in shoppers] == [0, 0]  # BOTH buyers, not just the first

    entry = LogEntry.objects.filter(message__contains="annulleret").get()
    assert f"txn#{txn.pk}" in entry.message
    assert "Anders Bo" in entry.message and "Cecilie Dam" in entry.message
    assert "oek-sales@gahk.dk" in entry.message  # who did it


@pytest.mark.django_db
def test_void_is_idempotent_and_rejects_get(make_resident: Callable) -> None:
    txn = _sale(make_resident, ("Anders", "Bo"))
    url = reverse("oelkaelder:void_sale", args=[txn.pk])
    c = _oek(make_resident)

    assert c.get(url).status_code == 405  # legacy deleteTransaction moved money on GET
    txn.refresh_from_db()
    assert txn.is_valid is True

    c.post(url, WIDE)
    c.post(url, WIDE)
    assert LogEntry.objects.filter(message__contains="annulleret").count() == 1


@pytest.mark.django_db
def test_voided_sale_stays_visible_without_a_void_button(make_resident: Callable) -> None:
    txn = _sale(make_resident, ("Anders", "Bo"))
    c = _oek(make_resident)
    c.post(reverse("oelkaelder:void_sale", args=[txn.pk]), WIDE)

    html = c.get(reverse(SALES), WIDE).content.decode()

    assert "Anders Bo" in html  # the audit trail keeps it
    assert "Annulleret" in html
    assert reverse("oelkaelder:void_sale", args=[txn.pk]) not in html


@pytest.mark.django_db
def test_pager_and_void_redirect_preserve_filters(
    make_resident: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "PAGE_SIZE", 1)
    _sale(make_resident, ("Anders", "Hansen"))
    txn = _sale(make_resident, ("Cecilie", "Hansen"))
    c = _oek(make_resident)

    html = c.get(reverse(SALES), {**WIDE, "q": "Hansen"}).content.decode()
    assert "q=Hansen" in html and "start=2000-01-01" in html  # pager keeps the filters
    assert "format=" not in html.split("næste")[-1][:200]  # …and never turns the pager into a download

    resp = c.post(reverse("oelkaelder:void_sale", args=[txn.pk]), {**WIDE, "q": "Hansen", "page": "1"})
    assert "q=Hansen" in resp["Location"]
    assert "start=2000-01-01" in resp["Location"]


@pytest.mark.django_db
def test_person_history_shows_purchases_deposits_and_balance(make_resident: Callable) -> None:
    from oelkaelder.models import Deposit

    txn = _sale(make_resident, ("Anders", "Bo"))
    shopper = PurchaseShare.objects.get(transaction=txn).shopper
    Deposit.objects.create(shopper=shopper, amount_ore=5000)

    html = (
        _oek(make_resident)
        .get(reverse("oelkaelder:person_history"), {"resident": shopper.resident_id})
        .content.decode()
    )

    assert "Anders Bo" in html
    assert "2× Fadøl" in html
    assert "Indbetaling" in html
    assert "29,00 kr" in html  # 50,00 deposited − 21,00 spent


@pytest.mark.django_db
def test_both_pages_are_oelkaelder_only(make_resident: Callable) -> None:
    txn = _sale(make_resident, ("Anders", "Bo"))
    plain = Client()
    plain.force_login(make_resident(email="plain-sales@gahk.dk"))

    assert plain.get(reverse(SALES)).status_code == 403
    assert plain.get(reverse("oelkaelder:person_history")).status_code == 403
    assert plain.post(reverse("oelkaelder:void_sale", args=[txn.pk])).status_code == 403
    txn.refresh_from_db()
    assert txn.is_valid is True

    admin = Client()
    admin.force_login(make_resident(email="admin-sales@gahk.dk", roles=("administrator",)))
    assert admin.get(reverse(SALES)).status_code == 200  # administrator implies every role


@pytest.mark.django_db
def test_adjustment_subtracts_with_a_written_reason_and_logs(make_resident: Callable) -> None:
    """The migration repair tool: correct a balance, with the explanation recorded for the resident."""
    txn = _sale(make_resident, ("Anders", "Bo"))
    shopper = PurchaseShare.objects.get(transaction=txn).shopper
    assert shopper.balance_ore == -2100

    resp = _oek(make_resident).post(
        reverse("oelkaelder:add_adjustment", args=[shopper.pk]),
        {"direction": "subtract", "amount_kr": "45,50", "reason": "Manglende køb okt. 2023"},
    )

    assert resp.status_code == 302
    assert shopper.balance_ore == -2100 - 4550
    adj = Adjustment.objects.get(shopper=shopper, kind=Adjustment.Kind.MANUAL)
    assert adj.amount_ore == -4550
    assert adj.reason == "Manglende køb okt. 2023"
    entry = LogEntry.objects.filter(message__contains="Manuel justering").get()
    assert "oek-sales@gahk.dk" in entry.message and "Manglende køb okt. 2023" in entry.message


@pytest.mark.django_db
def test_adjustment_can_also_credit(make_resident: Callable) -> None:
    """Baskets where only some buyers were migrated over-charged the survivor, so corrections run
    both ways even though the common case is subtracting."""
    txn = _sale(make_resident, ("Anders", "Bo"))
    shopper = PurchaseShare.objects.get(transaction=txn).shopper

    _oek(make_resident).post(
        reverse("oelkaelder:add_adjustment", args=[shopper.pk]),
        {"direction": "add", "amount_kr": "10", "reason": "Betalte hele kurven alene"},
    )

    assert Adjustment.objects.get(shopper=shopper).amount_ore == 1000
    assert shopper.balance_ore == -2100 + 1000


@pytest.mark.django_db
def test_adjustment_requires_reason_and_nonzero_amount(make_resident: Callable) -> None:
    txn = _sale(make_resident, ("Anders", "Bo"))
    shopper = PurchaseShare.objects.get(transaction=txn).shopper
    url = reverse("oelkaelder:add_adjustment", args=[shopper.pk])
    c = _oek(make_resident)

    c.post(url, {"direction": "subtract", "amount_kr": "50", "reason": "   "})
    c.post(url, {"direction": "subtract", "amount_kr": "0", "reason": "Ingen effekt"})
    c.post(url, {"direction": "subtract", "amount_kr": "-50", "reason": "Negativt input"})
    c.post(url, {"direction": "subtract", "amount_kr": "abc", "reason": "Ikke et tal"})

    assert not Adjustment.objects.exists()
    assert shopper.balance_ore == -2100  # untouched by every rejected attempt


@pytest.mark.django_db
def test_adjustment_shows_in_the_residents_own_statement(make_resident: Callable) -> None:
    """The resident must be able to see why their balance moved, not just that it did."""
    txn = _sale(make_resident, ("Anders", "Bo"))
    shopper = PurchaseShare.objects.get(transaction=txn).shopper
    _oek(make_resident).post(
        reverse("oelkaelder:add_adjustment", args=[shopper.pk]),
        {"direction": "subtract", "amount_kr": "45,50", "reason": "Manglende køb okt. 2023"},
    )

    own = Client()
    own.force_login(shopper.resident)
    html = own.get(reverse("oelkaelder:my")).content.decode()

    assert "Manglende køb okt. 2023" in html
    assert "-45,50 kr" in html


@pytest.mark.django_db
def test_adjustment_can_be_voided_and_is_idempotent(make_resident: Callable) -> None:
    txn = _sale(make_resident, ("Anders", "Bo"))
    shopper = PurchaseShare.objects.get(transaction=txn).shopper
    c = _oek(make_resident)
    c.post(
        reverse("oelkaelder:add_adjustment", args=[shopper.pk]),
        {"direction": "subtract", "amount_kr": "45,50", "reason": "Tastefejl"},
    )
    adj = Adjustment.objects.get(shopper=shopper)
    url = reverse("oelkaelder:void_adjustment", args=[adj.pk])

    assert c.get(url).status_code == 405
    c.post(url)
    c.post(url)  # double submit

    adj.refresh_from_db()
    assert adj.is_valid is False
    assert shopper.balance_ore == -2100  # correction rolled back
    assert LogEntry.objects.filter(message__contains="annulleret").count() == 1


@pytest.mark.django_db
def test_adjustment_endpoints_are_oelkaelder_only(make_resident: Callable) -> None:
    txn = _sale(make_resident, ("Anders", "Bo"))
    shopper = PurchaseShare.objects.get(transaction=txn).shopper
    plain = Client()
    plain.force_login(make_resident(email="plain-adj@gahk.dk"))

    resp = plain.post(
        reverse("oelkaelder:add_adjustment", args=[shopper.pk]),
        {"direction": "subtract", "amount_kr": "50", "reason": "Bør afvises"},
    )

    assert resp.status_code == 403
    assert not Adjustment.objects.exists()
    assert shopper.balance_ore == -2100
