"""ØK debt warnings (edge-triggered on purchase), and the monthly interest: one charge per account
into Renter, once a month, skipping exempt accounts (F-003)."""

import datetime
from collections.abc import Callable

import pytest
from django.core import mail
from django.test import Client
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from credit import ledger as credit_ledger
from credit.models import Transfer
from oelkaelder.ledger import accounts, balance, open_shopper
from oelkaelder.models import Adjustment, InterestExemption, InterestPolicy, Product, Warning
from oelkaelder.services import (
    add_exemption,
    apply_interest,
    end_exemption,
    record_adjustment,
    record_deposit,
    record_purchase,
    void_adjustment,
)

LOCMEM = override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")


@pytest.mark.django_db
def test_the_balance_is_the_ledgers(make_resident: Callable) -> None:
    s = open_shopper(make_resident(email="a@gahk.dk"))
    record_deposit(s, 5000, actor=None)
    record_adjustment(s, -2000, "Rettelse", None)
    assert balance(s) == 3000
    void_adjustment(record_adjustment(s, -1000, "Forkert", None).pk, None)  # reversed: drops out
    assert balance(s) == 3000
    assert balance(s) == credit_ledger.balance_of(s.credit_account)


@LOCMEM
@pytest.mark.django_db
def test_purchase_crossing_threshold_sends_one_warning(
    make_resident: Callable, django_capture_on_commit_callbacks: Callable
) -> None:
    Warning.objects.update_or_create(
        id=1, defaults={"message": "Saldo: SALDOSALDOSALDO kr", "threshold_ore": 0, "active": True}
    )
    s = open_shopper(make_resident(email="buyer@gahk.dk"))
    record_deposit(s, 500, actor=None)  # +5 kr, above 0
    p = Product.objects.create(name="Øl", price_ore=1200)  # -7 kr after buying
    mail.outbox = []
    with django_capture_on_commit_callbacks(execute=True):
        record_purchase([s.id], [{"product": p.id, "mode": "fixed", "qty": 1}])
    assert len(mail.outbox) == 1
    assert "-7,00" in mail.outbox[0].body  # SALDOSALDOSALDO token replaced with the new balance


@LOCMEM
@pytest.mark.django_db
def test_no_warning_when_balance_stays_above(
    make_resident: Callable, django_capture_on_commit_callbacks: Callable
) -> None:
    Warning.objects.update_or_create(id=1, defaults={"message": "x", "threshold_ore": 0, "active": True})
    s = open_shopper(make_resident(email="rich@gahk.dk"))
    record_deposit(s, 100000, actor=None)  # 1000 kr
    p = Product.objects.create(name="Øl", price_ore=1200)
    mail.outbox = []
    with django_capture_on_commit_callbacks(execute=True):
        record_purchase([s.id], [{"product": p.id, "mode": "fixed", "qty": 1}])  # stays far above 0
    assert mail.outbox == []


def _interest(active: bool = True) -> None:
    InterestPolicy.objects.update_or_create(
        id=1, defaults={"active": active, "rate_percent": 5, "threshold_ore": -10000}
    )


def _debtor(make_resident: Callable, email: str, debt_ore: int):  # noqa: ANN202
    shopper = open_shopper(make_resident(email=email))
    record_adjustment(shopper, -debt_ore, "Gammel gæld", None)
    return shopper


@pytest.mark.django_db
def test_interest_is_one_charge_per_account_into_renter_once_a_month(make_resident: Callable) -> None:
    _interest()
    a = _debtor(make_resident, "a@gahk.dk", 20000)
    b = _debtor(make_resident, "b@gahk.dk", 30000)

    assert apply_interest() == 2

    assert (balance(a), balance(b)) == (-21000, -31500)  # 5 % of each one's own debt
    charges = Transfer.objects.filter(source="oelkaelder.interest")
    assert charges.count() == 2  # never one shared transfer
    assert credit_ledger.balance_of(accounts().interest) == 1000 + 1500
    assert Adjustment.objects.filter(kind=Adjustment.Kind.INTEREST).count() == 2
    assert apply_interest() == 0  # idempotent within the month
    assert balance(a) == -21000


