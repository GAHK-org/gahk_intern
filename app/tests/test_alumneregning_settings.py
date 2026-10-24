"""The regnskab UI for editing the alumneregning mail body + følgebrev PDF (F-010)."""

from collections.abc import Callable

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from fpdf import FPDF

from regnskab.models import AlumneregningSettings


def _real_pdf_bytes() -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", "", 12)
    pdf.cell(0, 10, "Kaere alumner")
    return bytes(pdf.output())


@pytest.mark.django_db
def test_requires_regnskab_role(make_resident: Callable) -> None:
    plain = make_resident(email="plain@gahk.dk")
    c = Client()
    c.force_login(plain)

    assert c.get(reverse("regnskab:alumneregning_settings")).status_code == 403


@pytest.mark.django_db
def test_get_renders_the_default_message(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    html = c.get(reverse("regnskab:alumneregning_settings")).content.decode()

    assert "FORNAVN" in html  # default template body, textarea-rendered


@pytest.mark.django_db
def test_post_saves_a_new_message(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    response = c.post(reverse("regnskab:alumneregning_settings"), {"message_template": "Ny besked: $FORNAVN"})

    assert response.status_code == 302
    assert AlumneregningSettings.get_solo().message_template == "Ny besked: $FORNAVN"


@pytest.mark.django_db
def test_post_saves_a_followup_pdf(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)
    pdf = SimpleUploadedFile("brev.pdf", _real_pdf_bytes(), content_type="application/pdf")

    c.post(reverse("regnskab:alumneregning_settings"), {"message_template": "x", "followup_pdf": pdf})

    assert AlumneregningSettings.get_solo().followup_pdf.name.endswith("brev.pdf")


@pytest.mark.django_db
def test_post_rejects_a_non_pdf_followup(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)
    not_a_pdf = SimpleUploadedFile("brev.txt", b"hello", content_type="text/plain")

    html = c.post(
        reverse("regnskab:alumneregning_settings"), {"message_template": "x", "followup_pdf": not_a_pdf}
    ).content.decode()

    assert "skal være en PDF-fil" in html
    assert AlumneregningSettings.get_solo().followup_pdf.name == ""


@pytest.mark.django_db
def test_post_rejects_a_file_merely_named_pdf(make_resident: Callable) -> None:
    """Extension/content-type alone don't prove it's a real PDF — a broken upload here would
    otherwise only surface later, as a crash of every alumneregning send that reads it."""
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)
    fake = SimpleUploadedFile("brev.pdf", b"not actually a pdf", content_type="application/pdf")

    html = c.post(
        reverse("regnskab:alumneregning_settings"), {"message_template": "x", "followup_pdf": fake}
    ).content.decode()

    assert "kunne ikke læses som en PDF" in html
    assert AlumneregningSettings.get_solo().followup_pdf.name == ""


@pytest.mark.django_db
def test_post_rejects_an_unknown_placeholder(make_resident: Callable) -> None:
    """A typo'd placeholder ($FORNAVNN) would otherwise reach every alumne as literal text
    (Template.safe_substitute leaves it as-is) — caught here, at the one point a human reviews it."""
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    html = c.post(
        reverse("regnskab:alumneregning_settings"), {"message_template": "Hej $FORNAVNN"}
    ).content.decode()

    assert "Ukendt(e) pladsholder(e)" in html
    assert "$FORNAVNN" in html


@pytest.mark.django_db
def test_post_accepts_every_known_placeholder(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)
    template = "Hej $FORNAVN. $SALDO_LINE Reg: $REG_NR Konto: $KONTO_NR"

    response = c.post(reverse("regnskab:alumneregning_settings"), {"message_template": template})

    assert response.status_code == 302
    assert AlumneregningSettings.get_solo().message_template == template
