"""Reconcile what is in media storage against what the database actually references.

    THIS COMMAND NEVER DELETES ANYTHING, AND MUST NOT LEARN HOW.

It exists because core.files swallows storage failures — a DELETE over the network fails routinely
and raising would 500 the Den Hurtige feed — so objects leak, and something has to be able to find
them. It is also the check after `migrate_media_to_s3`: "referenced but missing" is the failure mode
that matters there, and it is invisible on a page that renders a broken image.

Reporting rather than sweeping is the whole design, and the reason is the list below. A reference to
an uploaded file can live in SIX places, only one of which is a FileField:

  1. the eight FileFields themselves;
  2. cms.Page.background_image and cms.PageVersion.background_image — CharFields holding the URL
     string, not a name;
  3. /media/ URLs written into cms Page/NewsItem/Event HTML bodies by the admin toolbar;
  4. opslagstavle.Notice.body, as Markdown, recovered via core.markdown.extract_image_names;
  5. rooms.RoomConditionScore.image — a TextField of `;`-separated legacy paths;
  6. rooms.RoomConditionScore.photo (a FileField, so covered by 1, but its legacy sibling is not).

Miss any one of those and an automatic sweep deletes live content, with no undo unless bucket
versioning happens to be on. A human reading a list cannot make that mistake at scale. If this ever
grows a --delete flag, every one of the six has to be re-verified first.
"""

import argparse
import re
from pathlib import Path
from typing import cast
from urllib.parse import unquote

from django.conf import settings
from django.core.files.storage import storages
from django.core.management.base import BaseCommand
from django.db import models

from cms.models import Event, NewsItem, Page, PageVersion
from core.markdown import extract_image_names
from core.storage import MediaS3Storage
from opslagstavle.models import Notice
from rooms.models import RoomConditionScore

# A /media/ reference inside stored HTML or a stored URL column. Deliberately not the REF regex in
# cms.sync_cms_media: that one matches the *legacy* /public/image/ paths, which are a different
# migration and live under static/, not here.
MEDIA_REF = re.compile(r"/media/([^\s\"'()<>]+)")


def _name_from_url(value: str) -> str | None:
    """The storage name inside a stored /media/ URL, or None if it is not one of ours."""
    prefix = settings.MEDIA_URL
    if not value or not value.startswith(prefix):
        return None
    return unquote(value[len(prefix) :])


