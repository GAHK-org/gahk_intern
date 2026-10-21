"""The regnskab UI for editing the alumneregning mail body + følgebrev PDF (F-010)."""

from collections.abc import Callable

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from residents.models import AlumneregningSettings


@pytest.mark.django_db
def test_requires_regnskab_role(make_resident: Callable) -> None:
    plain = make_resident(email="plain@gahk.dk")
    c = Client()
    c.force_login(plain)

    assert c.get(reverse("alumneregning_settings")).status_code == 403


@pytest.mark.django_db
def test_get_renders_the_default_message(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    html = c.get(reverse("alumneregning_settings")).content.decode()

    assert "FORNAVN" in html  # default template body, textarea-rendered


@pytest.mark.django_db
def test_post_saves_a_new_message(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    response = c.post(reverse("alumneregning_settings"), {"message_template": "Ny besked: $FORNAVN"})

    assert response.status_code == 302
    assert AlumneregningSettings.get_solo().message_template == "Ny besked: $FORNAVN"


@pytest.mark.django_db
def test_post_saves_a_followup_pdf(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)
    pdf = SimpleUploadedFile("brev.pdf", b"%PDF-1.4 fake", content_type="application/pdf")

    c.post(reverse("alumneregning_settings"), {"message_template": "x", "followup_pdf": pdf})

    assert AlumneregningSettings.get_solo().followup_pdf.name.endswith("brev.pdf")


@pytest.mark.django_db
def test_post_rejects_a_non_pdf_followup(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)
    not_a_pdf = SimpleUploadedFile("brev.txt", b"hello", content_type="text/plain")

    html = c.post(
        reverse("alumneregning_settings"), {"message_template": "x", "followup_pdf": not_a_pdf}
    ).content.decode()

    assert "skal være en PDF-fil" in html
    assert AlumneregningSettings.get_solo().followup_pdf.name == ""
