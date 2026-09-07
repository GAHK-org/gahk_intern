"""Demo content for Arkiv, so a fresh developer database has an archive worth looking at.

Called by `manage.py seed_demo`, like opslagstavle.demo and events.demo. Three things beyond the
roots, each chosen to make one behaviour visible without anyone having to read the access rules:

  * a **nested** folder under Billeder, so the breadcrumb and the subfolder listing do something;
  * a folder under a gated root, so it is obvious that visibility is inherited downwards;
  * a few small files with real bytes in the store, so downloading works rather than 404ing.

Deliberately tiny files. The point is that the listing and the download path are exercised, not that
the demo ships megabytes - and `--fresh` runs often enough that writing real photographs would be a
tax on every run.
"""

import random
from datetime import datetime
from io import BytesIO

from residents.models import Resident

from .models import ArchiveFile, ArchiveFolder, object_key
from .services import ensure_root_folders, sha256_of
from .storage import get_store

# (folder name, parent root, files) - the parent is looked up by name so this survives reordering.
DEMO_TREE: list[tuple[str, str, list[str]]] = [
    ("Sommerfest 2026", "Billeder", ["gruppebillede.jpg", "teltet.jpg"]),
    ("Husorden", "Fælles dokumenter", ["husorden.pdf"]),
    ("Regnskab 2026", "Regnskabsgruppen", ["kvartalsrapport.pdf"]),
]


def seed(residents: list[Resident], now: datetime, rng: random.Random) -> int:
    """Create the roots plus a little content. Returns the number of files made."""
    ensure_root_folders()

    store = get_store()
    made = 0
    for folder_name, parent_name, filenames in DEMO_TREE:
        parent = ArchiveFolder.objects.alive().filter(parent=None, name=parent_name).first()
        if parent is None:
            # The embedsgruppe does not exist in this database - fine, skip that branch.
            continue
        folder, _ = ArchiveFolder.objects.get_or_create(parent=parent, name=folder_name)
        for filename in filenames:
            if ArchiveFile.objects.alive().filter(folder=folder, name=filename).exists():
                continue
            # Content derived from the name, so the same demo file hashes the same on every run and
            # two different names never collide into one object.
            body = f"demo-{folder_name}-{filename}".encode()
            digest = sha256_of(BytesIO(body))
            store.save(object_key(digest), BytesIO(body))
            ArchiveFile.objects.create(
                folder=folder,
                name=filename,
                sha256=digest,
                size=len(body),
                content_type="application/pdf" if filename.endswith(".pdf") else "image/jpeg",
                uploaded_by=rng.choice(residents) if residents else None,
            )
            made += 1
    return made