class Command(BaseCommand):
    help = "Report media objects that nothing references, and references with no object. Read-only."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--limit", type=int, default=50, help="How many names to print per section (0 = all)."
        )

    def handle(self, *args: object, **opts: object) -> None:
        # Rows pointing at an absolute URL rather than a stored file; counted, never chased.
        self.external = 0
        referenced = self._referenced()
        present = self._present()

        missing = sorted(referenced - present)
        orphaned = sorted(present - referenced)
        # `**opts` is typed `object`; argparse guarantees an int here because of type=int.
        limit = cast("int", opts["limit"]) or None

        self.stdout.write(f"Referenced by the database: {len(referenced)}")
        self.stdout.write(f"Present in storage:         {len(present)}")
        if self.external:
            self.stdout.write(
                f"Rows pointing at an absolute URL, not at storage: {self.external} "
                "(legacy oelkaelder product images; nothing to migrate, and nothing missing)"
            )

        # Missing first: a referenced file that is gone is a visible bug on somebody's page, while
        # an orphan costs a fraction of a cent a year.
        if missing:
            self.stdout.write(
                self.style.ERROR(f"\nMISSING — referenced but not in storage ({len(missing)}):")
            )
            for name in missing[:limit]:
                self.stdout.write(f"  {name}")
            if limit and len(missing) > limit:
                self.stdout.write(f"  … and {len(missing) - limit} more")
        else:
            self.stdout.write(self.style.SUCCESS("\nNothing missing — every reference resolves."))

        if orphaned:
            self.stdout.write(
                self.style.WARNING(f"\nORPHANED — in storage but unreferenced ({len(orphaned)}):")
            )
            for name in orphaned[:limit]:
                self.stdout.write(f"  {name}")
            if limit and len(orphaned) > limit:
                self.stdout.write(f"  … and {len(orphaned) - limit} more")
            self.stdout.write(
                "\nReview these by hand before removing any. Freshly uploaded opslag images are "
                "unreferenced on purpose until their post is saved (see opslagstavle.images), so an "
                "orphan list is never a delete list."
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nNo orphans."))

    def _referenced(self) -> set[str]:
        names: set[str] = set()

        # 1. Every FileField on every model, discovered rather than listed, so a new one added to
        #    any app is covered without editing this command.
        #
        #    ABSOLUTE URLS ARE NOT STORAGE NAMES. oelkaelder.Product.image is a FileField whose
        #    legacy rows hold the old site's URL outright ("legacy imageurl", models.py) — around
        #    134 of them in production. Counting those as references made every one of them a
        #    permanent entry in the MISSING list, which is the section that has to stay empty to be
        #    worth reading: an operator who is used to seeing 134 there will not notice the 135th.
        #    relocate_media.legacy_image_segments skips them for the same reason.
        for model in self._concrete_models():
            fields = [f for f in model._meta.get_fields() if isinstance(f, models.FileField)]
            if not fields:
                continue
            for row in model._default_manager.values_list(*[f.name for f in fields]):
                for value in row:
                    if not value:
                        continue
                    if value.startswith(("http://", "https://")):
                        self.external += 1
                        continue
                    names.add(value)

        # 2. The two CharFields holding a URL rather than a name.
        for value in [
            *Page.objects.values_list("background_image", flat=True),
            *PageVersion.objects.values_list("background_image", flat=True),
        ]:
            name = _name_from_url(value or "")
            if name:
                names.add(name)

        # 3. /media/ URLs embedded in admin-authored HTML.
        for body in [
            *Page.objects.values_list("body", flat=True),
            *NewsItem.objects.values_list("body", flat=True),
            *Event.objects.values_list("description", flat=True),
        ]:
            names.update(unquote(m.group(1)) for m in MEDIA_REF.finditer(body or ""))

        # 4. Resident-authored Markdown. The token walk, not a regex — a URL in a code fence is not
        #    a reference, which is the whole reason opslagstavle claims images this way.
        for body in Notice.objects.values_list("body", flat=True):
            names.update(extract_image_names(body or ""))

        # 5. The legacy `;`-separated column, which no FileField discovery will ever find.
        #
        #    Mirror RoomConditionScore.image_urls EXACTLY. It maps a stored path to a name with
        #    nothing but `lstrip("/")`, so `public/image/intern/roomimages/...` keeps its `public/`
        #    prefix and the file really does live at MEDIA_ROOT/public/image/... Stripping that
        #    prefix here — which reads like the obviously right thing to do — makes every migrated
        #    room photo look orphaned, which is exactly the mistake this command exists to not make.
        for blob in RoomConditionScore.objects.values_list("image", flat=True):
            for part in (blob or "").split(";"):
                value = part.strip()
                if not value or value.startswith(("http://", "https://")):
                    continue  # already absolute: not ours, and not in our storage
                names.add(unquote(value.lstrip("/")))

        return {n for n in names if n}

    def _concrete_models(self) -> list[type[models.Model]]:
        from django.apps import apps

        return [m for m in apps.get_models() if not m._meta.abstract and not m._meta.proxy]

    def _present(self) -> set[str]:
        storage = storages["default"]
        if isinstance(storage, MediaS3Storage):
            # Scoped to the media prefix, so the backups sharing this bucket are neither listed nor
            # ever reported as orphans.
            prefix = storage.location.rstrip("/") + "/"
            return {
                obj.key[len(prefix) :]
                for obj in storage.bucket.objects.filter(Prefix=prefix)
                if not obj.key.endswith("/")
            }
        root = Path(settings.MEDIA_ROOT)
        if not root.is_dir():
            return set()
        return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
