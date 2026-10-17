"""ØK debt warnings (edge-triggered on purchase), configurable interest, and the derived balance
including adjustments."""

from collections.abc import Callable

import pytest
from django.core import mail
from django.test.utils import override_settings

from oelkaelder.models import Adjustment, Deposit, InterestPolicy, Product, Shopper, Warning
from oelkaelder.services import apply_interest, record_purchase

LOCMEM = override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")


@pytest.mark.django_db
def test_balance_includes_valid_adjustments(make_resident: Callable) -> None:
    s = Shopper.objects.create(resident=make_resident(email="a@gahk.dk"))
    Deposit.objects.create(shopper=s, amount_ore=5000)
    Adjustment.objects.create(shopper=s, amount_ore=-2000, kind=Adjustment.Kind.INTEREST)
    assert s.balance_ore == 3000
    Adjustment.objects.create(shopper=s, amount_ore=-1000, is_valid=False)  # invalid drops out
    assert s.balance_ore == 3000


@LOCMEM
@pytest.mark.django_db
def test_purchase_crossing_threshold_sends_one_warning(
    make_resident: Callable, django_capture_on_commit_callbacks: Callable
) -> None:
    Warning.objects.update_or_create(
        id=1, defaults={"message": "Saldo: SALDOSALDOSALDO kr", "threshold_ore": 0, "active": True}
    )
    s = Shopper.objects.create(resident=make_resident(email="buyer@gahk.dk"))
    Deposit.objects.create(shopper=s, amount_ore=500)  # +5 kr, above 0
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
    s = Shopper.objects.create(resident=make_resident(email="rich@gahk.dk"))
    Deposit.objects.create(shopper=s, amount_ore=100000)  # 1000 kr
    p = Product.objects.create(name="Øl", price_ore=1200)
    mail.outbox = []
    with django_capture_on_commit_callbacks(execute=True):
        record_purchase([s.id], [{"product": p.id, "mode": "fixed", "qty": 1}])  # stays far above 0
    assert mail.outbox == []


@pytest.mark.django_db
def test_apply_interest_charges_debtors_once_per_month(make_resident: Callable) -> None:
    InterestPolicy.objects.update_or_create(
        id=1, defaults={"active": True, "rate_percent": 5, "threshold_ore": -10000}
    )
    debtor = Shopper.objects.create(resident=make_resident(email="debt@gahk.dk"))
    p = Product.objects.create(name="Øl", price_ore=20000)  # -200 kr, past the -100 kr threshold
    record_purchase([debtor.id], [{"product": p.id, "mode": "fixed", "qty": 1}])
    assert debtor.balance_ore == -20000

    assert apply_interest() == 1
    assert debtor.balance_ore == -21000  # 5% of 200 kr = 10 kr more debt
    assert apply_interest() == 0  # idempotent within the month
    assert debtor.balance_ore == -21000


@pytest.mark.django_db
def test_apply_interest_respects_active_and_threshold(make_resident: Callable) -> None:
    InterestPolicy.objects.update_or_create(
        id=1, defaults={"active": False, "rate_percent": 5, "threshold_ore": -10000}
    )
    big = Shopper.objects.create(resident=make_resident(email="big@gahk.dk"))
    record_purchase(
        [big.id],
        [{"product": Product.objects.create(name="Øl", price_ore=20000).id, "mode": "fixed", "qty": 1}],
    )
    assert apply_interest() == 0  # interest is off

    InterestPolicy.objects.filter(id=1).update(active=True)
    small = Shopper.objects.create(resident=make_resident(email="small@gahk.dk"))
    record_purchase(
        [small.id],
        [{"product": Product.objects.create(name="Vand", price_ore=5000).id, "mode": "fixed", "qty": 1}],
    )
    apply_interest()
    assert big.adjustments.filter(kind=Adjustment.Kind.INTEREST).exists()  # -200 kr charged
    assert not small.adjustments.exists()  # -50 kr is inside the threshold, untouched


@pytest.mark.django_db
def test_apply_interest_month_guard_uses_local_time(
    make_resident: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """00:30 on 1 November in Copenhagen is still 31 October in UTC. The guard took year/month from
    the UTC clock but the created_at__month lookup runs in local time, so a charge booked ten minutes
    earlier (local November) was not seen and the debtor was charged twice for the same month."""
    from datetime import UTC, datetime

    from django.utils import timezone

    InterestPolicy.objects.update_or_create(
        id=1, defaults={"active": True, "rate_percent": 5, "threshold_ore": -10000}
    )
    debtor = Shopper.objects.create(resident=make_resident(email="midnight@gahk.dk"))
    Adjustment.objects.create(shopper=debtor, amount_ore=-20000, kind=Adjustment.Kind.MANUAL, reason="gæld")
    Adjustment.objects.create(
        shopper=debtor,
        amount_ore=-1000,
        kind=Adjustment.Kind.INTEREST,
        reason="Rente 5%",
        created_at=datetime(2026, 10, 31, 23, 10, tzinfo=UTC),  # 00:10 on 1 Nov, local
    )
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 10, 31, 23, 30, tzinfo=UTC))

    assert apply_interest() == 0  # November is already charged
