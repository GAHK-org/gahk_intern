"""Page types (cms.page_types): /optagelse as a fixed page whose texts are fields, edited, versioned,
audited and published like a body."""

from collections.abc import Callable
from io import BytesIO
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import Client
from django.urls import reverse
from PIL import Image

from cms import services
from cms.models import CmsImage, ContentChange, Page
from cms.snapshot import build_snapshot, preview_request
from residents.models import Resident

FIELDS = {
    "intro": "<p>Velkommen til optagelsen.</p>",
    "criteria": "<h2>Husleje</h2><p><em>Pt. 5.616 kr.</em></p>",
    "quote": "«Flid og trang»",
    "quote_by": "G. A. Hagemann",
}


@pytest.fixture
def editor(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="pr@gahk.dk", first_name="Rita", last_name="Redaktør", roles=("pr",))


@pytest.fixture
def admin(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="admin@gahk.dk", first_name="Ada", last_name="Admin", roles=("administrator",))


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    from cms import tasks

    monkeypatch.setattr(tasks.publish_gahk_web, "delay", lambda **kwargs: calls.append(kwargs))
    return calls


@pytest.fixture
def admissions(db: None) -> Page:
    Page.put_front_page(slug="velkommen", header="Velkommen", body="<p>Hej</p>")
    return Page.objects.create(
        slug="optagelse", header="Optagelse", page_type="admissions", fields=dict(FIELDS)
    )


