"""ØK (role `oelkaelder`) manages the kiosk assortment/prices and corrects deposits (F-003)."""

from collections.abc import Callable
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from credit.models import Transfer
from oelkaelder.ledger import accounts, balance, open_shopper
from oelkaelder.models import Deposit, Product, Shopper, Warning
from oelkaelder.services import record_deposit

from .media_samples import heic_bytes


@pytest.mark.django_db
def test_product_create_and_price_update(make_resident: Callable) -> None:
    oek = make_resident(email="oek@gahk.dk", roles=("oelkaelder",))
    c = Client()
    c.force_login(oek)

    c.post(reverse("oelkaelder:product_create"), {"name": "Fadøl", "price_kr": "10,50", "active": "on"})
    p = Product.objects.get(name="Fadøl")
    assert p.price_ore == 1050  # kr -> øre, comma decimals accepted
    assert p.active

    c.post(
        reverse("oelkaelder:product_update", args=[p.id]), {"name": "Fadøl", "price_kr": "12", "active": "on"}
    )
    p.refresh_from_db()
    assert p.price_ore == 1200

    # Unchecking "active" (checkbox absent) takes it out of the till.
    c.post(reverse("oelkaelder:product_update", args=[p.id]), {"name": "Fadøl", "price_kr": "12"})
    p.refresh_from_db()
    assert p.active is False


@pytest.mark.django_db
def test_a_deposit_is_a_transfer_from_the_bank_recorded_by_a_manager(make_resident: Callable) -> None:
    oek = make_resident(email="oek2@gahk.dk", roles=("oelkaelder",))
    shopper = open_shopper(make_resident(email="buyer@gahk.dk", first_name="Bo", last_name="Dam"))
    c = Client()
    c.force_login(oek)

    form = c.get(reverse("oelkaelder:admin")).content.decode()
    token = form.split('name="token" value="')[1].split('"')[0]
    post = {"amount_kr": "50,00", "token": token}
    c.post(reverse("oelkaelder:deposit", args=[shopper.pk]), post)
    c.post(reverse("oelkaelder:deposit", args=[shopper.pk]), post)  # the same form twice: one deposit

    deposit = Deposit.objects.get()
    assert (deposit.amount_ore, balance(shopper)) == (5000, 5000)
    transfer = deposit.transfer
    assert transfer.created_by == oek
    sides = {p.account: (p.amount_ore, p.text) for p in transfer.postings.all()}
    assert sides == {
        accounts().bank: (-5000, "Indbetaling fra Bo Dam"),
        shopper.credit_account: (5000, "Indbetaling"),
    }


@pytest.mark.django_db
def test_void_deposit_reverses_its_transfer(make_resident: Callable) -> None:
    oek = make_resident(email="oek2@gahk.dk", roles=("oelkaelder",))
    shopper = open_shopper(make_resident(email="buyer@gahk.dk"))
    dep = record_deposit(shopper, 5000, actor=oek)
    assert balance(shopper) == 5000

    c = Client()
    c.force_login(oek)
    c.post(reverse("oelkaelder:void_deposit", args=[dep.id]))
    c.post(reverse("oelkaelder:void_deposit", args=[dep.id]))  # a double submit reverses once

    assert balance(shopper) == 0
    assert Transfer.objects.filter(reverses=dep.transfer).count() == 1
    # Gone from the admin page's recent deposits, kept in the ledger and the record.
    assert (
        reverse("oelkaelder:void_deposit", args=[dep.id])
        not in c.get(reverse("oelkaelder:admin")).content.decode()
    )


@pytest.mark.django_db
def test_only_the_books_managers_record_deposits(make_resident: Callable) -> None:
    """An administrator sees the ølkælder pages, but the money is Ølkælderen's to move."""
    admin = make_resident(email="admin@gahk.dk", roles=("administrator",))
    shopper = open_shopper(make_resident(email="buyer@gahk.dk"))
    c = Client()
    c.force_login(admin)

    c.post(reverse("oelkaelder:deposit", args=[shopper.pk]), {"amount_kr": "50"})

    assert not Deposit.objects.exists()
    assert balance(shopper) == 0


@pytest.mark.django_db
def test_product_management_is_oelkaelder_only(make_resident: Callable) -> None:
    plain = make_resident(email="plain@gahk.dk")
    c = Client()
    c.force_login(plain)
    assert c.get(reverse("oelkaelder:products")).status_code == 403
    assert c.post(reverse("oelkaelder:product_create"), {"name": "X", "price_kr": "5"}).status_code == 403
    assert not Product.objects.filter(name="X").exists()


