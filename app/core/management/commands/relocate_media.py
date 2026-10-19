"""Copy referenced legacy image files into MEDIA_ROOT so Django/WhiteNoise can serve them.

The ETL preserved legacy image *paths* (Product.image, RoomConditionScore.image) but not the files.
This copies the actual files from legacy_site/public/<...> into MEDIA_ROOT/<same path> so the stored
references resolve under MEDIA_URL. Idempotent; skips URLs and already-copied files.
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from oelkaelder.models import Product
from rooms.models import RoomConditionScore


def legacy_image_segments(field: str | None) -> list[str]:
    """Normalise a stored legacy image field into MEDIA-relative paths, one per image.

    The field may hold several ';'-separated paths (RoomConditionScore.image), each optionally
    prefixed "public/" or "/public/". Returns the leading-slash-stripped local paths (e.g.
    "public/image/intern/roomimages/110/floor/img.jpg"), skipping blanks and absolute URLs. Must
    match RoomConditionScore.image_urls, which splits on ';' the same way — the previous relocate
    treated the whole ';'-joined blob as one filename, so every multi-image row silently missed.
    """
    out = []
    for part in (field or "").split(";"):
        v = part.strip().lstrip("/")
        if v and not v.startswith(("http://", "https://")):
            out.append(v)
    return out


class Command(BaseCommand):
    help = "Copy referenced legacy image files into MEDIA_ROOT."

    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        legacy_public = Path(settings.BASE_DIR).parent / "legacy_site" / "public"
        media = Path(settings.MEDIA_ROOT)
        stats = {"copied": 0, "missing": 0, "skipped": 0}

        def relocate(field: str | None) -> None:
            for v in legacy_image_segments(field):  # -> "public/image/..."
                src = legacy_public / v.removeprefix("public/")
                dst = media / v  # matches MEDIA_URL + v, i.e. what image_urls generates
                if src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        shutil.copy2(src, dst)
                    stats["copied"] += 1
                else:
                    stats["missing"] += 1

        for p in Product.objects.exclude(image=""):
            relocate(p.image.name)
        for s in RoomConditionScore.objects.exclude(image=""):
            relocate(s.image)

        self.stdout.write(
            self.style.SUCCESS(
                f"Media relocate: copied {stats['copied']}, missing {stats['missing']}, skipped {stats['skipped']}."
            )
        )
