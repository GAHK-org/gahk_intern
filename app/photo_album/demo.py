"""Real, lightweight pictures for the local photo-album demo."""

import random
from datetime import datetime
from io import BytesIO

from django.core.files.base import ContentFile

from residents.models import Resident

from .models import Album
from .services import upload_media

ALBUMS = [("2026", "Sommerfest"), ("2026", "Hyttetur"), ("2025", "Julefrokost"), ("Andet", "Hverdagsglimt")]
COLOURS = ["#dd6b43", "#1f6c64", "#d8a32f", "#315b8d", "#8a4e70", "#517544"]


def seed(residents: list[Resident], _now: datetime, _rng: random.Random) -> int:
    if not residents:
        return 0
    made = 0
    for album_index, (folder, album_name) in enumerate(ALBUMS):
        album, _ = Album.objects.get_or_create(folder=folder, name=album_name)
        for image_index in range(4):
            title = f"{album_name} {image_index + 1}"
            if album.media.filter(title=title).exists():
                continue
            upload_media(
                album=album,
                uploaded_file=ContentFile(
                    _image(title, COLOURS[(album_index + image_index) % len(COLOURS)]), name=f"{title}.jpg"
                ),
                resident=residents[0],
                title=title,
                approved=True,
            )
            made += 1
    return made


def _image(title: str, colour: str) -> bytes:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return title.encode()
    image = Image.new("RGB", (1200, 800), colour)
    ImageDraw.Draw(image).text((48, 48), title, fill="white")
    output = BytesIO()
    image.save(output, format="JPEG", quality=80)
    return output.getvalue()