@pytest.mark.django_db
def test_deactivate_reactivate_and_add_shopper(make_resident: Callable) -> None:
    oek = make_resident(email="oek@gahk.dk", roles=("oelkaelder",))
    s = open_shopper(make_resident(email="s@gahk.dk"))
    c = Client()
    c.force_login(oek)

    c.post(reverse("oelkaelder:deactivate_shopper", args=[s.id]))
    s.refresh_from_db()
    assert s.active is False

    c.post(reverse("oelkaelder:activate_shopper"), {"shopper": s.id})
    s.refresh_from_db()
    assert s.active is True

    r = make_resident(email="new@gahk.dk")
    c.post(reverse("oelkaelder:add_shopper"), {"resident": r.id})
    added = Shopper.objects.get(resident=r)
    assert added.credit_account.holder.person == r.person  # their own account in the Ølkælder book
    assert added.credit_account.book == accounts().book

    script = make_resident(email="script@gahk.dk", person=None)  # a login that is no one
    c.post(reverse("oelkaelder:add_shopper"), {"resident": script.id})
    assert not Shopper.objects.filter(resident=script).exists()


@pytest.mark.django_db
def test_update_warning_stores_kr_as_ore(make_resident: Callable) -> None:
    oek = make_resident(email="oek2@gahk.dk", roles=("oelkaelder",))
    c = Client()
    c.force_login(oek)
    c.post(
        reverse("oelkaelder:update_warning", args=[1]),
        {"message": "Hej", "threshold_kr": "-100", "active": "on"},
    )
    w = Warning.objects.get(id=1)
    assert w.threshold_ore == -10000  # -100 kr -> øre
    assert w.message == "Hej"


@pytest.mark.django_db
def test_reports_render_and_export(make_resident: Callable) -> None:
    oek = make_resident(email="oek3@gahk.dk", roles=("oelkaelder",))
    s = open_shopper(make_resident(email="d@gahk.dk"))
    record_deposit(s, 5000, actor=None)
    c = Client()
    c.force_login(oek)
    q = {"start": "2000-01-01", "end": "2100-01-01"}

    html = c.get(reverse("oelkaelder:report_deposits"), q).content.decode()
    assert "Indbetalinger" in html and "50,00" in html
    csv_resp = c.get(reverse("oelkaelder:report_deposits"), {**q, "format": "csv"})
    assert csv_resp["Content-Type"].startswith("text/csv")


@pytest.mark.django_db
def test_admin_actions_are_oelkaelder_only(make_resident: Callable) -> None:
    plain = make_resident(email="plain2@gahk.dk")
    c = Client()
    c.force_login(plain)
    assert c.post(reverse("oelkaelder:apply_interest")).status_code == 403
    assert c.get(reverse("oelkaelder:report_sales")).status_code == 403
    assert c.post(reverse("oelkaelder:add_shopper"), {"resident": plain.id}).status_code == 403


@pytest.mark.django_db
def test_a_product_image_goes_through_the_upload_policy(
    make_resident: Callable, settings: object, tmp_path: Path
) -> None:
    """Product images are stored under oel/, which core.media serves WITHOUT a login (the kiosk
    shows them). An SVG there would run script on our origin for anyone who opened its URL, so the
    same policy as everywhere else applies: SVG is dropped with a warning, the product is still
    created, and an iPhone's HEIC is stored as a JPEG."""
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    oek = make_resident(email="oek3@gahk.dk", roles=("oelkaelder",))
    c = Client()
    c.force_login(oek)

    svg = SimpleUploadedFile("logo.svg", b"<svg onload=alert(1)>", content_type="image/svg+xml")
    c.post(reverse("oelkaelder:product_create"), {"name": "Cola", "price_kr": "8", "image": svg})
    product = Product.objects.get(name="Cola")
    assert not product.image

    photo = SimpleUploadedFile("IMG_0001.HEIC", heic_bytes(), content_type="image/heic")
    c.post(
        reverse("oelkaelder:product_update", args=[product.id]),
        {"name": "Cola", "price_kr": "8", "image": photo},
    )
    product.refresh_from_db()
    assert product.image.name.endswith(".jpg")
