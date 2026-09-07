"""Build the archive's folder tree from a directory of files, hashing each one into the store.

    THE DROPBOX MIGRATION. Run it once per source tree, then again if it is interrupted.

`rclone` the Dropbox export onto a machine with real bandwidth first - 2 TB over the kollegium's
line is days, and ingress to Hetzner is free but time is not. Then point this at it: it walks the
tree, hashes each file, uploads any bytes the bucket does not already have, and creates the folder
and file rows that make them browsable.

**Idempotent, and that is the whole reason it hashes rather than trusting paths.** A 2 TB import
*will* be interrupted - a laptop lid, a dropped connection, a full disk. Re-running skips every
object already in the bucket and every row already in the database, so the second run costs a walk
and a read rather than another 2 TB. It is also why the same photograph in four Dropbox folders
becomes four rows and one object.

**Nothing is deleted, ever.** A file that has disappeared from the source since the last run is left
alone: this is an importer, not a synchroniser, and "the source no longer has it" is not evidence
that the kollegium wanted it gone.

Access is NOT set here beyond `--workgroup`. Everything lands under one root, and who may see it is
whatever that root says - see arkiv/access.py. Sorting 2 TB into embedsgrupper is a job for people
who know what the folders mean, done afterwards in the admin.
"""

import argparse
import mimetypes
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from arkiv.models import ARCHIVE_PREFIX, ArchiveFile, ArchiveFolder, object_key
from arkiv.services import sha256_of
from arkiv.storage import get_store
from core.models import Workgroup

# Names that are an artefact of the exporting filesystem rather than anything a resident filed.
SKIP_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", ".dropbox", ".dropbox.attr"})


class Command(BaseCommand):
    help = "Importer et bibliotek (fx en Dropbox-eksport) ind i Arkivet. Sletter aldrig noget."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("source", type=str, help="Directory to import")
        parser.add_argument(
            "--root",
            type=str,
            required=True,
            help="Name of the top-level Arkiv folder everything lands under.",
        )
        parser.add_argument(
            "--workgroup",
            type=str,
            default="",
            help="Embedsgruppe that owns the root (exact Workgroup name). Empty = all residents.",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        source = Path(str(opts["source"])).expanduser()
        if not source.is_dir():
            raise CommandError(f"not a directory: {source}")

        workgroup = None
        if opts["workgroup"]:
            try:
                workgroup = Workgroup.objects.get(name=str(opts["workgroup"]))
            except Workgroup.DoesNotExist as exc:
                names = ", ".join(Workgroup.objects.values_list("name", flat=True))
                raise CommandError(f"no such embedsgruppe. Known: {names}") from exc

        dry = bool(opts["dry_run"])
        store = get_store()

        # Sorted so two runs walk in the same order, which makes an interrupted import resume
        # somewhere predictable and makes the log diffable.
        files = sorted(p for p in source.rglob("*") if p.is_file() and p.name not in SKIP_NAMES)
        if not files:
            self.stdout.write("Nothing to import.")
            return

        # ASKED ONCE, NOT PER FILE. Both of these were per-file round trips, and at the scale this
        # command exists for - a Dropbox export of several hundred thousand photographs - that was
        # the whole runtime rather than a detail. A file three directories deep cost three folder
        # queries plus a HEAD; the same folders were re-asked thousands of times, and the HEAD was
        # answered "no" for every object on a first run.
        self._folders: dict[tuple[int | None, str], ArchiveFolder] = {}
        stored_keys = store.list_keys(f"{ARCHIVE_PREFIX}/")
        self.stdout.write(f"{len(files)} file(s) to consider; {len(stored_keys)} already in the store.")

        root = self._folder(None, str(opts["root"]), workgroup, dry)
        stats = {"rows": 0, "uploaded": 0, "deduped": 0, "skipped": 0}

        # One query for every name already filed, instead of an existence check per file. Keyed by
        # (folder, name) because that is what the unique constraint is.
        existing_rows: set[tuple[int, str]] = set()
        if not dry:
            existing_rows = set(ArchiveFile.objects.alive().values_list("folder_id", "name"))

        for path in files:
            rel = path.relative_to(source)
            folder = root
            for part in rel.parts[:-1]:
                folder = self._folder(folder, part, None, dry)

            if folder is not None and (folder.pk, rel.name) in existing_rows:
                stats["skipped"] += 1
                continue

            with path.open("rb") as fh:
                digest = sha256_of(fh)

            key = object_key(digest)
            if key in stored_keys:
                # An earlier run put it there, or another folder holds the same bytes. Both mean
                # there is nothing to upload - which is what makes a re-run cheap.
                stats["deduped"] += 1
            else:
                if not dry:
                    with path.open("rb") as fh:
                        store.save(key, fh)
                stored_keys.add(key)  # so a duplicate later in this run is not uploaded twice
                stats["uploaded"] += 1

            if not dry and folder is not None:
                ArchiveFile.objects.create(
                    folder=folder,
                    name=rel.name,
                    sha256=digest,
                    size=path.stat().st_size,
                    content_type=mimetypes.guess_type(rel.name)[0] or "",
                )
                existing_rows.add((folder.pk, rel.name))
            stats["rows"] += 1
            if stats["rows"] % 500 == 0:
                self.stdout.write(f"  … {stats['rows']} of {len(files)}")

        prefix = "[dry-run] " if dry else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Arkiv: {stats['rows']} file(s) imported "
                f"({stats['uploaded']} uploaded, {stats['deduped']} already in the bucket), "
                f"{stats['skipped']} already imported and unchanged."
            )
        )
        if not dry:
            self.stdout.write("Set folder ownership in the admin; everything landed under the root.")

    def _folder(
        self, parent: ArchiveFolder | None, name: str, workgroup: Workgroup | None, dry: bool
    ) -> ArchiveFolder | None:
        """Get or make one folder. Returns None on a dry run, which the caller tolerates.

        NOT @transaction.atomic, deliberately. It wraps a single create(), which is already atomic,
        and the decorator issues a SAVEPOINT/RELEASE pair on every call - including the cache hits,
        which are almost all of them. That was four extra round trips per file, or about 800,000
        across a Dropbox export, to protect nothing.

        Written as look-then-create rather than get_or_create because the lookup has to be scoped to
        LIVE rows - a folder somebody soft-deleted must not silently become the destination of the
        next import - and `deleted_at__isnull=True` is a lookup, which get_or_create would try to
        pass to the constructor. A one-off import has no concurrent writer to race with.
        """
        if dry:
            return None
        cache_key = (parent.pk if parent else None, name)
        cached = self._folders.get(cache_key)
        if cached is not None:
            return cached

        existing = ArchiveFolder.objects.alive().filter(parent=parent, name=name).first()
        if existing is None:
            # save() resolves effective_workgroup from `workgroup` or the parent; a folder created
            # here has no descendants yet, so there is nothing for reassign_subtree to do.
            existing = ArchiveFolder.objects.create(parent=parent, name=name, workgroup=workgroup)
        self._folders[cache_key] = existing
        return existing
