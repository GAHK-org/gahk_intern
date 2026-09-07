"""Operations that touch more than one row, kept out of the models and out of the views.

Two of them, and both exist because content addressing and a denormalised owner each buy something
at the cost of an invariant that has to be maintained deliberately.
"""

import hashlib
from typing import BinaryIO

from django.db import transaction

from core.models import Workgroup

from .models import ArchiveFile, ArchiveFolder

# Hashing reads the whole file, so it reads it in pieces. 1 MiB is large enough that the syscall
# overhead disappears against a 2 GB video and small enough to stay off the request path's heap.
HASH_CHUNK = 1024 * 1024


def sha256_of(fileobj: BinaryIO) -> str:
    """Hex digest, from wherever the stream currently is. Leaves the stream at EOF."""
    digest = hashlib.sha256()
    for chunk in iter(lambda: fileobj.read(HASH_CHUNK), b""):
        digest.update(chunk)
    return digest.hexdigest()


@transaction.atomic
def reassign_subtree(folder: ArchiveFolder) -> int:
    """Re-resolve `effective_workgroup` for every descendant of `folder`. Returns rows touched.

    `ArchiveFolder.save()` keeps one folder in step; this is the other half. Moving a folder, or
    giving one an embedsgruppe it did not have, changes what every folder underneath it inherits -
    and `visible_folders` reads only the denormalised column, so a subtree left stale is not a
    cosmetic inconsistency. It is a folder that is either invisible to the people who own it or,
    worse, visible to people who do not.

    Level by level rather than one recursive CTE: the tree is a few deep, each level is one indexed
    query on `parent_id`, and the resolution rule (own workgroup wins, else the parent's resolved
    one) is the same sentence here as it is in the model. Atomic because a half-applied reassignment
    is exactly the state that leaks.
    """
    touched = 0
    frontier = [folder]
    while frontier:
        parents = {f.pk: f for f in frontier}
        children = list(ArchiveFolder.objects.filter(parent_id__in=parents).select_related("parent"))
        updates = []
        for child in children:
            # parent_id is non-None by construction: these rows came from filter(parent_id__in=...).
            parent = parents[child.parent_id] if child.parent_id is not None else None
            inherited = (
                child.workgroup_id
                if child.workgroup_id is not None
                else (parent.effective_workgroup_id if parent is not None else None)
            )
            if child.effective_workgroup_id != inherited:
                child.effective_workgroup_id = inherited
                updates.append(child)
        if updates:
            ArchiveFolder.objects.bulk_update(updates, ["effective_workgroup"])
            touched += len(updates)
        # Descend through every child, not only the changed ones: an unchanged folder can still have
        # descendants that need re-resolving beneath it.
        frontier = children
    return touched


def unreferenced_keys(hashes: set[str]) -> set[str]:
    """Of `hashes`, those no *live* row references any more - the ones whose bytes may go.

    The question content addressing forces: two rows in different folders share one object, so
    deleting a row can never delete the object on its own. Ask this instead, and only after the rows
    are gone.

    Soft-deleted rows count as references. A file in the undo window still needs its bytes, or the
    undo restores a row pointing at nothing.
    """
    from .models import object_key

    still_used = set(
        ArchiveFile.objects.filter(sha256__in=hashes).values_list("sha256", flat=True).distinct()
    )
    return {object_key(h) for h in hashes - still_used}


# Roots that belong to the whole house. Everyone who can reach Arkiv can read AND upload here -
# access.can_write follows can_read deliberately, so there is no separate step to grant it.
SHARED_ROOTS = ["Billeder", "Fælles dokumenter"]


def ensure_root_folders() -> tuple[list[str], list[str]]:
    """Create the archive's top level if it is missing. Returns (created, already there).

    Shared by `manage.py seed_arkiv_roots` and by the demo seeder, so a fresh developer database and
    a real deployment get the same shape rather than two versions of it that drift.

    **One folder per Workgroup, owned by that Workgroup** - every row, not only the nine that map to
    a Role. Bladet, Haven, Festudvalget and Vinklubben carry no privilege and are still
    embedsgrupper with members, and access resolves through `Residency`, never through a role. A
    group with no folder has nowhere of its own to file anything, which is the Drive problem being
    replaced.

    Scoped to LIVE rows, and it never touches an existing folder: a root somebody deliberately
    soft-deleted stays deleted, and one Inspektionen has re-pointed by hand is not argued with.
    """
    created: list[str] = []
    existing: list[str] = []

    def ensure(name: str, workgroup: Workgroup | None) -> None:
        if ArchiveFolder.objects.alive().filter(parent=None, name=name).exists():
            existing.append(name)
            return
        created.append(name)
        ArchiveFolder.objects.create(parent=None, name=name, workgroup=workgroup)

    for name in SHARED_ROOTS:
        ensure(name, None)
    # Ordered so a re-run logs the same way twice.
    for workgroup in Workgroup.objects.order_by("name"):
        ensure(workgroup.name, workgroup)
    return created, existing
