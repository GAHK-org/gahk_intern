"""Old /media/cms/<name> links to the website's pictures (core.media.register_public_copy, cms.apps).

The library was anonymous at /media/cms/ while this app rendered gahk.dk, and gahk.dk now forwards
/media/ to intern. An anonymous request for a picture gahk.dk publishes goes 301 to gahk.dk's own
copy (/billeder/<sha16>/<name>); every other cms/ file still needs a login, and a logged-in resident
gets the file from here as before.
"""

from io import BytesIO
from urllib.parse import quote

import pytest
from django.conf import settings
from django.core.files.base import ContentFile
from django.shortcuts import resolve_url
from django.test import Client
from PIL import Image

from cms.models import CmsImage, Event, NewsItem, Page, SiteSettings
from cms.snapshot import build_snapshot, ensure_image_metadata

pytestmark = pytest.mark.django_db


def _jpeg() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (40, 30), "red").save(buffer, format="JPEG")
    return buffer.getvalue()


def _image(name: str = "Have Foto.jpg", **fields: object) -> CmsImage:
    image = CmsImage(caption="Haven", **fields)
    image.file.save(name, ContentFile(_jpeg()), save=True)
    return image


def _media_path(image: CmsImage) -> str:
    return f"/media/{quote(image.file.name or '', safe='/')}"


def _snapshot_url(image: CmsImage) -> str:
    """Where the snapshot puts `image` on gahk.dk (and fills in its sha256 on the way)."""
    data = build_snapshot().data
    image.refresh_from_db()
    (url,) = [entry["url"] for entry in data["images"] if entry["sha256"] == image.sha256]
    return str(url)


def _login_url() -> str:
    return resolve_url(settings.LOGIN_URL)


def test_an_old_link_to_a_published_picture_goes_to_gahk_dk(settings: object) -> None:
    settings.PUBLIC_SITE_URL = "https://gahk.dk/"  # type: ignore[attr-defined]
    image = _image()
    Page.objects.create(slug="haven", header="Haven", body=f'<p><img src="{image.url}"></p>')
    url = _snapshot_url(image)

    response = Client().get(_media_path(image))

    assert response.status_code == 301
    assert response["Location"] == f"https://gahk.dk{url}"
    assert url.startswith(f"/billeder/{image.sha256[:16]}/")
    # Short and per-browser: the copy goes away once the picture is no longer published.
    assert response["Cache-Control"] == "private, max-age=900"
    assert response["Vary"] == "Cookie"


def test_a_logged_in_resident_still_gets_the_file_from_here(make_resident: object) -> None:
    image = _image()
    Page.objects.create(slug="haven", header="Haven", body=f'<img src="{image.url}">')
    _snapshot_url(image)
    client = Client()
    client.force_login(make_resident(email="editor@gahk.dk"))  # type: ignore[operator]

    response = client.get(_media_path(image))

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == image.file.open("rb").read()


def test_the_address_is_percent_encoded() -> None:
    image = _image("Tovtrækning på græsset.jpg")
    Page.objects.create(slug="haven", header="Haven", body=f'<img src="{image.url}">')
    url = _snapshot_url(image)
    assert "æ" in url

    response = Client().get(_media_path(image))

    assert response.status_code == 301
    assert response["Location"] == f"https://gahk.dk{quote(url, safe='/')}"
    assert "%C3%A6" in response["Location"]


@pytest.mark.parametrize("where", ["news", "event", "background", "default_background", "legacy"])
def test_every_way_of_being_published_counts(where: str) -> None:
    image = _image(legacy_path="upload/images/have.jpg" if where == "legacy" else None)
    tag = f'<img src="{image.url}">'
    if where == "news":
        NewsItem.objects.create(title="Nyhed", body=tag, published_at="2026-11-01T12:00:00+01:00")
    elif where == "event":
        Event.objects.create(title="Havedag", description=tag, starts_on="2026-12-01")
    elif where == "background":
        Page.objects.create(slug="haven", header="Haven", body="<p>x</p>", background_image=image.url)
    elif where == "default_background":
        SiteSettings.objects.update_or_create(pk=1, defaults={"default_background": image.url})
    url = _snapshot_url(image)

    response = Client().get(_media_path(image))

    assert response.status_code == 301
    assert response["Location"] == f"https://gahk.dk{url}"


def test_a_picture_gahk_dk_does_not_publish_still_needs_a_login() -> None:
    """In the library but on no published page: only an unpublished page (no address) uses it."""
    image = _image()
    Page.objects.create(slug=None, header="Kladde", body=f'<img src="{image.url}">')
    assert ensure_image_metadata(image)  # gahk.dk could take it: only "not published" is the reason
    assert build_snapshot().data["images"] == []

    response = Client().get(_media_path(image))

    assert response.status_code == 302
    assert response["Location"].startswith(_login_url())
    assert "Cache-Control" not in response.headers


def test_a_picture_never_in_a_snapshot_still_needs_a_login() -> None:
    """No sha256 yet means no snapshot has carried it, and the check reads no file to find out."""
    image = _image()
    Page.objects.create(slug="haven", header="Haven", body=f'<img src="{image.url}">')

    response = Client().get(_media_path(image))

    assert response.status_code == 302
    assert response["Location"].startswith(_login_url())


def test_a_cms_file_with_no_library_row_still_needs_a_login() -> None:
    response = Client().get("/media/cms/2026/08/logo.jpg")

    assert response.status_code == 302
    assert response["Location"].startswith(_login_url())