@pytest.mark.django_db
def test_a_wrong_interest_charge_is_reversed_alone_and_not_charged_again(make_resident: Callable) -> None:
    _interest()
    a = _debtor(make_resident, "a@gahk.dk", 20000)
    b = _debtor(make_resident, "b@gahk.dk", 20000)
    apply_interest()

    wrong = Adjustment.objects.get(shopper=a, kind=Adjustment.Kind.INTEREST)
    assert void_adjustment(wrong.pk, None)

    assert (balance(a), balance(b)) == (-20000, -21000)  # b's charge stands
    assert apply_interest() == 0  # this month's charge for a was made (and reversed); not again


@pytest.mark.django_db
def test_apply_interest_respects_active_and_threshold(make_resident: Callable) -> None:
    _interest(active=False)
    big = _debtor(make_resident, "big@gahk.dk", 20000)
    assert apply_interest() == 0  # interest is off

    InterestPolicy.objects.filter(id=1).update(active=True)
    small = _debtor(make_resident, "small@gahk.dk", 5000)
    apply_interest()
    assert big.adjustments.filter(kind=Adjustment.Kind.INTEREST).exists()  # -200 kr charged
    assert not small.adjustments.filter(kind=Adjustment.Kind.INTEREST).exists()  # -50 kr is inside


@pytest.mark.django_db
def test_an_exempt_account_is_skipped_while_its_exemption_lasts(make_resident: Callable) -> None:
    _interest()
    today = timezone.localdate()
    away = _debtor(make_resident, "away@gahk.dk", 20000)
    later = _debtor(make_resident, "later@gahk.dk", 20000)
    over = _debtor(make_resident, "over@gahk.dk", 20000)
    add_exemption(away, "Udveksling", today - datetime.timedelta(days=3), None, None)
    add_exemption(later, "Fra næste måned", today + datetime.timedelta(days=3), None, None)
    add_exemption(
        over, "Sidste måned", today - datetime.timedelta(days=30), today - datetime.timedelta(days=1), None
    )

    assert apply_interest() == 2

    assert balance(away) == -20000
    assert (balance(later), balance(over)) == (-21000, -21000)
    # The account itself knows nothing of it: exemptions are Ølkælder's own records.
    assert InterestExemption.objects.filter(credit_account=away.credit_account).exists()


@pytest.mark.django_db
def test_ending_an_exemption(make_resident: Callable) -> None:
    today = timezone.localdate()
    shopper = _debtor(make_resident, "a@gahk.dk", 20000)
    running = add_exemption(shopper, "Udveksling", today - datetime.timedelta(days=10), None, None)
    future = add_exemption(shopper, "Senere", today + datetime.timedelta(days=10), None, None)

    end_exemption(running, None)
    end_exemption(future, None)

    running.refresh_from_db()
    assert running.ends_on == today - datetime.timedelta(days=1)  # charged again from today
    assert not InterestExemption.objects.filter(pk=future.pk).exists()  # never applied: removed


@pytest.mark.django_db
def test_managers_add_and_end_exemptions_on_the_admin_page(make_resident: Callable) -> None:
    oek = make_resident(email="oek@gahk.dk", roles=("oelkaelder",))
    shopper = _debtor(make_resident, "a@gahk.dk", 20000)
    c = Client()
    c.force_login(oek)

    c.post(
        reverse("oelkaelder:add_exemption"),
        {
            "shopper": shopper.pk,
            "reason": "Udveksling",
            "starts_on": str(timezone.localdate()),
            "ends_on": "",
        },
    )
    exemption = InterestExemption.objects.get()
    assert (exemption.created_by, exemption.ends_on) == (oek, None)
    assert "Udveksling" in c.get(reverse("oelkaelder:admin")).content.decode()

    c.post(reverse("oelkaelder:end_exemption", args=[exemption.pk]))
    assert not InterestExemption.objects.exists()  # it began today, so it never exempted anything

    plain = Client()
    plain.force_login(make_resident(email="plain@gahk.dk"))
    assert plain.post(reverse("oelkaelder:add_exemption"), {}).status_code == 403