def client_for(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def _library_image() -> CmsImage:
    buffer = BytesIO()
    Image.new("RGB", (40, 30), "red").save(buffer, format="JPEG")
    image = CmsImage(caption="Gangen")
    image.file.save("gang.jpg", ContentFile(buffer.getvalue()), save=True)
    return image


# ------------------------------------------------------------------------------------- services
@pytest.mark.django_db
def test_saving_cleans_each_text_by_its_kind_and_logs_it_per_text(
    admissions: Page, editor: Resident, published: list[dict[str, Any]]
) -> None:
    services.save_page(
        admissions,
        {
            "fields": {
                "criteria": "<h2>Husleje</h2><script>x</script><p>6.000 kr.</p>",
                "quote": "  To\n linjer  ",
            }
        },
        editor,
    )

    admissions.refresh_from_db()
    assert admissions.fields["criteria"] == "<h2>Husleje</h2><p>6.000 kr.</p>"
    assert admissions.fields["quote"] == "To linjer"
    assert admissions.fields["intro"] == FIELDS["intro"]  # not sent: kept
    change = ContentChange.objects.latest("pk")
    assert set(change.changes) == {"fields.criteria", "fields.quote"}
    assert admissions.versions.first().fields == admissions.fields


@pytest.mark.django_db
def test_a_plain_text_has_a_length_limit(admissions: Page, editor: Resident) -> None:
    with pytest.raises(ValidationError) as caught:
        services.save_page(admissions, {"fields": {"quote_by": "x" * 256}}, editor)
    assert "fields.quote_by" in caught.value.error_dict


@pytest.mark.django_db
def test_a_typed_page_keeps_its_address_and_cannot_be_deleted(admissions: Page, editor: Resident) -> None:
    with pytest.raises(ValidationError) as caught:
        services.save_page(admissions, {"slug": "soeg"}, editor)
    assert "slug" in caught.value.error_dict
    with pytest.raises(ValidationError, match="kan ikke slettes"):
        services.delete_page(admissions, editor)
    assert Page.objects.filter(slug="optagelse").exists()


@pytest.mark.django_db
def test_an_ordinary_page_has_no_fields_to_save(admissions: Page, editor: Resident) -> None:
    ordinary = Page.objects.create(slug="kontakt", header="Kontakt", body="<p>Skriv</p>")
    with pytest.raises(ValidationError):
        services.save_page(ordinary, {"fields": {"intro": "<p>x</p>"}}, editor)


@pytest.mark.django_db
def test_restoring_brings_the_texts_back_but_not_from_before_they_existed(
    admissions: Page, editor: Resident
) -> None:
    services.save_page(admissions, {"fields": {"quote_by": "Første"}}, editor)
    first = admissions.versions.first()
    services.save_page(admissions, {"fields": {"quote_by": "Anden"}}, editor)

    services.restore_version(admissions, first, editor)
    admissions.refresh_from_db()
    assert admissions.fields["quote_by"] == "Første"

    # The version kept from before the page had fields (cms migration 0011) has none.
    old = admissions.versions.create(slug="optagelse", header="Optagelse", body="<p>Gammel tekst</p>")
    result = services.restore_version(admissions, old, editor)
    admissions.refresh_from_db()
    assert admissions.fields["quote_by"] == "Første"
    assert any("før siden fik faste felter" in warning for warning in result.warnings)


@pytest.mark.django_db
def test_an_image_in_a_text_counts_as_used(admissions: Page, editor: Resident) -> None:
    image = _library_image()
    services.save_page(admissions, {"fields": {"criteria": f'<p><img src="{image.url}" alt=""></p>'}}, editor)
    assert services.image_usage(image) == ["Side: Optagelse"]


# ------------------------------------------------------------------------------------- snapshot
@pytest.mark.django_db
def test_the_snapshot_sends_the_page_type_and_its_texts(admissions: Page) -> None:
    image = _library_image()
    admissions.fields["criteria"] = f'<p><img src="{image.url}" alt=""><a href="/intern/">Intern</a></p>'
    admissions.fields["quote"] = "  «Flid»\n og trang "
    admissions.save()

    data = build_snapshot().data
    front, page = data["pages"]
    assert "page_type" not in front and "fields" not in front  # ordinary pages as before
    assert page["page_type"] == "admissions"
    assert set(page["fields"]) == {"intro", "criteria", "quote", "quote_by"}
    [entry] = data["images"]  # the image a text uses is shipped, under its gahk.dk address
    assert entry["url"] in page["fields"]["criteria"] and "/media/" not in page["fields"]["criteria"]
    assert 'href="/intern/"' not in page["fields"]["criteria"]  # links into the intranet point at intern
    assert page["fields"]["quote"] == "«Flid» og trang"


@pytest.mark.django_db
def test_a_preview_of_a_typed_page_sends_its_draft_texts(admissions: Page) -> None:
    draft = Page(
        pk=admissions.pk, slug="optagelse", header="Optagelse", page_type="admissions", fields=FIELDS
    )
    request = preview_request(draft)
    assert request["item"]["page_type"] == "admissions"
    assert request["item"]["fields"]["criteria"] == FIELDS["criteria"]


# --------------------------------------------------------------------------------------- editor
@pytest.mark.django_db
def test_the_editor_shows_the_texts_instead_of_the_body_and_saves_them(
    admissions: Page, editor: Resident, published: list[dict[str, Any]]
) -> None:
    client = client_for(editor)
    url = reverse("cms:page_edit", args=[admissions.pk])
    html = client.get(url).content.decode()
    assert 'name="text_criteria"' in html and 'name="text_quote_by"' in html
    assert 'name="body"' not in html and 'name="path_segment"' not in html
    assert "Siden har en fast adresse." in html
    assert "Slet siden" not in html

    response = client.post(
        url,
        {
            "header": "Optagelse",
            "description": "",
            "background_image": "",
            "text_intro": FIELDS["intro"],
            "text_criteria": "<h2>Husleje</h2><p>6.000 kr.</p>",
            "text_quote": FIELDS["quote"],
            "text_quote_by": FIELDS["quote_by"],
            "loaded_at": admissions.updated_at.isoformat(),
        },
    )
    assert response.status_code == 302
    admissions.refresh_from_db()
    assert admissions.fields["criteria"] == "<h2>Husleje</h2><p>6.000 kr.</p>"
    assert admissions.slug == "optagelse"


@pytest.mark.django_db
def test_a_text_error_lands_on_its_field(admissions: Page, editor: Resident) -> None:
    response = client_for(editor).post(
        reverse("cms:page_edit", args=[admissions.pk]),
        {"header": "Optagelse", "text_quote_by": "x" * 256, "loaded_at": admissions.updated_at.isoformat()},
    )
    assert response.status_code == 200
    assert response.context["form"].errors["text_quote_by"]


@pytest.mark.django_db
def test_the_history_and_change_log_show_what_changed_in_each_text(
    admissions: Page, editor: Resident, published: list[dict[str, Any]]
) -> None:
    services.save_page(admissions, {"fields": {"criteria": "<p>Første</p>"}}, editor)
    services.save_page(admissions, {"fields": {"criteria": "<p>Anden</p>", "quote_by": "Ny"}}, editor)
    client = client_for(editor)

    history = client.get(reverse("cms:page_history", args=[admissions.pk])).content.decode()
    assert "Ændringer i boksen" in history
    assert "<b>Citat af:</b>" in history

    log = client.get(reverse("cms:change_log")).content.decode()
    assert "Boksen" in log and "Citat af" in log


@pytest.mark.django_db
def test_the_preview_sends_the_draft_texts(
    admissions: Page,
    editor: Resident,
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,  # noqa: ANN401
) -> None:
    settings.GAHK_WEB_KEY = "k"
    settings.GAHK_WEB_URL = "https://gahk.dk"
    sent: list[dict[str, Any]] = []

    def fake_preview(self: object, request: dict[str, Any]) -> str:
        sent.append(request)
        return "<!doctype html><html><head></head><body>gahk.dk</body></html>"

    monkeypatch.setattr("cms.gahk_web.GahkWebClient.preview", fake_preview)
    client_for(editor).post(
        reverse("cms:preview"),
        {
            "preview_kind": "page",
            "preview_id": admissions.pk,
            "header": "Optagelse",
            "text_criteria": "<p>Kladde</p><script>x</script>",
            "text_quote": "«Kladde»",
        },
    )
    item = sent[0]["item"]
    assert item["page_type"] == "admissions" and item["path"] == "optagelse"
    assert item["fields"]["criteria"] == "<p>Kladde</p>"
    assert item["fields"]["quote"] == "«Kladde»"
