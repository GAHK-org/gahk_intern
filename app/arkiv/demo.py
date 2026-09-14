"""Demo content for Arkiv, so a fresh developer database has an archive worth looking at.

Called by `manage.py seed_demo`, like opslagstavle.demo and events.demo. Three things beyond the
roots, each chosen to make one behaviour visible without anyone having to read the access rules:

  * a **nested** folder under Billeder, so the breadcrumb and the subfolder listing do something;
  * a folder under a gated root, so it is obvious that visibility is inherited downwards;
  * a few small files with real bytes in the store, so downloading works rather than 404ing;
  * and for the images, REAL JPEGs with both derived sizes, so the grid and the viewer work.

That last one is not decoration. Every image here used to be a few bytes of text with a .jpg name,
which meant `has_thumbnail` was false for all of them, the listing drew file icons, and the viewer -
the thing the archive is mostly for - could not be opened at all on a developer machine. A feature
you cannot see locally is a feature that gets verified in production.

Deliberately tiny. A flat-colour 1200x800 JPEG is some tens of kilobytes, and `--fresh` runs often
enough that shipping real photographs would be a tax on every run.
"""

import random
from collections.abc import Callable
from datetime import datetime
from io import BytesIO

from residents.models import Resident

from .models import ArchiveFile, ArchiveFolder, object_key, preview_key, thumbnail_key
from .services import ensure_root_folders, sha256_of
from .storage import get_store

# Flat colours, one per demo photograph, so the viewer's paging is visibly doing something.
DEMO_COLOURS = ["#2f6f4e", "#6f4e2f", "#2f4e6f", "#6f2f4e"]

# (folder name, parent root, files) - the parent is looked up by name so this survives reordering.
DEMO_TREE: list[tuple[str, str, list[str]]] = [
    (
        "Sommerfest 2026",
        "Billeder",
        ["gruppebillede.jpg", "teltet.jpg", "morgenmad.jpg", "oprydning.jpg"],
    ),
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
            is_image = not filename.endswith(".pdf")
            # Content derived from the name, so the same demo file hashes the same on every run and
            # two different names never collide into one object.
            body = _image(filename, made) if is_image else f"demo-{folder_name}-{filename}".encode()
            digest = sha256_of(BytesIO(body))
            store.save(object_key(digest), BytesIO(body))
            derived = _derived(body) if is_image else {}
            for key_for, sized in derived.items():
                store.save(key_for(digest), BytesIO(sized))
            ArchiveFile.objects.create(
                folder=folder,
                name=filename,
                sha256=digest,
                size=len(body),
                content_type="application/pdf" if filename.endswith(".pdf") else "image/jpeg",
                uploaded_by=rng.choice(residents) if residents else None,
                has_thumbnail=bool(derived),
                has_preview=bool(derived),
            )
            made += 1
    return made


def _image(filename: str, index: int) -> bytes:
    """A real JPEG, or a few bytes of text if Pillow is not installed.

    Pillow is a dev-only dependency and this is a dev-only command, so it is normally there - but
    `seed_demo` must not become the reason someone cannot run the project, so a missing Pillow
    degrades to what this used to do rather than raising. The listing then draws file icons, which
    is exactly the state this change exists to get away from, so it says so.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return f"demo-{filename}".encode()

    image = Image.new("RGB", (1200, 800), DEMO_COLOURS[index % len(DEMO_COLOURS)])
    # The name drawn on the picture, so paging through the viewer visibly moves rather than showing
    # four rectangles that could all be the same one.
    ImageDraw.Draw(image).text((40, 40), filename, fill="white")
    out = BytesIO()
    image.save(out, format="JPEG", quality=80)
    return out.getvalue()


def _derived(body: bytes) -> dict[Callable[[str], str], bytes]:
    """The thumbnail and preview for a demo image, keyed by the function that names their object.

    The same two sizes the browser makes on upload and `make_arkiv_thumbnails` makes for the
    backlog - a third implementation, which is a real cost, and the reason it is worth it is that
    without them a developer sees neither the grid nor the viewer.
    """
    try:
        from PIL import Image
    except ImportError:
        return {}

    sizes: dict[Callable[[str], str], bytes] = {}
    for key_for, dim, quality in ((thumbnail_key, 320, 70), (preview_key, 1600, 82)):
        image = Image.open(BytesIO(body))
        image.thumbnail((dim, dim))
        out = BytesIO()
        image.save(out, format="JPEG", quality=quality)
        sizes[key_for] = out.getvalue()
    return sizes
