"""CMS image uploads (issue: images had to be committed to GitHub before they could be linked).

Editors now upload from the admin and the body toolbar writes the <img> tag. These cover the parts
that would fail silently or dangerously: the role gate on the endpoints, what file types are let in,
and that the background picker does not eat the migrated legacy paths.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from cms.models import CmsImage, Event, NewsItem, Page
from residents.models import Resident, Role

LIST_URL = "/django-admin/cms/cmsimage/toolbar/list"
UPLOAD_URL = "/django-admin/cms/cmsimage/toolbar/upload"

pytestmark = pytest.mark.django_db


def png(name: str = "plakat.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"\x89PNG\r\n\x1a\n" + b"x" * 64, content_type="image/png")


@pytest.fixture
def editor(make_resident: Callable[..., Resident]) -> Resident:
    """Someone holding a CMS-editor role — pr is the frontpage/PR group, the intended user here."""
    return make_resident(email="pr@gahk.dk", roles=(Role.PR,))


@pytest.fixture
def media_tmp(settings: object, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    return tmp_path


# --- the endpoints the toolbar uses ---------------------------------------------------------------


def test_an_editor_can_upload_and_gets_a_usable_url(
    client: Client, editor: Resident, media_tmp: Path
) -> None:
    """The whole point: a file goes in and a URL comes back that can be dropped into body HTML."""
    client.force_login(editor)

    response = client.post(UPLOAD_URL, {"file": png(), "caption": "Plakat"})

    assert response.status_code == 201
    payload = response.json()
    image = CmsImage.objects.get()
    assert payload["url"] == image.url
    assert payload["url"].startswith("/media/cms/")
    assert payload["alt"] == "Plakat"
    assert (media_tmp / image.file.name).is_file()
    assert image.uploaded_by_id == editor.pk


def test_upload_is_closed_to_residents_without_a_cms_role(
    client: Client, make_resident: Callable[..., Resident], media_tmp: Path
) -> None:
    """It writes to disk from a browser, so the gate matters more here than on a read view."""
    client.force_login(make_resident(email="beboer@gahk.dk"))

    assert client.post(UPLOAD_URL, {"file": png()}).status_code in (302, 403)
    assert client.get(LIST_URL).status_code in (302, 403)
    assert not CmsImage.objects.exists()


def test_upload_requires_login(client: Client, media_tmp: Path) -> None:
    assert client.post(UPLOAD_URL, {"file": png()}).status_code in (302, 403)
    assert not CmsImage.objects.exists()


def test_the_list_endpoint_feeds_the_picker(client: Client, editor: Resident, media_tmp: Path) -> None:
    CmsImage.objects.create(file=png(), caption="Plakat")
    client.force_login(editor)

    images = client.get(LIST_URL).json()["images"]

    assert len(images) == 1
    assert images[0]["label"] == "Plakat"
    assert images[0]["url"].startswith("/media/cms/")


# --- what is allowed in --------------------------------------------------------------------------


def test_svg_is_refused(client: Client, editor: Resident, media_tmp: Path) -> None:
    """An SVG is a document: it can carry <script>, and served from our own origin a direct
    navigation would run it as us — straight past nh3, which only ever sees the page HTML."""
    client.force_login(editor)
    svg = SimpleUploadedFile(
        "logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", content_type="image/svg+xml"
    )

    response = client.post(UPLOAD_URL, {"file": svg})

    assert response.status_code == 400
    assert not CmsImage.objects.exists()


def test_a_disguised_extension_is_refused(client: Client, editor: Resident, media_tmp: Path) -> None:
    """content_type is a client-supplied hint; the extension is what the file is served as. Claiming
    image/png on a .svg must not get through."""
    client.force_login(editor)
    sneaky = SimpleUploadedFile("evil.svg", b"<svg/>", content_type="image/png")

    assert client.post(UPLOAD_URL, {"file": sneaky}).status_code == 400
    assert not CmsImage.objects.exists()


def test_a_non_image_is_refused(client: Client, editor: Resident, media_tmp: Path) -> None:
    client.force_login(editor)
    pdf = SimpleUploadedFile("program.pdf", b"%PDF-1.4", content_type="application/pdf")

    assert client.post(UPLOAD_URL, {"file": pdf}).status_code == 400
    assert not CmsImage.objects.exists()


def test_an_oversized_image_is_refused(
    client: Client, editor: Resident, media_tmp: Path, settings: object
) -> None:
    settings.CMS_IMAGE_MAX_MB = 0  # type: ignore[attr-defined]  — any file counts as too big
    client.force_login(editor)

    response = client.post(UPLOAD_URL, {"file": png()})

    assert response.status_code == 400
    assert "for stort" in response.json()["error"]
    assert not CmsImage.objects.exists()


# --- lifecycle and the background picker ----------------------------------------------------------


def test_the_file_is_deleted_with_the_row(
    client: Client,
    editor: Resident,
    media_tmp: Path,
    django_capture_on_commit_callbacks: Callable,
) -> None:
    """Django has not removed FileField files on delete since 1.3, and an orphan here is invisible:
    nothing lists it and nothing cleans it up."""
    image = CmsImage.objects.create(file=png())
    stored = media_tmp / image.file.name
    assert stored.is_file()

    # The file delete is deferred to commit (core.files) so a rolled-back transaction cannot
    # leave the row alive with its bytes gone.
    with django_capture_on_commit_callbacks(execute=True):
        image.delete()

    assert not stored.exists()


def test_usage_shows_where_an_image_is_referenced(editor: Resident, media_tmp: Path) -> None:
    """Deleting an image that a live page uses would 404 it silently, so the change form says where
    it is in use before you press delete."""
    from cms.admin import CmsImageAdmin

    image = CmsImage.objects.create(file=png(), caption="Plakat")
    Page.objects.create(slug="fest", header="Fest", body=f'<img src="{image.url}">')
    NewsItem.objects.create(
        title="Nyhed", body=f'se <img src="{image.url}">', published_at="2026-01-01T00:00Z"
    )
    Event.objects.create(title="Begivenhed", description=f'<img src="{image.url}">', starts_on="2026-02-01")
    unused = CmsImage.objects.create(file=png("anden.png"))

    usage = CmsImageAdmin.usage(CmsImageAdmin, image)  # type: ignore[arg-type]

    assert "Side: Fest" in usage
    assert "Nyhed: Nyhed" in usage
    assert "Begivenhed: Begivenhed" in usage
    assert CmsImageAdmin.usage(CmsImageAdmin, unused) == "Ingen steder endnu"  # type: ignore[arg-type]


def test_the_background_picker_keeps_an_existing_legacy_path(media_tmp: Path) -> None:
    """Every migrated page stores a /public/… path that is not in the library. Turning the field
    into a dropdown must not silently blank those on the next save."""
    from cms.admin import PageAdminForm

    legacy = "/public/image/upload/images/gang.jpg"
    page = Page.objects.create(slug="faciliteter", header="Faciliteter", background_image=legacy)
    CmsImage.objects.create(file=png(), caption="Ny plakat")

    values = [value for value, _label in PageAdminForm(instance=page).fields["background_image"].choices]

    assert legacy in values  # still selectable...
    assert PageAdminForm(instance=page).initial.get("background_image", page.background_image) == legacy


def test_the_background_picker_offers_the_library(media_tmp: Path) -> None:
    from cms.admin import PageAdminForm

    image = CmsImage.objects.create(file=png(), caption="Ny plakat")
    page = Page.objects.create(slug="forside", header="Forside")

    labels = dict(PageAdminForm(instance=page).fields["background_image"].choices)

    assert labels[image.url] == "Ny plakat"


def test_uploaded_images_survive_the_sanitizer(media_tmp: Path) -> None:
    """nh3 strips anything it does not recognise, so a /media/ src has to be on the allowed side of
    it — otherwise the tag the toolbar just inserted would vanish on save."""
    from cms.sanitize import clean_html

    image = CmsImage.objects.create(file=png(), caption="Plakat")
    html = f'<p>Se her <img src="{image.url}" alt="Plakat"></p>'

    assert image.url in (clean_html(html) or "")


def test_the_toolbar_script_is_loaded_on_the_page_form(client: Client, editor: Resident) -> None:
    """The upload button is the feature; if the Media declaration ever drops, the admin silently
    falls back to hand-written paths and nobody notices until they try."""
    client.force_login(editor)
    page = Page.objects.create(slug="fest", header="Fest")

    html = client.get(reverse("admin:cms_page_change", args=[page.pk])).content.decode()

    assert "cms/insert_image.js" in html
