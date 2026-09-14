"""audit_media must recognise every place a media reference can hide.

The command is the only thing that can find leaked objects, and the only check that
migrate_media_to_s3 left nothing behind. Its danger is asymmetric: a missed reference source turns a
live file into something a human is invited to delete, and there is no undo unless bucket versioning
happens to be on.

So this file is a list of the six sources, one test each, each asserting the *same* thing — the file
is not reported as an orphan. Add a seventh place a name can be stored and a seventh test belongs
here, or the sweep-by-hand this command feeds becomes unsafe.
"""

from io import StringIO
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.utils import timezone

from cms.models import CmsImage, Event, NewsItem, Page, PageVersion
from core.models import Room
from residents.models import Resident
from rooms.models import RoomCondition, RoomConditionScore, RoomCriterion

pytestmark = pytest.mark.django_db


@pytest.fixture
def media_tmp(settings: object, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    return tmp_path


def audit() -> str:
    out = StringIO()
    call_command("audit_media", "--limit", "0", stdout=out)
    return out.getvalue()


def drop_a_file(media_tmp: Path, name: str) -> str:
    """Put a file in storage under `name` without going through any model."""
    path = media_tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"pngbytes")
    return name


def test_a_file_nothing_points_at_is_reported(media_tmp: Path) -> None:
    """The control: without it, a test that finds no orphans proves nothing."""
    drop_a_file(media_tmp, "opslag/2026/09/nobody-wants-me.png")

    report = audit()

    assert "opslag/2026/09/nobody-wants-me.png" in report
    assert "ORPHANED" in report


def test_source_1_a_filefield(media_tmp: Path) -> None:
    image = CmsImage.objects.create(file=SimpleUploadedFile("x.png", b"pngbytes", "image/png"))

    assert image.file.name not in audit()


def test_source_2_a_background_image_url_column(media_tmp: Path) -> None:
    """Page.background_image is a CharField holding the URL, not a FileField holding a name."""
    name = drop_a_file(media_tmp, "cms/2019/03/baggrund.jpg")
    Page.objects.create(slug="historie", header="Historie", background_image=f"/media/{name}")

    assert name not in audit()


def test_source_2b_a_page_version_background(media_tmp: Path) -> None:
    """Versions keep their own copy of the column, and a rollback would resurrect the reference."""
    name = drop_a_file(media_tmp, "cms/2019/03/gammel-baggrund.jpg")
    page = Page.objects.create(slug="p", header="P")
    PageVersion.objects.create(page=page, slug="p", header="P", background_image=f"/media/{name}")

    assert name not in audit()


@pytest.mark.parametrize("model_field", ["page", "news", "event"])
def test_source_3_a_media_url_inside_stored_html(media_tmp: Path, model_field: str) -> None:
    """The CMS toolbar writes <img src="/media/..."> straight into the body."""
    name = drop_a_file(media_tmp, f"cms/2020/01/{model_field}.jpg")
    html = f'<p>Se <img src="/media/{name}" alt=""></p>'
    if model_field == "page":
        Page.objects.create(slug="s", header="S", body=html)
    elif model_field == "news":
        NewsItem.objects.create(title="N", body=html, published_at=timezone.now())
    else:
        Event.objects.create(title="E", description=html, starts_on=timezone.now().date())

    assert name not in audit()


def test_source_4_markdown_in_an_opslag(media_tmp: Path, make_resident: object) -> None:
    """Recovered by walking the token stream, so a code fence is correctly not a reference."""
    from opslagstavle.models import Notice

    author: Resident = make_resident(email="a@gahk.dk")  # type: ignore[operator]
    name = drop_a_file(media_tmp, "opslag/2026/09/fest.jpg")
    Notice.objects.create(author=author, category="nyt", body=f"Fest!\n\n![](/media/{name})")

    assert name not in audit()


def test_source_5_the_legacy_semicolon_separated_column(media_tmp: Path) -> None:
    """The trap. RoomConditionScore.image_urls maps the stored path with nothing but lstrip("/"),
    so `public/image/...` KEEPS its public/ prefix and the file really is at that name. Stripping
    the prefix here would make every migrated room photo look deletable."""
    first = drop_a_file(media_tmp, "public/image/intern/roomimages/112/skab/image.jpg")
    second = drop_a_file(media_tmp, "public/image/intern/roomimages/112/gulv/image.jpg")
    room = Room.objects.create(legacy_index=112, number=112, floor="1", side="mod gaden")
    condition = RoomCondition.objects.create(
        room=room, recorded_by_name="Inspektionen", recorded_at=timezone.now()
    )
    criterion = RoomCriterion.objects.create(code="floor", name="Gulv", options=5)
    RoomConditionScore.objects.create(
        condition=condition,
        criterion=criterion,
        score=3,
        # Exactly the shape the ETL produced: mixed leading slashes, semicolon separated.
        image=f"/{first};{second}",
    )

    report = audit()

    assert first not in report
    assert second not in report


def test_source_6_a_room_photo_filefield(media_tmp: Path) -> None:
    room = Room.objects.create(legacy_index=7, number=7, floor="stuen", side="mod gaarden")
    condition = RoomCondition.objects.create(
        room=room, recorded_by_name="Inspektionen", recorded_at=timezone.now()
    )
    criterion = RoomCriterion.objects.create(code="wall", name="Vaeg", options=5)
    score = RoomConditionScore.objects.create(
        condition=condition,
        criterion=criterion,
        score=4,
        photo=SimpleUploadedFile("v.jpg", b"jpegbytes", "image/jpeg"),
    )

    assert score.photo.name not in audit()


def test_a_reference_with_no_file_is_reported_as_missing(media_tmp: Path) -> None:
    """The failure mode that matters after migrate_media_to_s3, and the one a page hides: a broken
    image still returns 200."""
    Page.objects.create(slug="x", header="X", background_image="/media/cms/gone.jpg")

    report = audit()

    assert "MISSING" in report
    assert "cms/gone.jpg" in report


def test_a_filefield_holding_an_absolute_url_is_not_reported_as_missing(media_tmp: Path) -> None:
    """oelkaelder.Product.image is a FileField whose legacy rows hold the old site's URL outright
    ("legacy imageurl"), ~134 of them in production. Treating those as storage names put a
    permanent 134-line MISSING section in every report - and MISSING is the section that has to
    stay empty to be worth reading, since it is the go/no-go gate after migrate_media_to_s3.
    An operator used to seeing 134 there will not notice the 135th."""
    from oelkaelder.models import Product

    Product.objects.create(
        name="Peanuts",
        price_ore=1000,
        image="https://gahk.dk/public/image/intern/oel/peanuts.jpg",
    )

    report = audit()

    assert "MISSING" not in report
    assert "Nothing missing" in report
    assert "absolute URL" in report


def test_a_real_stored_product_image_is_still_referenced(media_tmp: Path) -> None:
    """The other half: skipping absolute URLs must not skip ordinary uploads on the same field."""
    from oelkaelder.models import Product

    name = drop_a_file(media_tmp, "oel/tuborg.jpg")
    Product.objects.create(name="Tuborg", price_ore=1200, image=name)

    assert name not in audit()
