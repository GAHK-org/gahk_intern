"""Drive: what a file's page shows of it (images with next and previous, PDF, video and sound inline,
text, CSV and markdown), the thumbnails and icons in the list, and the image previews' life."""

from collections.abc import Callable
from datetime import timedelta
from io import BytesIO
from unittest import mock

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from core.models import Workgroup
from drive import operations, preview, services, sharing
from drive.access import Viewer
from drive.models import File, FileVersion, Folder, ImagePreview
from drive.structure import ensure_structure
from residents.models import Resident

from .drive_support import (  # noqa: F401 - fixtures, used by name
    WINTER,
    _period_folder,
    _today,
    _upload,
    group,
    open_gate,
    resident_in,
    room,
)


@pytest.fixture
def people(resident_in: Callable[..., Resident], group: Workgroup, open_gate: None) -> dict[str, Resident]:
    found = {
        "member": resident_in({(2026, 11): group}),
        "outsider": resident_in({(2026, 11): None}),
    }
    ensure_structure()
    return found


def _client(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def _png(width: int = 2400, height: int = 1200) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), "teal").save(output, format="PNG")
    return output.getvalue()


def _put(client: Client, folder: Folder, name: str, content: bytes, captured: Callable) -> File:
    with captured(execute=True):
        assert _upload(client, folder, name, content)["status"] == 201
    return File.objects.get(folder=folder, name=name)


# --- Images ----------------------------------------------------------------------------------------


def test_an_uploaded_image_gets_a_viewer_copy_and_a_thumbnail(
    people: dict[str, Resident], group: Workgroup, django_capture_on_commit_callbacks: Callable
) -> None:
    member = _client(people["member"])
    file = _put(member, _period_folder(group, WINTER), "Tag.png", _png(), django_capture_on_commit_callbacks)
    row = ImagePreview.objects.get(key=file.latest_version().key)
    assert row.state == ImagePreview.State.READY

    viewer_copy = member.get(reverse("drive:image", args=[file.uuid]))
    with Image.open(BytesIO(b"".join(viewer_copy.streaming_content))) as shown:
        assert (shown.format, max(shown.size)) == ("JPEG", 1600)
    thumbnail = member.get(reverse("drive:thumbnail", args=[file.uuid]))
    with Image.open(BytesIO(b"".join(thumbnail.streaming_content))) as small:
        assert max(small.size) == 320
    assert thumbnail["X-Content-Type-Options"] == "nosniff"

    # Not to anyone who cannot read the file.
    assert _client(people["outsider"]).get(reverse("drive:thumbnail", args=[file.uuid])).status_code == 404


def test_a_heic_from_an_iphone_is_shown_as_a_jpeg(
    people: dict[str, Resident], group: Workgroup, django_capture_on_commit_callbacks: Callable
) -> None:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    output = BytesIO()
    Image.new("RGB", (64, 32), "red").save(output, format="HEIF")
    member = _client(people["member"])
    file = _put(
        member,
        _period_folder(group, WINTER),
        "IMG_0001.HEIC",
        output.getvalue(),
        django_capture_on_commit_callbacks,
    )
    response = member.get(reverse("drive:image", args=[file.uuid]))
    assert response["Content-Type"] == "image/jpeg"
    assert 'filename="IMG_0001.jpg"' in response["Content-Disposition"]


