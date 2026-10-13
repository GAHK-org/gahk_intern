"""Real, lightweight pictures for the local photo-album demo."""

import random
from datetime import datetime, timedelta
from io import BytesIO

from django.core.files.base import ContentFile
from django.utils.text import slugify

from residents.models import Resident

from .models import Album
from .services import upload_media

ALBUMS = [
    ("2026", "Sommerfest", 6),
    ("2026", "Hyttetur", 6),
    ("2026", "Forårstur", 6),
    ("2025", "Julefrokost", 6),
    ("Andet", "Hverdagsglimt", 6),
    ("Andet", "Fælleskøkken", 6),
    ("Andet", "Paginationstest", 60),
]
COLOURS = ["#dd6b43", "#1f6c64", "#d8a32f", "#315b8d", "#8a4e70", "#517544"]
DIMENSIONS = [(1600, 900), (900, 1600), (1200, 800), (1000, 1000), (1600, 1067), (1067, 1600)]
CAPTIONS = [
    "Ankomst og velkomst",
    "Langbordet er dækket",
    "Fællessang sent på aftenen",
    "Pause i solen",
    "Holdet samlet",
    "Oprydning dagen derpå",
]
LIFECYCLE_MEDIA = [
    ("Andet", "Testdata: sletning", "Uploadet for 40 minutter", timedelta(minutes=40), None),
    ("Andet", "Testdata: sletning", "Uploadet for 14 dage", timedelta(days=14), None),
    ("Andet", "Testdata: sletning", "Uploadet for 31 dage", timedelta(days=31), None),
    ("Andet", "Papirkurv: nylig", "Uploadet for 25 minutter", timedelta(minutes=25), timedelta(minutes=5)),
    ("Andet", "Papirkurv: nylig", "Uploadet for 12 dage", timedelta(days=12), timedelta(days=2)),
    (
        "Andet",
        "Papirkurv: gammel",
        "Uploadet for 45 dage - i papirkurven i 31 dage",
        timedelta(days=45),
        timedelta(days=31),
    ),
    ("2025", "Låst demoalbum", "Sidste upload for 91 dage", timedelta(days=91), None),
]


def seed(residents: list[Resident], _now: datetime, _rng: random.Random) -> int:
    if not residents:
        return 0
    made = 0
    for album_index, (folder, album_name, count) in enumerate(ALBUMS):
        album, _ = Album.objects.get_or_create(folder=folder, name=album_name)
        for image_index in range(count):
            filename = f"{slugify(album_name)}-{slugify(CAPTIONS[image_index % len(CAPTIONS)])}-{image_index + 1}.jpg"
            title = filename
            if album.media.filter(title=title).exists():
                continue
            media = upload_media(
                album=album,
                uploaded_file=ContentFile(
                    _image(
                        title,
                        COLOURS[(album_index + image_index) % len(COLOURS)],
                        DIMENSIONS[(album_index + image_index) % len(DIMENSIONS)],
                    ),
                    name=filename,
                ),
                resident=residents[(album_index + image_index) % len(residents)],
                title=title,
                approved=True,
            )
            media.added_at = _now - timedelta(days=album_index * 4 + image_index)
            media.save(update_fields=["added_at"])
            made += 1
    for media_index, (folder, album_name, caption, upload_age, bin_age) in enumerate(LIFECYCLE_MEDIA):
        album, _ = Album.objects.get_or_create(folder=folder, name=album_name)
        filename = f"{slugify(caption)}.jpg"
        title = filename
        if album.media.filter(title=title).exists():
            continue
        media = upload_media(
            album=album,
            uploaded_file=ContentFile(
                _image(
                    title,
                    COLOURS[media_index % len(COLOURS)],
                    DIMENSIONS[media_index % len(DIMENSIONS)],
                ),
                name=filename,
            ),
            resident=residents[media_index % len(residents)],
            title=title,
            approved=True,
        )
        media.added_at = _now - upload_age
        if bin_age is not None:
            media.deleted_at = _now - bin_age
            media.deleted_by = residents[media_index % len(residents)]
            media.save(update_fields=["added_at", "deleted_at", "deleted_by"])
        else:
            media.save(update_fields=["added_at"])
        made += 1
    return made


def _image(title: str, colour: str, dimensions: tuple[int, int]) -> bytes:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return title.encode()
    image = Image.new("RGB", dimensions, colour)
    ImageDraw.Draw(image).text((48, 48), title, fill="white")
    output = BytesIO()
    image.save(output, format="JPEG", quality=80)
    return output.getvalue()
