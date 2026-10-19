"""Real, lightweight pictures for the local photo-album demo."""

import random
from datetime import datetime, timedelta
from io import BytesIO

from django.core.files.base import ContentFile
from django.utils.text import slugify

from residents.models import Resident

from .models import Album, Media
from .services import add_tag, upload_media

ALBUMS = [
    ("2026", "Sommerfest", 6),
    # A "/" makes a sub-album, so the demo has a tree to browse — one level deeper than can be made
    # by hand, as a ZIP import would.
    ("2026", "Sommerfest/Fredag", 4),
    ("2026", "Sommerfest/Lørdag", 4),
    ("2026", "Sommerfest/Lørdag/Natten", 3),
    ("2026", "Hyttetur", 6),
    ("2026", "Forårstur", 6),
    ("2025", "Julefrokost", 6),
    ("2025", "Studietur", 2),
    ("2025", "Studietur/Berlin", 5),
    ("2025", "Studietur/Berlin/Museer", 3),
    ("2025", "Studietur/Prag", 4),
    ("Andet", "Hverdagsglimt", 6),
    ("Andet", "Fælleskøkken", 6),
    ("Andet", "Sport", 2),
    ("Andet", "Sport/Fodbold", 4),
    ("Andet", "Sport/Fodbold/Pokalfinale", 5),
    ("Andet", "Sport/Badminton", 3),
    ("Andet", "Paginationstest", 60),
]
# Tags on albums apply to everything below them, so these are spread over every level of the trees
# above: the timeline's tag filter then has direct, inherited and combined hits to show.
ALBUM_TAGS = {
    ("2026", "Sommerfest"): ["sommerfest", "fest"],
    ("2026", "Sommerfest/Lørdag"): ["koncert"],
    ("2026", "Sommerfest/Lørdag/Natten"): ["nattefest"],
    ("2025", "Julefrokost"): ["jul", "fest"],
    ("2025", "Studietur"): ["studietur", "rejse"],
    ("2025", "Studietur/Berlin"): ["berlin"],
    ("2025", "Studietur/Berlin/Museer"): ["museum"],
    ("2025", "Studietur/Prag"): ["prag"],
    ("Andet", "Sport"): ["sport"],
    ("Andet", "Sport/Fodbold"): ["fodbold"],
    ("Andet", "Sport/Fodbold/Pokalfinale"): ["pokal.2025"],
}
# (folder, album path, image index) -> the item's own tags.
MEDIA_TAGS = {
    ("2026", "Sommerfest", 0): ["velkomst"],
    ("2026", "Sommerfest", 4): ["gruppebillede", "portræt"],
    ("2026", "Sommerfest/Fredag", 2): ["fællessang"],
    ("2026", "Sommerfest/Lørdag/Natten", 0): ["dans", "fællessang", "sent-på-aftenen"],
    ("2026", "Hyttetur", 3): ["natur", "sol"],
    ("2026", "Hyttetur", 4): ["gruppebillede"],
    ("2026", "Forårstur", 3): ["natur"],
    ("2025", "Julefrokost", 2): ["fællessang"],
    ("2025", "Julefrokost", 4): ["gruppebillede", "julefrokost_2025"],
    ("2025", "Studietur/Berlin", 1): ["mad"],
    ("2025", "Studietur/Berlin/Museer", 0): ["arkitektur", "kunst"],
    ("2025", "Studietur/Prag", 3): ["natur", "arkitektur"],
    ("Andet", "Hverdagsglimt", 0): ["morgenkaffe"],
    ("Andet", "Hverdagsglimt", 5): ["oprydning", "fælleskøkken"],
    ("Andet", "Fælleskøkken", 1): ["mad", "fælleskøkken"],
    ("Andet", "Sport/Fodbold/Pokalfinale", 4): ["gruppebillede", "sejr"],
    ("Andet", "Sport/Badminton", 0): ["badminton"],
}
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


def _album(folder: str, path: str) -> Album:
    parent: Album | None = None
    for name in path.split("/"):
        parent, _ = Album.objects.get_or_create(folder=folder, name=name, parent=parent)
    assert parent is not None  # noqa: S101 — a path has at least one name  # nosec B101
    return parent


def seed(residents: list[Resident], _now: datetime, _rng: random.Random) -> int:
    if not residents:
        return 0
    made = 0
    for album_index, (folder, album_path, count) in enumerate(ALBUMS):
        album = _album(folder, album_path)
        album_name = album.name
        for name in ALBUM_TAGS.get((folder, album_path), []):
            add_tag(album, name)
        for image_index in range(count):
            filename = f"{slugify(album_name)}-{slugify(CAPTIONS[image_index % len(CAPTIONS)])}-{image_index + 1}.jpg"
            title = filename
            media_tags = MEDIA_TAGS.get((folder, album_path, image_index), [])
            if (existing := album.media.filter(title=title).first()) is not None:
                _tag(existing, media_tags)  # so a re-seed tags what an older seed made
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
            _tag(media, media_tags)
            made += 1
    for media_index, (folder, album_name, caption, upload_age, bin_age) in enumerate(LIFECYCLE_MEDIA):
        album = _album(folder, album_name)
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


def _tag(media: Media, names: list[str]) -> None:
    for name in names:
        add_tag(media, name)


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
