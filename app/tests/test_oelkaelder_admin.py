"""ØK (role `oelkaelder`) manages the kiosk assortment/prices and corrects deposits."""

from collections.abc import Callable
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from oelkaelder.models import Deposit, LogEntry, Product, Shopper, Warning

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
def test_void_deposit_drops_out_of_derived_balance(make_resident: Callable) -> None:
    oek = make_resident(email="oek2@gahk.dk", roles=("oelkaelder",))
    shopper = Shopper.objects.create(resident=make_resident(email="buyer@gahk.dk"))
    dep = Deposit.objects.create(shopper=shopper, amount_ore=5000)
    assert shopper.balance_ore == 5000

    c = Client()
    c.force_login(oek)
    c.post(reverse("oelkaelder:void_deposit", args=[dep.id]))

    dep.refresh_from_db()
    assert dep.is_valid is False
    assert shopper.balance_ore == 0  # derived balance excludes the voided deposit


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
    s = Shopper.objects.create(resident=make_resident(email="s@gahk.dk"))
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
    assert Shopper.objects.filter(resident=r).exists()


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
    s = Shopper.objects.create(resident=make_resident(email="d@gahk.dk"))
    Deposit.objects.create(shopper=s, amount_ore=5000)
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


@pytest.mark.django_db
@pytest.mark.parametrize("amount", ["inf", "-inf", "nan", "sNaN", "1e9", "Infinity", "abc"])
def test_non_finite_or_absurd_amounts_are_form_errors(make_resident: Callable, amount: str) -> None:
    """float() accepted "inf"/"nan" and round() then raised OverflowError/ValueError as a 500. Every
    officer money field must turn them into a normal error message and write nothing."""
    oek = make_resident(email="oek3@gahk.dk", roles=("oelkaelder",))
    shopper = Shopper.objects.create(resident=make_resident(email="buyer3@gahk.dk"))
    product = Product.objects.create(name="Fadøl", price_ore=1000)
    c = Client()
    c.force_login(oek)

    posts = [
        (reverse("oelkaelder:deposit", args=[shopper.pk]), {"amount_kr": amount}),
        (
            reverse("oelkaelder:add_adjustment", args=[shopper.pk]),
            {"amount_kr": amount, "direction": "add", "reason": "test"},
        ),
        (reverse("oelkaelder:product_create"), {"name": "Ny", "price_kr": amount}),
        (reverse("oelkaelder:product_update", args=[product.pk]), {"name": "Fadøl", "price_kr": amount}),
    ]
    for url, data in posts:
        resp = c.post(url, data, follow=True)
        assert resp.status_code == 200, url
        assert [m.level_tag for m in resp.context["messages"]] == ["error"], url

    assert shopper.balance_ore == 0
    product.refresh_from_db()
    assert product.price_ore == 1000
    assert not Product.objects.filter(name="Ny").exists()


@pytest.mark.django_db
def test_kroner_amounts_are_parsed_exactly(make_resident: Callable) -> None:
    oek = make_resident(email="oek4@gahk.dk", roles=("oelkaelder",))
    shopper = Shopper.objects.create(resident=make_resident(email="buyer4@gahk.dk"))
    c = Client()
    c.force_login(oek)

    c.post(reverse("oelkaelder:deposit", args=[shopper.pk]), {"amount_kr": "0,29"})

    assert shopper.balance_ore == 29  # float would have needed round() to avoid 28.999…


@pytest.mark.django_db
def test_voiding_a_deposit_is_logged_with_who_and_how_much(make_resident: Callable) -> None:
    """Voiding a sale or an adjustment writes a LogEntry naming the officer; voiding a deposit wrote
    nothing at all, so a vanished payment could not be traced."""
    oek = make_resident(email="oek5@gahk.dk", roles=("oelkaelder",))
    shopper = Shopper.objects.create(resident=make_resident(email="buyer5@gahk.dk"))
    dep = Deposit.objects.create(shopper=shopper, amount_ore=5000)
    c = Client()
    c.force_login(oek)

    first = c.post(reverse("oelkaelder:void_deposit", args=[dep.id]), follow=True)
    assert [m.level_tag for m in first.context["messages"]] == ["success"]
    resp = c.post(reverse("oelkaelder:void_deposit", args=[dep.id]), follow=True)  # double-submit

    logs = list(LogEntry.objects.filter(message__contains=f"Indbetaling #{dep.id}"))
    assert len(logs) == 1
    assert "5000 øre" in logs[0].message
    assert "oek5@gahk.dk" in logs[0].message
    assert shopper.resident.full_name in logs[0].message
    assert [m.level_tag for m in resp.context["messages"]] == ["info"]