def test_the_list_shows_thumbnails_and_an_icon_for_each_kind(
    people: dict[str, Resident], group: Workgroup, django_capture_on_commit_callbacks: Callable
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    image = _put(member, winter, "Tag.png", _png(), django_capture_on_commit_callbacks)
    _upload(member, winter, "Regnskab.xlsx", b"x")
    _upload(member, winter, "Referat.pdf", b"%PDF")
    page = member.get(reverse("drive:folder", args=[winter.uuid])).content.decode()
    assert f"{reverse('drive:thumbnail', args=[image.uuid])}?v=1" in page
    assert "#i-sheet" in page
    assert "#i-doc" in page


def test_an_image_page_steps_through_the_folders_images_by_name(
    people: dict[str, Resident], group: Workgroup, django_capture_on_commit_callbacks: Callable
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    first = _put(member, winter, "a.png", _png(40, 20), django_capture_on_commit_callbacks)
    _upload(member, winter, "b.txt", b"not an image")
    middle = _put(member, winter, "c.png", _png(40, 20), django_capture_on_commit_callbacks)
    last = _put(member, winter, "d.png", _png(40, 20), django_capture_on_commit_callbacks)

    page = member.get(reverse("drive:file", args=[middle.uuid])).content.decode()
    assert "2 af 3" in page
    assert f'href="{reverse("drive:file", args=[first.uuid])}" rel="prev"' in page
    assert f'href="{reverse("drive:file", args=[last.uuid])}" rel="next"' in page
    assert reverse("drive:image", args=[middle.uuid]) in page


def test_an_image_shared_on_its_own_shows_no_way_into_its_folder(
    people: dict[str, Resident], group: Workgroup, django_capture_on_commit_callbacks: Callable
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    _put(member, winter, "a.png", _png(40, 20), django_capture_on_commit_callbacks)
    shared = _put(member, winter, "b.png", _png(40, 20), django_capture_on_commit_callbacks)
    sharing.share(
        Viewer.for_resident(people["member"]),
        file=shared,
        resident_id=people["outsider"].pk,
        access="read",
        actor=people["member"],
    )
    outsider = _client(people["outsider"])
    page = outsider.get(reverse("drive:file", args=[shared.uuid])).content.decode()
    assert 'rel="prev"' not in page
    assert outsider.get(reverse("drive:image", args=[shared.uuid])).status_code == 200


def test_an_image_that_cannot_be_decoded_says_so(
    people: dict[str, Resident], group: Workgroup, django_capture_on_commit_callbacks: Callable
) -> None:
    member = _client(people["member"])
    file = _put(
        member,
        _period_folder(group, WINTER),
        "Ødelagt.jpg",
        b"not a jpeg",
        django_capture_on_commit_callbacks,
    )
    assert ImagePreview.objects.get().state == ImagePreview.State.FAILED
    page = member.get(reverse("drive:file", args=[file.uuid])).content.decode()
    assert "Billedet kan ikke vises her" in page
    assert member.get(reverse("drive:image", args=[file.uuid])).status_code == 404


def test_a_copy_shares_the_preview_and_it_goes_with_the_last_version(
    people: dict[str, Resident], group: Workgroup, django_capture_on_commit_callbacks: Callable
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    file = _put(member, winter, "Tag.png", _png(40, 20), django_capture_on_commit_callbacks)
    year = Folder.objects.get(kind=Folder.Kind.YEAR, period_start__year=2026)
    with django_capture_on_commit_callbacks(execute=True):
        member.post(
            reverse("drive:move", args=[winter.uuid]), {"op": "copy", "file": file.uuid, "to": year.uuid}
        )
    assert FileVersion.objects.count() == 2
    assert ImagePreview.objects.count() == 1
    row = ImagePreview.objects.get()
    store = services.storage()
    assert store.exists(row.thumbnail_key)

    for each in File.objects.all():
        each.deleted_at = timezone.now() - timedelta(days=31)
        each.save()
    with django_capture_on_commit_callbacks(execute=True):
        operations.empty_bin()
    assert not ImagePreview.objects.exists()
    assert not store.exists(row.thumbnail_key)
    assert not store.exists(row.viewer_key)


def test_previews_left_waiting_are_queued_again(people: dict[str, Resident]) -> None:
    ImagePreview.objects.create(key="drive/old")
    ImagePreview.objects.create(key="drive/new")
    ImagePreview.objects.filter(key="drive/old").update(created_at=timezone.now() - timedelta(hours=2))
    with mock.patch.object(preview, "_queue") as queue:
        assert preview.queue_stranded() == 1
    queue.assert_called_once_with("drive/old")


# --- Shown by the browser ---------------------------------------------------------------------------


def test_a_pdf_is_shown_inline_under_its_own_type(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    _upload(member, winter, "Referat.pdf", b"%PDF-1.4")
    file = File.objects.get(name="Referat.pdf")
    page = member.get(reverse("drive:file", args=[file.uuid])).content.decode()
    assert f'<iframe class="drive-pdf" src="{reverse("drive:inline", args=[file.uuid])}"' in page
    response = member.get(reverse("drive:inline", args=[file.uuid]))
    assert response["Content-Type"] == "application/pdf"  # not the text/plain the upload claimed
    assert response["Content-Disposition"].startswith("inline;")
    assert response["X-Frame-Options"] == "SAMEORIGIN"
    assert _client(people["outsider"]).get(reverse("drive:inline", args=[file.uuid])).status_code == 404


def test_only_the_listed_kinds_are_served_inline(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    _upload(member, _period_folder(group, WINTER), "side.html", b"<script>alert(1)</script>")
    file = File.objects.get(name="side.html")
    assert member.get(reverse("drive:inline", args=[file.uuid])).status_code == 404
    page = member.get(reverse("drive:file", args=[file.uuid])).content.decode()
    assert "alert(1)" not in page


def test_video_and_sound_get_the_browsers_player(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    _upload(member, winter, "Fest.mov", b"x")
    _upload(member, winter, "Sang.m4a", b"x")
    video = member.get(reverse("drive:file", args=[File.objects.get(name="Fest.mov").uuid])).content.decode()
    assert '<video class="drive-video"' in video
    sound = member.get(reverse("drive:file", args=[File.objects.get(name="Sang.m4a").uuid])).content.decode()
    assert '<audio class="drive-audio"' in sound
    response = member.get(reverse("drive:inline", args=[File.objects.get(name="Fest.mov").uuid]))
    assert response["Content-Type"] == "video/quicktime"


# --- Text ------------------------------------------------------------------------------------------


def test_text_shows_its_first_megabyte(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    _upload(member, _period_folder(group, WINTER), "log.txt", b"<b>first</b>\n" + b"x" * preview.TEXT_BYTES)
    page = member.get(reverse("drive:file", args=[File.objects.get(name="log.txt").uuid])).content.decode()
    assert "&lt;b&gt;first&lt;/b&gt;" in page
    assert "Viser den første 1,0" in page


def test_a_danish_excel_csv_is_a_table(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    content = "Navn;Beløb\nØl;120,50\nKaffe;45\n".encode("cp1252")
    _upload(member, _period_folder(group, WINTER), "Kasse.csv", content)
    page = member.get(reverse("drive:file", args=[File.objects.get(name="Kasse.csv").uuid])).content.decode()
    assert '<th scope="col">Beløb</th>' in page
    assert "<td>Øl</td><td>120,50</td>" in page


def test_markdown_is_rendered_without_the_authors_html(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    _upload(
        member, _period_folder(group, WINTER), "LÆSMIG.md", b"# Velkommen\n\n<script>x()</script> **fed**"
    )
    page = member.get(reverse("drive:file", args=[File.objects.get(name="LÆSMIG.md").uuid])).content.decode()
    assert "<strong>fed</strong>" in page
    assert "<script>x()" not in page


def test_a_folders_readme_is_rendered_above_its_list(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    member.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Underskab"})
    _upload(member, winter, "Readme.md", b"# Velkommen\n\n<script>x()</script> **Husk** budgettet")
    page = member.get(reverse("drive:folder", args=[winter.pk])).content.decode()
    assert 'class="drive-readme' in page
    assert page.index("<strong>Husk</strong>") < page.index("Underskab")  # above the folders
    assert "<script>x()" not in page
    assert page.count("Readme.md") >= 2  # still in the list too

    # Only README.md, in any case, and not from the bin.
    member.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Andet"})
    other = Folder.objects.get(parent=winter, name="Andet")
    _upload(member, other, "README.txt", b"x")
    _upload(member, other, "noter.md", b"x")
    assert 'class="drive-readme' not in member.get(reverse("drive:folder", args=[other.pk])).content.decode()
    File.objects.filter(name="Readme.md").update(deleted_at=timezone.now())
    assert 'class="drive-readme' not in member.get(reverse("drive:folder", args=[winter.pk])).content.decode()


def test_a_readme_is_read_from_the_bucket_once(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    _upload(member, winter, "README.md", b"Hej **alle**")
    assert "<strong>alle</strong>" in member.get(reverse("drive:folder", args=[winter.pk])).content.decode()
    with mock.patch.object(preview, "text", side_effect=AssertionError("read again")):
        assert (
            "<strong>alle</strong>" in member.get(reverse("drive:folder", args=[winter.pk])).content.decode()
        )

    # Bytes that cannot be read leave the README out, not the folder.
    FileVersion.objects.filter(file__name="README.md").update(key=services.new_key())
    page = member.get(reverse("drive:folder", args=[winter.pk]))
    assert page.status_code == 200
    assert 'class="drive-readme' not in page.content.decode()


def test_a_utf8_character_cut_by_the_limit_is_left_out() -> None:
    assert preview._decode("blåbær".encode()[:3]) == "bl"
    assert preview._decode("blåbær".encode("cp1252")) == "blåbær"


def test_previews_are_kept_on_the_servers_disk_when_the_cache_is_on(
    people: dict[str, Resident],
    group: Workgroup,
    django_capture_on_commit_callbacks: Callable,
    settings: object,
    tmp_path: object,
) -> None:
    settings.MEDIA_CACHE_DIR = str(tmp_path)  # type: ignore[attr-defined]
    member = _client(people["member"])
    file = _put(
        member, _period_folder(group, WINTER), "Tag.png", _png(40, 20), django_capture_on_commit_callbacks
    )
    response = member.get(reverse("drive:thumbnail", args=[file.uuid]))
    assert response["Cache-Control"] == "private, max-age=86400"
    row = ImagePreview.objects.get()
    assert (tmp_path / "drive" / row.thumbnail_key).exists()  # type: ignore[operator]
