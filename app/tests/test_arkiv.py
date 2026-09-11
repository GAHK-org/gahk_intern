"""Arkiv: the folder tree, and above all who can see what.

Most of this file is access control, because that is the part where being wrong is expensive rather
than annoying. A bug in the breadcrumb is a bad afternoon; a bug in `visible_folders` shows
Regnskabsgruppen's documents to the whole kollegium, and nothing on the page would look wrong.

Two properties are asserted repeatedly and on purpose:

  * a folder you may not see is ABSENT (404), never forbidden (403) - a 403 confirms the folder
    exists, which is the fact a private folder is hiding;
  * visibility is inherited DOWNWARDS through `effective_workgroup`, so a private subfolder inside a
    public parent stays private even when its id is guessed.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from django.test import Client

from arkiv import access
from arkiv.models import ArchiveFile, ArchiveFolder, object_key, thumbnail_key
from arkiv.services import reassign_subtree, sha256_of, unreferenced_keys
from arkiv.storage import get_store
from core.models import Room, Workgroup
from residents.models import Residency, Resident, Role, active_period

pytestmark = pytest.mark.django_db

ROOT_URL = "/intern/arkiv/"


@pytest.fixture(autouse=True)
def _open_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arkiv ships behind a rollout gate. These tests are about folder visibility, which is a
    different question, so the gate is opened for all of them - and closed again in the two tests
    that are specifically about it."""
    monkeypatch.setattr(access, "ACCESS_ROLES", None)


@pytest.fixture
def workgroups() -> tuple[Workgroup, Workgroup]:
    """Two embedsgrupper: one that exists for real, one invented.

    get_or_create because core/migrations/0003 seeds the real ones - "Regnskabsgruppen" is already
    in every database, and creating it again violates the unique name. Using the real name matters:
    it is the group whose folders this feature must actually keep private.
    """
    regnskab, _ = Workgroup.objects.get_or_create(name="Regnskabsgruppen")
    fest, _ = Workgroup.objects.get_or_create(name="Festudvalget")
    return regnskab, fest


# Above the rooms the migrations seed (1..61) and the literals used elsewhere in this file, and well
# under the 32767 a PositiveSmallIntegerField holds.
_room_seq = iter(range(1_000, 30_000))


@pytest.fixture
def resident_in(make_resident: Callable) -> Callable[..., Resident]:
    """A resident placed in a workgroup for the ACTIVE period - which is what access reads.

    Residency needs a room, so one is made per resident, numbered from a counter.

    NOT from the resident's pk, which is what this did first. Both Room columns are
    PositiveSmallIntegerField (max 32767) while the pk sequence is a bigserial that keeps climbing
    across every run sharing a `--reuse-db` database - so the fixture worked for months and then
    began raising "smallint out of range" on every test in this file at once, on one developer's
    machine only, with CI green because CI builds the database fresh. `_room_seq` is the pattern the
    rest of the suite already uses.
    """

    def _make(email: str, workgroup: Workgroup | None = None) -> Resident:
        resident = make_resident(email=email)
        year, month = active_period()
        n = next(_room_seq)
        room = Room.objects.create(legacy_index=n, number=n, floor="1", side="mod gaden")
        Residency.objects.create(resident=resident, room=room, workgroup=workgroup, year=year, month=month)
        return resident

    return _make


def login(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


# --- the denormalised owner ------------------------------------------------------------------------


def test_a_root_folder_with_no_group_is_owned_by_nobody(workgroups: tuple) -> None:
    folder = ArchiveFolder.objects.create(name="Billeder")

    assert folder.effective_workgroup_id is None


def test_a_subfolder_inherits_its_parents_group(workgroups: tuple) -> None:
    """The property every access check depends on: ownership flows downwards, resolved on write."""
    regnskab, _ = workgroups
    root = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    child = ArchiveFolder.objects.create(name="2026", parent=root)
    grandchild = ArchiveFolder.objects.create(name="Bilag", parent=child)

    assert child.effective_workgroup_id == regnskab.pk
    assert grandchild.effective_workgroup_id == regnskab.pk


def test_a_subfolder_can_narrow_but_inherits_otherwise(workgroups: tuple) -> None:
    _, fest = workgroups
    public = ArchiveFolder.objects.create(name="Billeder")
    narrowed = ArchiveFolder.objects.create(name="Internt", parent=public, workgroup=fest)
    under = ArchiveFolder.objects.create(name="Raa", parent=narrowed)

    assert narrowed.effective_workgroup_id == fest.pk
    assert under.effective_workgroup_id == fest.pk


def test_reassigning_a_parent_re_resolves_the_whole_subtree(workgroups: tuple) -> None:
    """The invariant `save()` alone cannot keep.

    visible_folders reads ONLY the denormalised column, so a subtree left stale after a parent
    changes hands is not a cosmetic inconsistency - it is folders invisible to the people who own
    them, or visible to people who do not.
    """
    regnskab, _ = workgroups
    root = ArchiveFolder.objects.create(name="Arkiv")
    mid = ArchiveFolder.objects.create(name="2026", parent=root)
    leaf = ArchiveFolder.objects.create(name="Bilag", parent=mid)
    assert leaf.effective_workgroup_id is None

    root.workgroup = regnskab
    root.save()
    touched = reassign_subtree(root)

    leaf.refresh_from_db()
    mid.refresh_from_db()
    assert touched == 2
    assert mid.effective_workgroup_id == regnskab.pk
    assert leaf.effective_workgroup_id == regnskab.pk


def test_reassignment_leaves_a_subfolder_with_its_own_group_alone(workgroups: tuple) -> None:
    """An explicit owner wins over an inherited one, at every depth."""
    regnskab, fest = workgroups
    root = ArchiveFolder.objects.create(name="Arkiv")
    own = ArchiveFolder.objects.create(name="Fest", parent=root, workgroup=fest)
    under = ArchiveFolder.objects.create(name="2026", parent=own)

    root.workgroup = regnskab
    root.save()
    reassign_subtree(root)

    own.refresh_from_db()
    under.refresh_from_db()
    assert own.effective_workgroup_id == fest.pk
    assert under.effective_workgroup_id == fest.pk


# --- who sees what ----------------------------------------------------------------------------------


def test_a_folder_with_no_group_is_visible_to_every_resident(resident_in: Callable) -> None:
    ArchiveFolder.objects.create(name="Billeder")
    outsider = resident_in("a@gahk.dk", None)

    assert access.visible_folders(outsider).count() == 1


def test_a_group_folder_is_invisible_to_a_non_member(resident_in: Callable, workgroups: tuple) -> None:
    regnskab, fest = workgroups
    ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    outsider = resident_in("a@gahk.dk", fest)

    assert not access.visible_folders(outsider).exists()


def test_a_group_folder_is_visible_to_a_current_member(resident_in: Callable, workgroups: tuple) -> None:
    regnskab, _ = workgroups
    ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    member = resident_in("a@gahk.dk", regnskab)

    assert access.visible_folders(member).count() == 1


def test_membership_is_current_not_historical(resident_in: Callable, workgroups: tuple) -> None:
    """THE DECISION, and the regression test for it.

    Access reads Residency for active_period(). A resident who was in Regnskabsgruppen last year and
    is not now cannot read its documents - which is the point for finances, and the reason anything
    meant to outlive a rotation belongs in a folder with no workgroup.
    """
    regnskab, fest = workgroups
    ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    former = resident_in("a@gahk.dk", fest)
    year, month = active_period()
    room = Room.objects.create(legacy_index=900, number=900, floor="2", side="mod gaarden")
    Residency.objects.create(resident=former, room=room, workgroup=regnskab, year=year - 1, month=month)

    assert not access.visible_folders(former).exists()


def test_a_resident_with_no_residency_still_sees_the_shared_archive(
    make_resident: Callable, workgroups: tuple
) -> None:
    """An alumnus, or someone between maanedslister: no residency row at all. That must mean "no
    group folders", not "no folders" and not "every folder"."""
    regnskab, _ = workgroups
    ArchiveFolder.objects.create(name="Billeder")
    ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    stranger = make_resident(email="alum@gahk.dk")

    visible = access.visible_folders(stranger)

    assert [f.name for f in visible] == ["Billeder"]


def test_a_private_subfolder_of_a_public_parent_stays_private(
    resident_in: Callable, workgroups: tuple
) -> None:
    """The case denormalisation exists to make cheap, and the one a parent-only check would miss."""
    regnskab, fest = workgroups
    public = ArchiveFolder.objects.create(name="Billeder")
    private = ArchiveFolder.objects.create(name="Bilag", parent=public, workgroup=regnskab)
    outsider = resident_in("a@gahk.dk", fest)

    assert [f.name for f in access.visible_folders(outsider)] == ["Billeder"]
    assert private.pk not in {f.pk for f in access.visible_folders(outsider)}


def test_a_soft_deleted_folder_is_invisible(resident_in: Callable) -> None:
    from django.utils import timezone

    ArchiveFolder.objects.create(name="Billeder", deleted_at=timezone.now())
    resident = resident_in("a@gahk.dk", None)

    assert not access.visible_folders(resident).exists()


# --- the views --------------------------------------------------------------------------------------


def test_browsing_a_folder_you_may_not_see_is_a_404_not_a_403(
    resident_in: Callable, workgroups: tuple
) -> None:
    """A 403 would confirm that a folder with this id exists, which is the fact being hidden."""
    regnskab, fest = workgroups
    private = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    outsider = resident_in("a@gahk.dk", fest)

    response = login(outsider).get(f"/intern/arkiv/mappe/{private.pk}/")

    assert response.status_code == 404


def test_the_root_listing_shows_only_visible_roots(resident_in: Callable, workgroups: tuple) -> None:
    regnskab, fest = workgroups
    ArchiveFolder.objects.create(name="Billeder")
    ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    outsider = resident_in("a@gahk.dk", fest)

    body = login(outsider).get(ROOT_URL).content.decode()

    assert "Billeder" in body
    assert "Regnskab" not in body


def test_a_member_can_browse_their_groups_folder(resident_in: Callable, workgroups: tuple) -> None:
    regnskab, _ = workgroups
    folder = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    member = resident_in("a@gahk.dk", regnskab)

    response = login(member).get(f"/intern/arkiv/mappe/{folder.pk}/")

    assert response.status_code == 200
    assert "Regnskab" in response.content.decode()


def test_the_gate_keeps_a_plain_resident_out(resident_in: Callable, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rollout gate, which the autouse fixture opens for every other test here."""
    monkeypatch.setattr(access, "ACCESS_ROLES", (Role.ADMINISTRATOR, Role.INSPEKTION))
    resident = resident_in("a@gahk.dk", None)

    assert login(resident).get(ROOT_URL).status_code == 403


def test_the_gate_lets_inspektion_in(make_resident: Callable, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access, "ACCESS_ROLES", (Role.ADMINISTRATOR, Role.INSPEKTION))
    inspektion = make_resident(email="i@gahk.dk", roles=[Role.INSPEKTION])

    assert login(inspektion).get(ROOT_URL).status_code == 200


def test_the_board_requires_login() -> None:
    assert Client().get(ROOT_URL).status_code in (302, 403)


# --- files and downloads ----------------------------------------------------------------------------


def make_file(folder: ArchiveFolder, name: str = "referat.pdf", body: bytes = b"pdfbytes") -> ArchiveFile:
    """A row plus its object, put into whatever store is configured (local disk under test)."""
    import hashlib
    from io import BytesIO

    from arkiv.storage import get_store

    digest = hashlib.sha256(body).hexdigest()
    get_store().save(object_key(digest), BytesIO(body))
    return ArchiveFile.objects.create(
        folder=folder, name=name, sha256=digest, size=len(body), content_type="application/pdf"
    )


@pytest.fixture
def media_tmp(settings: object, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    return tmp_path


def test_a_file_is_downloadable_by_someone_who_can_see_its_folder(
    resident_in: Callable, media_tmp: Path
) -> None:
    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder)
    resident = resident_in("a@gahk.dk", None)

    response = login(resident).get(f"/intern/arkiv/fil/{file.pk}/hent")

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"pdfbytes"


def test_downloading_a_file_from_a_folder_you_cannot_see_is_a_404(
    resident_in: Callable, workgroups: tuple, media_tmp: Path
) -> None:
    """The link is forwardable; the permission is not. Access is re-checked on every request, which
    is the whole reason downloads route through Django instead of a presigned URL in the page."""
    regnskab, fest = workgroups
    folder = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    file = make_file(folder)
    outsider = resident_in("a@gahk.dk", fest)

    assert login(outsider).get(f"/intern/arkiv/fil/{file.pk}/hent").status_code == 404


def test_a_file_listing_shows_only_visible_files(
    resident_in: Callable, workgroups: tuple, media_tmp: Path
) -> None:
    regnskab, fest = workgroups
    public = ArchiveFolder.objects.create(name="Billeder")
    private = ArchiveFolder.objects.create(name="Bilag", parent=public, workgroup=regnskab)
    make_file(public, name="fest.jpg")
    make_file(private, name="hemmeligt.pdf", body=b"other")
    outsider = resident_in("a@gahk.dk", fest)

    body = login(outsider).get(f"/intern/arkiv/mappe/{public.pk}/").content.decode()

    assert "fest.jpg" in body
    assert "hemmeligt.pdf" not in body


def test_a_soft_deleted_file_is_gone_from_the_listing_and_the_download(
    resident_in: Callable, media_tmp: Path
) -> None:
    """ASSERTED ON THE LINK, NOT ON THE NAME, and the reason is the two-stage delete.

    This used to say `file.name not in <the page>`, which was right when a removed file had no
    surface at all. It is not right any more: the removed list names it on purpose, so that whoever
    took it out can put it back or destroy it (see views.browse). The name being present is now the
    feature rather than the leak.

    What "gone from the listing" actually means is that nothing on the page reaches the bytes, so
    that is what this checks — no download URL, and the download itself 404s. Both halves matter:
    the URL could be absent because the template changed shape while the endpoint stayed open.
    """
    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder)
    resident = resident_in("a@gahk.dk", None)
    file.soft_delete()

    client = login(resident)

    body = client.get(f"/intern/arkiv/mappe/{folder.pk}/").content.decode()
    assert f"/intern/arkiv/fil/{file.pk}/hent" not in body
    assert f"/intern/arkiv/fil/{file.pk}/miniature" not in body
    assert client.get(f"/intern/arkiv/fil/{file.pk}/hent").status_code == 404


# --- content addressing -----------------------------------------------------------------------------


def test_the_same_bytes_in_two_folders_are_one_object(media_tmp: Path) -> None:
    """The reason keys are hashes: 2 TB of phone uploads from one weekend contains the same
    photograph several times over."""
    a = ArchiveFolder.objects.create(name="A")
    b = ArchiveFolder.objects.create(name="B")

    first = make_file(a, name="fest.jpg", body=b"same")
    second = make_file(b, name="fest-kopi.jpg", body=b"same")

    assert first.sha256 == second.sha256
    assert first.key == second.key


def test_deleting_one_row_must_not_orphan_the_shared_object(media_tmp: Path) -> None:
    """The invariant content addressing forces: a row is not the object's only owner."""
    a = ArchiveFolder.objects.create(name="A")
    b = ArchiveFolder.objects.create(name="B")
    first = make_file(a, name="fest.jpg", body=b"same")
    make_file(b, name="kopi.jpg", body=b"same")

    ArchiveFile.objects.filter(pk=first.pk).delete()

    assert unreferenced_keys({first.sha256}) == set()


def test_an_object_becomes_collectable_once_no_row_references_it(media_tmp: Path) -> None:
    folder = ArchiveFolder.objects.create(name="A")
    file = make_file(folder, body=b"lonely")
    digest = file.sha256

    ArchiveFile.objects.filter(pk=file.pk).delete()

    assert unreferenced_keys({digest}) == {object_key(digest)}


def test_a_soft_deleted_row_still_holds_its_object(media_tmp: Path) -> None:
    """Undo has to restore a row that still has bytes behind it."""
    folder = ArchiveFolder.objects.create(name="A")
    file = make_file(folder, body=b"recoverable")
    file.soft_delete()

    assert unreferenced_keys({file.sha256}) == set()


# --- the two-stage delete -----------------------------------------------------------------------
#
# "Fjern" takes a file out of the listing and keeps the bytes; "Slet permanent", reachable only from
# the removed list, destroys the row and - if no other row shares the hash - the object too. What
# makes the second one safe to offer every writer is the ORDER, not the permission: see
# arkiv/access.py::can_purge_file. These tests pin both halves of that.


def test_a_removed_file_leaves_the_listing_but_keeps_its_bytes(
    resident_in: Callable, media_tmp: Path
) -> None:
    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder, name="fest.jpg")
    client = login(resident_in("a@gahk.dk", None))

    client.post(f"{ROOT_URL}fil/{file.pk}/fjern")

    file.refresh_from_db()
    assert file.deleted_at is not None
    assert unreferenced_keys({file.sha256}) == set(), "the bytes must survive stage one"
    body = client.get(f"{ROOT_URL}mappe/{folder.pk}/").content.decode()
    assert "Fjernede filer" in body, "a writer sees what they removed, so it can be undone"


def test_the_removed_list_is_only_shown_to_someone_who_can_write(
    resident_in: Callable, workgroups: tuple, media_tmp: Path
) -> None:
    """It carries the only irreversible control in Arkiv, so it must not render for a reader who
    cannot act on it. can_write follows can_read, so the case is a folder you cannot read at all -
    which 404s - versus one you can."""
    regnskab, _fest = workgroups
    folder = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab, effective_workgroup=regnskab)
    file = make_file(folder, name="budget.pdf")
    file.soft_delete()

    outsider = login(resident_in("ude@gahk.dk", None))
    assert outsider.get(f"{ROOT_URL}mappe/{folder.pk}/").status_code == 404

    member = login(resident_in("inde@gahk.dk", regnskab))
    body = member.get(f"{ROOT_URL}mappe/{folder.pk}/").content.decode()
    assert "Fjernede filer" in body
    assert "Slet permanent" in body


def test_a_removed_file_is_no_longer_downloadable(resident_in: Callable, media_tmp: Path) -> None:
    """Otherwise "fjernet" would mean nothing: the row is out of the listing but the bytes are still
    in the store, and a link that still served them would make stage one cosmetic."""
    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder)
    file.soft_delete()
    client = login(resident_in("a@gahk.dk", None))

    assert client.get(f"{ROOT_URL}fil/{file.pk}/hent").status_code == 404


def test_restoring_puts_the_file_back(resident_in: Callable, media_tmp: Path) -> None:
    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder, name="fest.jpg")
    resident = resident_in("a@gahk.dk", None)
    file.soft_delete(by=resident)
    client = login(resident)

    client.post(f"{ROOT_URL}fil/{file.pk}/gendan")

    file.refresh_from_db()
    assert file.deleted_at is None
    assert file.deleted_by is None, "the column answers why it is missing, so it clears with it"
    assert "fest.jpg" in client.get(f"{ROOT_URL}mappe/{folder.pk}/").content.decode()


def test_restoring_over_a_reused_name_is_refused_not_silently_renamed(
    resident_in: Callable, media_tmp: Path
) -> None:
    """uniq_file_name_per_folder covers live rows only, so removing a file and uploading another
    with the same name is legal - and then the restore collides. Reported, because renaming either
    party to make room is worse than saying what happened."""
    folder = ArchiveFolder.objects.create(name="Billeder")
    first = make_file(folder, name="referat.pdf", body=b"gammel")
    first.soft_delete()
    make_file(folder, name="referat.pdf", body=b"ny")
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(f"{ROOT_URL}fil/{first.pk}/gendan", follow=True)

    first.refresh_from_db()
    assert first.deleted_at is not None, "still removed"
    assert "findes allerede en fil" in response.content.decode()


def test_purging_destroys_the_row_and_the_bytes(resident_in: Callable, media_tmp: Path) -> None:
    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder, name="fest.jpg", body=b"unique")
    digest = file.sha256
    file.soft_delete()
    client = login(resident_in("a@gahk.dk", None))

    client.post(f"{ROOT_URL}fil/{file.pk}/slet-permanent")

    assert not ArchiveFile.objects.filter(pk=file.pk).exists()
    assert not get_store().exists(object_key(digest)), "the bytes go too - that is the point"


def test_purging_one_copy_keeps_the_bytes_another_row_shares(resident_in: Callable, media_tmp: Path) -> None:
    """THE INVARIANT CONTENT ADDRESSING FORCES. The same photograph filed in two folders is two rows
    and one object, so purging one must not pull the bytes out from under the other."""
    a = ArchiveFolder.objects.create(name="A")
    b = ArchiveFolder.objects.create(name="B")
    mine = make_file(a, name="fest.jpg", body=b"same")
    theirs = make_file(b, name="kopi.jpg", body=b"same")
    mine.soft_delete()
    client = login(resident_in("a@gahk.dk", None))

    client.post(f"{ROOT_URL}fil/{mine.pk}/slet-permanent")

    assert not ArchiveFile.objects.filter(pk=mine.pk).exists()
    assert ArchiveFile.objects.filter(pk=theirs.pk).exists()
    assert get_store().exists(object_key(theirs.sha256)), "the other row still needs these bytes"


def test_purging_keeps_bytes_that_somebody_elses_undo_still_needs(
    resident_in: Callable, media_tmp: Path
) -> None:
    """A soft-deleted row counts as a reference, so purging my copy must not break their restore."""
    a = ArchiveFolder.objects.create(name="A")
    b = ArchiveFolder.objects.create(name="B")
    mine = make_file(a, name="fest.jpg", body=b"same")
    theirs = make_file(b, name="kopi.jpg", body=b"same")
    mine.soft_delete()
    theirs.soft_delete()
    client = login(resident_in("a@gahk.dk", None))

    client.post(f"{ROOT_URL}fil/{mine.pk}/slet-permanent")

    assert get_store().exists(object_key(theirs.sha256))
    theirs.refresh_from_db()
    theirs.restore()
    assert get_store().exists(object_key(theirs.sha256)), "restored row still has its bytes"


def test_purging_takes_the_thumbnail_with_it(resident_in: Callable, media_tmp: Path) -> None:
    """The preview is keyed off the same hash, so it is unreferenced exactly when the original is -
    and an orphaned thumbnail would be a paid-for object nothing can ever reach."""
    from io import BytesIO

    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder, name="fest.jpg", body=b"withthumb")
    get_store().save(thumbnail_key(file.sha256), BytesIO(b"jpegpreview"))
    ArchiveFile.objects.filter(pk=file.pk).update(has_thumbnail=True)
    digest = file.sha256
    file.soft_delete()
    client = login(resident_in("a@gahk.dk", None))

    client.post(f"{ROOT_URL}fil/{file.pk}/slet-permanent")

    assert not get_store().exists(thumbnail_key(digest))


def test_purging_a_file_that_never_had_a_preview_does_not_fail(
    resident_in: Callable, media_tmp: Path
) -> None:
    """The thumbnail is deleted unconditionally when the object goes, so a document with no preview
    is a delete of a key that was never there. Both stores are documented as no-ops on that."""
    folder = ArchiveFolder.objects.create(name="Dokumenter")
    file = make_file(folder, name="referat.pdf", body=b"nopreview")
    assert not file.has_thumbnail
    file.soft_delete()
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(f"{ROOT_URL}fil/{file.pk}/slet-permanent")

    assert response.status_code == 302
    assert not ArchiveFile.objects.filter(pk=file.pk).exists()


def test_a_live_file_cannot_be_purged(resident_in: Callable, media_tmp: Path) -> None:
    """THE ORDER IS THE SAFETY. Stage two is reachable only for a row stage one has marked, so a
    misplaced tap on a file somebody is reading cannot destroy it however the request is made."""
    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder, name="fest.jpg")
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(f"{ROOT_URL}fil/{file.pk}/slet-permanent")

    assert response.status_code == 404
    assert ArchiveFile.objects.filter(pk=file.pk).exists()
    assert get_store().exists(object_key(file.sha256))


def test_an_outsider_cannot_purge_a_file_in_a_folder_they_cannot_see(
    resident_in: Callable, workgroups: tuple, media_tmp: Path
) -> None:
    """404, never 403: whether a file exists in Regnskabsgruppen's folder is the thing being hidden,
    and a purge endpoint that answered differently for a real id would leak it."""
    regnskab, _fest = workgroups
    folder = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab, effective_workgroup=regnskab)
    file = make_file(folder, name="budget.pdf")
    file.soft_delete()

    client = login(resident_in("ude@gahk.dk", None))
    response = client.post(f"{ROOT_URL}fil/{file.pk}/slet-permanent")

    assert response.status_code == 404
    assert ArchiveFile.objects.filter(pk=file.pk).exists()


def test_neither_stage_two_route_answers_a_get(resident_in: Callable, media_tmp: Path) -> None:
    """Both change state, and one is irreversible. A GET would let a prefetching browser or a
    crawler purge things by following a link."""
    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder)
    file.soft_delete()
    client = login(resident_in("a@gahk.dk", None))

    assert client.get(f"{ROOT_URL}fil/{file.pk}/gendan").status_code == 405
    assert client.get(f"{ROOT_URL}fil/{file.pk}/slet-permanent").status_code == 405
    assert ArchiveFile.objects.filter(pk=file.pk).exists()


def test_the_purge_message_says_when_the_bytes_stayed(resident_in: Callable, media_tmp: Path) -> None:
    """A listing with one fewer row in it cannot tell you that the same file is still filed
    elsewhere, so the message does."""
    a = ArchiveFolder.objects.create(name="A")
    b = ArchiveFolder.objects.create(name="B")
    mine = make_file(a, name="fest.jpg", body=b"same")
    make_file(b, name="kopi.jpg", body=b"same")
    mine.soft_delete()
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(f"{ROOT_URL}fil/{mine.pk}/slet-permanent", follow=True)

    assert "en anden mappe" in response.content.decode()


def test_hashing_reads_the_whole_stream() -> None:
    import hashlib
    from io import BytesIO

    payload = b"x" * (1024 * 1024 * 2 + 17)  # spans several read chunks

    assert sha256_of(BytesIO(payload)) == hashlib.sha256(payload).hexdigest()


# --- names ------------------------------------------------------------------------------------------


def test_two_folders_may_not_share_a_name_under_one_parent() -> None:
    from django.db import IntegrityError

    root = ArchiveFolder.objects.create(name="Arkiv")
    ArchiveFolder.objects.create(name="2026", parent=root)

    with pytest.raises(IntegrityError):
        ArchiveFolder.objects.create(name="2026", parent=root)


def test_a_deleted_folder_does_not_reserve_its_name_forever() -> None:
    """Which is why the constraint is scoped to live rows."""
    from django.utils import timezone

    root = ArchiveFolder.objects.create(name="Arkiv")
    ArchiveFolder.objects.create(name="2026", parent=root, deleted_at=timezone.now())

    ArchiveFolder.objects.create(name="2026", parent=root)  # must not raise

    assert ArchiveFolder.objects.alive().filter(parent=root, name="2026").count() == 1


# --- the Dropbox import -----------------------------------------------------------------------------


def build_tree(root: Path) -> None:
    """A miniature of the shape a Dropbox export has: nesting, a duplicate, and OS litter."""
    (root / "2026" / "Sommerfest").mkdir(parents=True)
    (root / "2026" / "Sommerfest" / "a.jpg").write_bytes(b"aaa")
    (root / "2026" / "Sommerfest" / "b.jpg").write_bytes(b"bbb")
    # The same photograph, filed twice - the case content addressing exists for.
    (root / "2026" / "kopi-af-a.jpg").write_bytes(b"aaa")
    (root / ".DS_Store").write_bytes(b"junk")


def test_import_builds_the_tree_and_dedupes_identical_bytes(tmp_path: Path, media_tmp: Path) -> None:
    from django.core.management import call_command

    source = tmp_path / "dropbox"
    source.mkdir()
    build_tree(source)

    call_command("import_arkiv", str(source), "--root", "Billedarkiv", verbosity=0)

    root = ArchiveFolder.objects.get(name="Billedarkiv", parent=None)
    assert ArchiveFolder.objects.alive().filter(parent=root).values_list("name", flat=True)[0] == "2026"
    assert ArchiveFile.objects.count() == 3, "the .DS_Store must not be imported"
    # Three rows, two objects: a.jpg and kopi-af-a.jpg share their bytes.
    assert ArchiveFile.objects.values("sha256").distinct().count() == 2


def test_import_is_idempotent(tmp_path: Path, media_tmp: Path) -> None:
    """A 2 TB import WILL be interrupted. The second run has to cost a walk, not another 2 TB."""
    from django.core.management import call_command

    source = tmp_path / "dropbox"
    source.mkdir()
    build_tree(source)

    call_command("import_arkiv", str(source), "--root", "Billedarkiv", verbosity=0)
    call_command("import_arkiv", str(source), "--root", "Billedarkiv", verbosity=0)

    assert ArchiveFile.objects.count() == 3
    assert ArchiveFolder.objects.alive().filter(name="Billedarkiv").count() == 1
    assert ArchiveFolder.objects.alive().filter(name="Sommerfest").count() == 1


def test_import_can_hand_the_root_to_an_embedsgruppe(
    tmp_path: Path, media_tmp: Path, workgroups: tuple
) -> None:
    from django.core.management import call_command

    regnskab, _ = workgroups
    source = tmp_path / "bilag"
    source.mkdir()
    (source / "2026").mkdir()
    (source / "2026" / "kvittering.pdf").write_bytes(b"pdf")

    call_command("import_arkiv", str(source), "--root", "Regnskab", "--workgroup", regnskab.name, verbosity=0)

    nested = ArchiveFolder.objects.get(name="2026")
    assert nested.effective_workgroup_id == regnskab.pk, "ownership must reach imported subfolders"


def test_import_dry_run_writes_nothing(tmp_path: Path, media_tmp: Path) -> None:
    from django.core.management import call_command

    source = tmp_path / "dropbox"
    source.mkdir()
    build_tree(source)

    call_command("import_arkiv", str(source), "--root", "Billedarkiv", "--dry-run", verbosity=0)

    assert not ArchiveFolder.objects.exists()
    assert not ArchiveFile.objects.exists()


def test_import_rejects_an_unknown_embedsgruppe(tmp_path: Path, media_tmp: Path) -> None:
    """Naming a group that does not exist must stop, not quietly import 2 TB as world-readable."""
    from django.core.management import call_command
    from django.core.management.base import CommandError

    source = tmp_path / "x"
    source.mkdir()
    (source / "a.txt").write_bytes(b"a")

    with pytest.raises(CommandError):
        call_command("import_arkiv", str(source), "--root", "R", "--workgroup", "Ikke-en-gruppe")


# --- the root folders -------------------------------------------------------------------------------


def test_seeding_gives_every_embedsgruppe_a_gated_root() -> None:
    """One folder per Workgroup, owned by it - all of them, not only the nine that map to a Role.

    Bladet, Haven, Festudvalget and Vinklubben carry no privilege and are still embedsgrupper with
    members, and access here resolves through Residency rather than through a role. A group with no
    folder has nowhere of its own to file anything, which is the Drive problem being replaced.
    """
    from django.core.management import call_command

    call_command("seed_arkiv_roots", verbosity=0)

    for workgroup in Workgroup.objects.all():
        folder = ArchiveFolder.objects.get(parent=None, name=workgroup.name)
        assert folder.effective_workgroup_id == workgroup.pk, f"{workgroup.name} is not gated"


def test_seeding_gives_the_house_a_shared_photo_root() -> None:
    """Billeder has no embedsgruppe, so every resident reads it - and, because can_write follows
    can_read, every resident uploads to it too. It is also the answer to current-only membership:
    somewhere no rotation can take away."""
    from django.core.management import call_command

    call_command("seed_arkiv_roots", verbosity=0)

    billeder = ArchiveFolder.objects.get(parent=None, name="Billeder")
    assert billeder.effective_workgroup_id is None


def test_seeding_twice_changes_nothing(make_resident: Callable) -> None:
    from django.core.management import call_command

    call_command("seed_arkiv_roots", verbosity=0)
    before = ArchiveFolder.objects.count()
    call_command("seed_arkiv_roots", verbosity=0)

    assert ArchiveFolder.objects.count() == before


def test_seeding_does_not_resurrect_a_deleted_root() -> None:
    """A root Inspektionen deliberately removed must stay removed, not come back on the next run."""
    from django.core.management import call_command
    from django.utils import timezone

    call_command("seed_arkiv_roots", verbosity=0)
    ArchiveFolder.objects.filter(parent=None, name="Billeder").update(deleted_at=timezone.now())

    call_command("seed_arkiv_roots", verbosity=0)

    assert ArchiveFolder.objects.alive().filter(parent=None, name="Billeder").count() == 1


def test_every_resident_can_upload_to_the_shared_photo_root(resident_in: Callable) -> None:
    """The whole point of Billeder: see it, and add to it, with no embedsgruppe involved."""
    from django.core.management import call_command

    call_command("seed_arkiv_roots", verbosity=0)
    billeder = ArchiveFolder.objects.get(parent=None, name="Billeder")
    resident = resident_in("a@gahk.dk", None)

    body = login(resident).get(f"/intern/arkiv/mappe/{billeder.pk}/").content.decode()

    assert "data-arkiv-upload" in body, "no upload control on the shared root"


def test_a_group_root_offers_upload_to_its_members_only(resident_in: Callable, workgroups: tuple) -> None:
    from django.core.management import call_command

    call_command("seed_arkiv_roots", verbosity=0)
    regnskab, fest = workgroups
    folder = ArchiveFolder.objects.get(parent=None, name=regnskab.name)

    member_body = (
        login(resident_in("m@gahk.dk", regnskab)).get(f"/intern/arkiv/mappe/{folder.pk}/").content.decode()
    )
    assert "data-arkiv-upload" in member_body

    assert login(resident_in("o@gahk.dk", fest)).get(f"/intern/arkiv/mappe/{folder.pk}/").status_code == 404


# --- upload -------------------------------------------------------------------------------------


def digest_of(body: bytes) -> str:
    import hashlib

    return hashlib.sha256(body).hexdigest()


def begin(client: Client, folder: ArchiveFolder, body: bytes, name: str = "fest.jpg") -> object:
    import json

    return client.post(
        f"/intern/arkiv/mappe/{folder.pk}/upload/start",
        data=json.dumps(
            {"sha256": digest_of(body), "name": name, "size": len(body), "content_type": "image/jpeg"}
        ),
        content_type="application/json",
    )


def commit(client: Client, folder: ArchiveFolder, body: bytes, name: str = "fest.jpg") -> object:
    import json

    return client.post(
        f"/intern/arkiv/mappe/{folder.pk}/upload/faerdig",
        data=json.dumps({"sha256": digest_of(body), "name": name}),
        content_type="application/json",
    )


def send(client: Client, folder: ArchiveFolder, body: bytes, name: str = "fest.jpg") -> object:
    """The dev/CI leg: what the browser does when there is no bucket."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    return client.post(
        f"/intern/arkiv/mappe/{folder.pk}/upload/direkte",
        {"sha256": digest_of(body), "file": SimpleUploadedFile(name, body, "image/jpeg")},
    )


def test_a_resident_can_upload_end_to_end(resident_in: Callable, media_tmp: Path) -> None:
    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))
    body = b"jpegbytes"

    assert begin(client, folder, body).status_code == 200
    assert send(client, folder, body).status_code == 200
    assert commit(client, folder, body).status_code == 200

    file = ArchiveFile.objects.get()
    assert file.name == "fest.jpg"
    assert file.sha256 == digest_of(body)
    assert file.size == len(body)
    assert file.uploaded_by is not None


def test_commit_refuses_when_the_bytes_never_arrived(resident_in: Callable, media_tmp: Path) -> None:
    """THE REASON IT IS TWO STEPS. A row pointing at nothing is a broken file in a listing with no
    explanation; an object nobody references is a sweepable nuisance. Only one of those is
    acceptable, so the row is created last and only on the store's word."""
    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))
    body = b"never-sent"

    begin(client, folder, body)
    response = commit(client, folder, body)  # the upload leg deliberately skipped

    assert response.status_code == 409
    assert not ArchiveFile.objects.exists()


def test_the_size_recorded_is_the_stores_not_the_clients(resident_in: Callable, media_tmp: Path) -> None:
    """Everything the client said was its word; the HEAD is the store's."""
    import json

    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))
    body = b"x" * 40

    begin(client, folder, body)
    send(client, folder, body)
    client.post(
        f"/intern/arkiv/mappe/{folder.pk}/upload/faerdig",
        data=json.dumps({"sha256": digest_of(body), "name": "fest.jpg"}),
        content_type="application/json",
    )

    assert ArchiveFile.objects.get().size == 40


def test_uploading_to_a_folder_you_cannot_see_is_a_404(
    resident_in: Callable, workgroups: tuple, media_tmp: Path
) -> None:
    """Access is checked on BOTH legs: a client can call commit directly, and the folder it names is
    the only thing establishing permission."""
    regnskab, fest = workgroups
    folder = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    client = login(resident_in("o@gahk.dk", fest))
    body = b"jpegbytes"

    assert begin(client, folder, body).status_code == 404
    assert send(client, folder, body).status_code == 404
    assert commit(client, folder, body).status_code == 404
    assert not ArchiveFile.objects.exists()


def test_a_duplicate_name_in_one_folder_is_refused(resident_in: Callable, media_tmp: Path) -> None:
    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))
    body = b"jpegbytes"
    begin(client, folder, body)
    send(client, folder, body)
    commit(client, folder, body)

    assert begin(client, folder, b"other", name="fest.jpg").status_code == 409
    assert ArchiveFile.objects.count() == 1


def test_bytes_already_in_the_store_skip_the_upload(resident_in: Callable, media_tmp: Path) -> None:
    """Deduplication as the resident experiences it: the second copy of a photograph sends nothing.

    On 2 TB of phone uploads from one weekend this is not a micro-optimisation.
    """
    a = ArchiveFolder.objects.create(name="A")
    b = ArchiveFolder.objects.create(name="B")
    client = login(resident_in("a@gahk.dk", None))
    body = b"jpegbytes"
    begin(client, a, body)
    send(client, a, body)
    commit(client, a, body)

    plan = begin(client, b, body, name="kopi.jpg").json()

    assert plan["already_stored"] is True
    assert plan["upload"] is None
    assert commit(client, b, body, name="kopi.jpg").status_code == 200
    assert ArchiveFile.objects.count() == 2
    assert ArchiveFile.objects.values("sha256").distinct().count() == 1


def test_a_bad_hash_is_refused_before_it_can_shape_a_key(resident_in: Callable, media_tmp: Path) -> None:
    """object_key interpolates the hash into a path, so this is what keeps that path a flat
    namespace under arkiv/ rather than something a client can steer."""
    import json

    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))

    for bad in ("../../etc/passwd", "", "zz" * 32, "abc"):
        response = client.post(
            f"/intern/arkiv/mappe/{folder.pk}/upload/start",
            data=json.dumps({"sha256": bad, "name": "x.jpg", "size": 10}),
            content_type="application/json",
        )
        assert response.status_code == 400, bad


def test_an_oversized_file_is_refused_at_begin(resident_in: Callable, media_tmp: Path) -> None:
    import json

    from arkiv.uploads import MAX_UPLOAD_BYTES

    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(
        f"/intern/arkiv/mappe/{folder.pk}/upload/start",
        data=json.dumps({"sha256": digest_of(b"x"), "name": "stor.mov", "size": MAX_UPLOAD_BYTES + 1}),
        content_type="application/json",
    )

    assert response.status_code == 400


def test_upload_requires_post(resident_in: Callable, media_tmp: Path) -> None:
    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))

    assert client.get(f"/intern/arkiv/mappe/{folder.pk}/upload/start").status_code == 405


# --- creating subfolders ----------------------------------------------------------------------------


def new_folder(client: Client, parent: ArchiveFolder, name: str) -> object:
    return client.post(f"/intern/arkiv/mappe/{parent.pk}/ny-mappe", {"name": name})


def test_anyone_who_can_write_can_make_a_subfolder(resident_in: Callable) -> None:
    parent = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))

    new_folder(client, parent, "Sommerfest 2026")

    child = ArchiveFolder.objects.alive().get(parent=parent)
    assert child.name == "Sommerfest 2026"
    assert child.created_by is not None, "a folder with no attributed creator"


def test_a_subfolder_of_a_gated_root_is_gated_from_the_moment_it_exists(
    resident_in: Callable, workgroups: tuple
) -> None:
    """There must be no window in which it is public. save() resolves the inherited owner on insert,
    so the row is never written with effective_workgroup NULL."""
    regnskab, fest = workgroups
    parent = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    member = resident_in("m@gahk.dk", regnskab)
    outsider = resident_in("o@gahk.dk", fest)

    new_folder(login(member), parent, "Bilag")

    child = ArchiveFolder.objects.alive().get(parent=parent)
    assert child.effective_workgroup_id == regnskab.pk
    assert child.pk not in {f.pk for f in access.visible_folders(outsider)}


def test_making_a_subfolder_where_you_cannot_write_is_a_404(resident_in: Callable, workgroups: tuple) -> None:
    regnskab, fest = workgroups
    parent = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    outsider = login(resident_in("o@gahk.dk", fest))

    assert new_folder(outsider, parent, "Snyd").status_code == 404
    assert not ArchiveFolder.objects.filter(parent=parent).exists()


def test_a_root_cannot_be_created_this_way(resident_in: Callable) -> None:
    """Roots are the kollegium's filing system and belong to Inspektionen. There is deliberately no
    route that makes one with parent=None, so the top level cannot become a junk drawer."""
    from django.urls import NoReverseMatch, reverse

    ArchiveFolder.objects.create(name="Billeder")
    with pytest.raises(NoReverseMatch):
        reverse("arkiv:folder_create")


@pytest.mark.parametrize(
    ("name", "why"),
    [("", "empty"), ("   ", "whitespace only"), ("x" * 200, "too long"), ("a/b", "contains a slash")],
)
def test_a_bad_folder_name_is_refused(resident_in: Callable, name: str, why: str) -> None:
    parent = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))

    new_folder(client, parent, name)

    assert not ArchiveFolder.objects.filter(parent=parent).exists(), why


def test_a_duplicate_folder_name_is_refused(resident_in: Callable) -> None:
    parent = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))
    new_folder(client, parent, "2026")

    new_folder(client, parent, "2026")

    assert ArchiveFolder.objects.alive().filter(parent=parent, name="2026").count() == 1


def test_creating_a_folder_requires_post(resident_in: Callable) -> None:
    parent = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))

    assert client.get(f"/intern/arkiv/mappe/{parent.pk}/ny-mappe").status_code == 405


# --- deleting files ---------------------------------------------------------------------------------


def test_a_file_can_be_removed_and_the_bytes_stay(resident_in: Callable, media_tmp: Path) -> None:
    """Soft, on purpose: `services.unreferenced_keys` counts a soft-deleted row as a reference, so
    the object survives for an administrator to restore. That is what makes it safe to let anyone
    who can write here do it."""
    from arkiv.storage import get_store

    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder)
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(f"/intern/arkiv/fil/{file.pk}/fjern")

    file.refresh_from_db()
    assert response.status_code == 302
    assert file.deleted_at is not None
    assert file.deleted_by is not None, "a removal nobody is accountable for"
    assert get_store().exists(file.key), "the bytes must survive the row"
    assert unreferenced_keys({file.sha256}) == set()


def test_a_removed_file_leaves_the_listing_and_the_download(resident_in: Callable, media_tmp: Path) -> None:
    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder)
    client = login(resident_in("a@gahk.dk", None))
    client.post(f"/intern/arkiv/fil/{file.pk}/fjern")

    body = client.get(f"/intern/arkiv/mappe/{folder.pk}/").content.decode()
    # By link, not by name: the confirmation message names the file too, so a bare name check would
    # pass or fail on the wording rather than on the listing.
    assert f"/intern/arkiv/fil/{file.pk}/hent" not in body
    assert client.get(f"/intern/arkiv/fil/{file.pk}/hent").status_code == 404


def test_someone_who_did_not_upload_it_may_still_remove_it(resident_in: Callable, media_tmp: Path) -> None:
    """THE DECISION. The Dropbox this replaces let everyone delete everything; an archive only its
    original uploader can tidy accumulates mistakes nobody may fix, and half the uploaders have
    moved out. Soft delete plus attribution is what makes that reasonable."""
    folder = ArchiveFolder.objects.create(name="Billeder")
    uploader = resident_in("up@gahk.dk", None)
    file = make_file(folder)
    ArchiveFile.objects.filter(pk=file.pk).update(uploaded_by=uploader)
    someone_else = resident_in("other@gahk.dk", None)

    login(someone_else).post(f"/intern/arkiv/fil/{file.pk}/fjern")

    file.refresh_from_db()
    assert file.deleted_at is not None
    assert file.deleted_by_id == someone_else.pk


def test_removing_a_file_you_cannot_see_is_a_404(
    resident_in: Callable, workgroups: tuple, media_tmp: Path
) -> None:
    regnskab, fest = workgroups
    folder = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    file = make_file(folder)
    outsider = login(resident_in("o@gahk.dk", fest))

    assert outsider.post(f"/intern/arkiv/fil/{file.pk}/fjern").status_code == 404
    file.refresh_from_db()
    assert file.deleted_at is None


def test_removing_a_file_requires_post(resident_in: Callable, media_tmp: Path) -> None:
    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder)
    client = login(resident_in("a@gahk.dk", None))

    assert client.get(f"/intern/arkiv/fil/{file.pk}/fjern").status_code == 405


def test_removing_one_of_two_rows_sharing_bytes_keeps_the_object(
    resident_in: Callable, media_tmp: Path
) -> None:
    """Content addressing means a row is never the object's only owner."""
    from arkiv.storage import get_store

    a = ArchiveFolder.objects.create(name="A")
    b = ArchiveFolder.objects.create(name="B")
    first = make_file(a, name="fest.jpg", body=b"same")
    make_file(b, name="kopi.jpg", body=b"same")
    client = login(resident_in("a@gahk.dk", None))

    client.post(f"/intern/arkiv/fil/{first.pk}/fjern")

    assert get_store().exists(first.key)
    assert unreferenced_keys({first.sha256}) == set()


def test_the_controls_appear_only_where_you_can_write(
    resident_in: Callable, workgroups: tuple, media_tmp: Path
) -> None:
    """A control the server would refuse must not be rendered - the page and the view have to agree
    about what is possible."""
    regnskab, _ = workgroups
    folder = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    make_file(folder)
    member = login(resident_in("m@gahk.dk", regnskab))

    body = member.get(f"/intern/arkiv/mappe/{folder.pk}/").content.decode()

    assert "ny-mappe" in body
    assert "fjern" in body
    assert "data-arkiv-upload" in body


# --- thumbnails ---------------------------------------------------------------------------------
#
# Previews are made in the BROWSER for live uploads (frontend/src/imageupload.ts), so production
# still has no image library and no worker. `make_arkiv_thumbnails` is the one-off counterpart for
# the imported backlog, and it is the only thing here that needs Pillow - a dev-only dependency.


def real_jpeg(width: int = 900, height: int = 600) -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (width, height), (120, 90, 40)).save(buf, format="JPEG")
    return buf.getvalue()


def test_the_thumbnail_key_is_derived_from_the_originals_hash() -> None:
    """No second digest column: two rows sharing bytes share one preview, and a client never gets
    to name the key."""
    from arkiv.models import thumbnail_key

    digest = "a" * 64

    assert thumbnail_key(digest) == f"arkiv-thumb/aa/{digest}"
    assert thumbnail_key(digest) != object_key(digest)


def test_a_file_without_a_preview_renders_the_file_icon(resident_in: Callable, media_tmp: Path) -> None:
    folder = ArchiveFolder.objects.create(name="Billeder")
    make_file(folder, name="referat.pdf")
    client = login(resident_in("a@gahk.dk", None))

    body = client.get(f"/intern/arkiv/mappe/{folder.pk}/").content.decode()

    assert "#i-file" in body
    assert "arkiv-thumb" not in body


def test_a_preview_is_served_and_cached_hard(resident_in: Callable, media_tmp: Path) -> None:
    """Cacheable for a week only because the key is content-addressed: different bytes, different
    URL, so a preview can never go stale. On a folder of 200 photographs that is the difference
    between 200 revalidations per visit and none."""
    from io import BytesIO

    from arkiv.models import thumbnail_key
    from arkiv.storage import get_store

    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder, name="fest.jpg")
    get_store().save(thumbnail_key(file.sha256), BytesIO(b"thumbbytes"))
    ArchiveFile.objects.filter(pk=file.pk).update(has_thumbnail=True, content_type="image/jpeg")
    client = login(resident_in("a@gahk.dk", None))

    response = client.get(f"/intern/arkiv/fil/{file.pk}/miniature")

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"thumbbytes"
    assert "max-age=604800" in response.headers["Cache-Control"]
    assert response.headers["Cache-Control"].startswith("private")


def test_a_preview_obeys_the_same_access_rules_as_the_file(
    resident_in: Callable, workgroups: tuple, media_tmp: Path
) -> None:
    """A thumbnail of a Regnskabsgruppen document is as confidential as the document."""
    from io import BytesIO

    from arkiv.models import thumbnail_key
    from arkiv.storage import get_store

    regnskab, fest = workgroups
    folder = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    file = make_file(folder, name="bilag.jpg")
    get_store().save(thumbnail_key(file.sha256), BytesIO(b"thumbbytes"))
    ArchiveFile.objects.filter(pk=file.pk).update(has_thumbnail=True)

    outsider = login(resident_in("o@gahk.dk", fest))

    assert outsider.get(f"/intern/arkiv/fil/{file.pk}/miniature").status_code == 404


def test_a_row_claiming_a_preview_it_does_not_have_404s(resident_in: Callable, media_tmp: Path) -> None:
    """has_thumbnail is set from the store, never the client - but if it is ever wrong, the answer
    is a 404, not a 500 and not a broken image with no explanation."""
    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder, name="fest.jpg")
    ArchiveFile.objects.filter(pk=file.pk).update(has_thumbnail=True)
    client = login(resident_in("a@gahk.dk", None))

    assert client.get(f"/intern/arkiv/fil/{file.pk}/miniature").status_code == 404


def test_upload_sets_the_flag_only_when_a_preview_actually_arrived(
    resident_in: Callable, media_tmp: Path
) -> None:
    """THE HONESTY RULE. A flag set on the client's word renders a broken <img> for every file whose
    preview silently failed to upload - which is exactly what a phone with no canvas would do."""
    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))
    body = b"jpegbytes"

    begin(client, folder, body)
    send(client, folder, body)  # the original only; no thumbnail leg
    commit(client, folder, body)

    assert ArchiveFile.objects.get().has_thumbnail is False


def test_begin_offers_both_derived_sizes_for_images_only(resident_in: Callable, media_tmp: Path) -> None:
    """Both sizes are made in the browser, so both have to be offered a slot. A PDF gets neither."""
    import json

    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))

    def plan_for(content_type: str, name: str) -> object:
        return client.post(
            f"/intern/arkiv/mappe/{folder.pk}/upload/start",
            data=json.dumps(
                {"sha256": digest_of(name.encode()), "name": name, "size": 10, "content_type": content_type}
            ),
            content_type="application/json",
        ).json()

    image = plan_for("image/jpeg", "fest.jpg")["derived"]
    assert image["thumbnail"] is not None
    assert image["preview"] is not None

    document = plan_for("application/pdf", "referat.pdf")["derived"]
    assert document["thumbnail"] is None
    assert document["preview"] is None


def test_begin_does_not_ask_for_a_size_the_store_already_has(resident_in: Callable, media_tmp: Path) -> None:
    """The second copy of a photograph uploads nothing at all - including its derived sizes. Both
    are keyed by the original's hash, so somebody else's upload last year already made them."""
    import json
    from io import BytesIO

    from arkiv.models import preview_key, thumbnail_key
    from arkiv.storage import get_store

    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))
    sha = digest_of(b"fest")
    get_store().save(thumbnail_key(sha), BytesIO(b"already here"))

    plan = client.post(
        f"/intern/arkiv/mappe/{folder.pk}/upload/start",
        data=json.dumps({"sha256": sha, "name": "fest.jpg", "size": 10, "content_type": "image/jpeg"}),
        content_type="application/json",
    ).json()["derived"]

    assert plan["thumbnail"] is None, "asked for a thumbnail the bucket already holds"
    assert plan["preview"] is not None, "the preview is genuinely missing and must still be asked for"
    assert not get_store().exists(preview_key(sha))


def test_both_derived_legs_land_under_their_own_prefixes(resident_in: Callable, media_tmp: Path) -> None:
    """Three objects from one upload, each under its own prefix, and two flags set from the STORE."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from arkiv.models import preview_key, thumbnail_key
    from arkiv.storage import get_store

    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))
    body = b"jpegbytes"
    sha = digest_of(body)

    begin(client, folder, body)
    send(client, folder, body)
    for kind, blob in (("thumbnail", b"thumb"), ("preview", b"biggerpreview")):
        client.post(
            f"/intern/arkiv/mappe/{folder.pk}/upload/direkte",
            {"sha256": sha, "derived": kind, "file": SimpleUploadedFile(f"{kind}.jpg", blob)},
        )
    commit(client, folder, body)

    store = get_store()
    assert store.exists(object_key(sha)), "the original moved"
    assert store.exists(thumbnail_key(sha)), "the thumbnail did not land"
    assert store.exists(preview_key(sha)), "the preview did not land"
    file = ArchiveFile.objects.get()
    assert file.has_thumbnail is True
    assert file.has_preview is True


def test_a_browser_that_makes_only_the_thumbnail_still_gets_a_usable_file(
    resident_in: Callable, media_tmp: Path
) -> None:
    """Each size is asked about separately, because a browser can manage one and not the other.
    The flag has to follow what actually arrived - the viewer then serves the original instead,
    which is slower but not broken. Believing the client here would render a broken image."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))
    body = b"jpegbytes"
    sha = digest_of(body)

    begin(client, folder, body)
    send(client, folder, body)
    client.post(
        f"/intern/arkiv/mappe/{folder.pk}/upload/direkte",
        {"sha256": sha, "derived": "thumbnail", "file": SimpleUploadedFile("t.jpg", b"thumb")},
    )
    commit(client, folder, body)

    file = ArchiveFile.objects.get()
    assert file.has_thumbnail is True
    assert file.has_preview is False, "claimed a preview that was never uploaded"

    # And the viewer still works, on the original.
    response = client.get(f"/intern/arkiv/fil/{file.pk}/stor")
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == body


def test_an_unknown_derived_size_is_refused(resident_in: Callable, media_tmp: Path) -> None:
    """The kind names a key prefix, so it is not something a client may invent."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(
        f"/intern/arkiv/mappe/{folder.pk}/upload/direkte",
        {
            "sha256": digest_of(b"x"),
            "derived": "../../../etc",
            "file": SimpleUploadedFile("t.jpg", b"x"),
        },
    )

    assert response.status_code == 400


@pytest.mark.parametrize("kind", ["thumbnail", "preview"])
def test_an_oversized_derived_image_is_refused(resident_in: Callable, media_tmp: Path, kind: str) -> None:
    """Neither slot may become a way to smuggle a second full-size upload past the cap. Both are
    checked, because they have DIFFERENT limits and a shared test would only prove one of them."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from arkiv.uploads import derived_limit

    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(
        f"/intern/arkiv/mappe/{folder.pk}/upload/direkte",
        {
            "sha256": digest_of(b"x"),
            "derived": kind,
            "file": SimpleUploadedFile("t.jpg", b"x" * (derived_limit(kind) + 1)),
        },
    )

    assert response.status_code == 400


# --- the imported backlog (Pillow, dev-only) ------------------------------------------------------


def test_the_backlog_command_renders_a_real_preview(media_tmp: Path) -> None:
    from io import BytesIO

    from django.core.management import call_command
    from PIL import Image

    from arkiv.storage import get_store

    folder = ArchiveFolder.objects.create(name="Billeder")
    original = real_jpeg(900, 600)
    file = make_file(folder, name="stor.jpg", body=original)
    ArchiveFile.objects.filter(pk=file.pk).update(content_type="image/jpeg")

    call_command("make_arkiv_thumbnails", verbosity=0)

    file.refresh_from_db()
    assert file.has_thumbnail is True
    thumb_path = get_store().path(file.thumb_key)
    made = Image.open(BytesIO(thumb_path.read_bytes()))
    assert max(made.size) <= 320, "the preview is not thumbnail-sized"
    assert thumb_path.stat().st_size < len(original), "the preview is not smaller than the original"


def test_the_backlog_command_is_idempotent(media_tmp: Path) -> None:
    """A 2 TB backlog will be interrupted; the second run must cost a query, not a re-render."""
    from django.core.management import call_command

    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder, name="stor.jpg", body=real_jpeg())
    ArchiveFile.objects.filter(pk=file.pk).update(content_type="image/jpeg")

    call_command("make_arkiv_thumbnails", verbosity=0)
    first = get_store_mtime(file)
    call_command("make_arkiv_thumbnails", verbosity=0)

    assert get_store_mtime(file) == first, "the second run re-rendered"


def get_store_mtime(file: ArchiveFile) -> float:
    from arkiv.storage import get_store

    return get_store().path(file.thumb_key).stat().st_mtime


def test_the_backlog_command_skips_non_images_and_survives_a_bad_one(media_tmp: Path) -> None:
    """One corrupt file must not stop two hundred thousand others."""
    from django.core.management import call_command

    folder = ArchiveFolder.objects.create(name="Billeder")
    good = make_file(folder, name="god.jpg", body=real_jpeg())
    ArchiveFile.objects.filter(pk=good.pk).update(content_type="image/jpeg")
    bad = make_file(folder, name="daarlig.jpg", body=b"not actually a jpeg")
    ArchiveFile.objects.filter(pk=bad.pk).update(content_type="image/jpeg")
    doc = make_file(folder, name="referat.pdf", body=b"pdf")

    call_command("make_arkiv_thumbnails", verbosity=0)

    good.refresh_from_db()
    bad.refresh_from_db()
    doc.refresh_from_db()
    assert good.has_thumbnail is True
    assert bad.has_thumbnail is False, "a corrupt file must not be marked as having a preview"
    assert doc.has_thumbnail is False, "a PDF is not an image"


def test_import_skips_the_debris_a_twenty_year_dropbox_accumulates(tmp_path: Path, media_tmp: Path) -> None:
    """Shaped after the real GAHK export: macOS resource forks, .AppleDouble directories, and the
    usual OS droppings, mixed in with the photographs.

    Left in, these do not just waste rows - `._DSC_0310.JPG` appears in the browser beside the
    photograph it describes, and `.AppleDouble` appears as a folder inside every album, in a root
    the whole kollegium can see.
    """
    from django.core.management import call_command

    source = tmp_path / "gahk-export"
    album = source / "2004" / "Terrasseåbning 2004"
    (album / ".AppleDouble").mkdir(parents=True)
    (album / "DSC_0003.JPG").write_bytes(b"a real photograph")
    (album / "._DSC_0003.JPG").write_bytes(b"resource fork")
    (album / ".AppleDouble" / "DSC_0003.JPG").write_bytes(b"another fork")
    (album / ".AppleDouble" / ".Parent").write_bytes(b"fork metadata")
    (source / "2016").mkdir()
    (source / "2016" / "DSC_0310.JPG").write_bytes(b"another real photograph")
    (source / "2016" / "._DSC_0310.JPG").write_bytes(b"fork")
    (source / "2002" / ".DS_Store").parent.mkdir(parents=True, exist_ok=True)
    (source / "2002" / ".DS_Store").write_bytes(b"junk")
    (source / "2002" / "._.DS_Store").write_bytes(b"junk about junk")
    (source / "2002" / "GAHK.jpg").write_bytes(b"a third real photograph")

    call_command("import_arkiv", str(source), "--root", "Billeder", verbosity=0)

    names = sorted(ArchiveFile.objects.values_list("name", flat=True))
    assert names == ["DSC_0003.JPG", "DSC_0310.JPG", "GAHK.jpg"], names
    folders = set(ArchiveFolder.objects.values_list("name", flat=True))
    assert ".AppleDouble" not in folders, "a resource-fork directory became a folder"
    assert folders == {"Billeder", "2004", "Terrasseåbning 2004", "2016", "2002"}, folders


# --- the viewer's size, and taking several files at once --------------------------------------------


def test_the_viewer_gets_the_large_preview_not_the_thumbnail(resident_in: Callable, media_tmp: Path) -> None:
    """Three sizes exist for a reason; this is the one that proves the middle one is wired up."""
    from io import BytesIO

    from arkiv.models import preview_key
    from arkiv.storage import get_store

    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder, name="fest.jpg")
    get_store().save(preview_key(file.sha256), BytesIO(b"bigpreviewbytes"))
    ArchiveFile.objects.filter(pk=file.pk).update(has_preview=True, content_type="image/jpeg")
    client = login(resident_in("a@gahk.dk", None))

    response = client.get(f"/intern/arkiv/fil/{file.pk}/stor")

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"bigpreviewbytes"
    assert "max-age=604800" in response.headers["Cache-Control"]


def test_a_file_with_no_preview_yet_falls_back_to_the_original(
    resident_in: Callable, media_tmp: Path
) -> None:
    """THE CASE THAT WOULD BREAK EVERY FRESH UPLOAD. The browser makes a thumbnail client-side and
    cannot make a preview, so a photograph is `has_preview=False` from the moment it is uploaded
    until the backlog command next runs. 404ing here would mean the one photo that does not open in
    the viewer is always the one somebody just added - and went straight to the folder to check."""
    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder, name="ny.jpg", body=b"theoriginalbytes")
    ArchiveFile.objects.filter(pk=file.pk).update(has_preview=False, content_type="image/jpeg")
    client = login(resident_in("a@gahk.dk", None))

    response = client.get(f"/intern/arkiv/fil/{file.pk}/stor")

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"theoriginalbytes"


def test_the_preview_obeys_the_same_access_rules_as_the_file(
    resident_in: Callable, workgroups: tuple, media_tmp: Path
) -> None:
    """A second route to the bytes is a second place to leak them."""
    regnskab, _ = workgroups
    folder = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    file = make_file(folder, name="budget.jpg")
    ArchiveFile.objects.filter(pk=file.pk).update(has_preview=True, content_type="image/jpeg")
    client = login(resident_in("outsider@gahk.dk", None))

    assert client.get(f"/intern/arkiv/fil/{file.pk}/stor").status_code == 404


def test_selected_files_come_back_as_one_zip(resident_in: Callable, media_tmp: Path) -> None:
    """The whole point of the feature: pick several, get an archive with all of them in it."""
    import zipfile
    from io import BytesIO

    folder = ArchiveFolder.objects.create(name="Sommerfest")
    one = make_file(folder, name="en.jpg", body=b"first file bytes")
    two = make_file(folder, name="to.jpg", body=b"second file bytes")
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(f"/intern/arkiv/mappe/{folder.pk}/hent-valgte", {"ids": [one.pk, two.pk]})

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/zip"
    assert "Sommerfest.zip" in response.headers["Content-Disposition"]
    body = zipfile.ZipFile(BytesIO(b"".join(response.streaming_content)))
    assert sorted(body.namelist()) == ["en.jpg", "to.jpg"]
    assert body.read("en.jpg") == b"first file bytes"
    assert body.read("to.jpg") == b"second file bytes"


def test_the_zip_cannot_be_used_to_reach_a_file_you_cannot_see(
    resident_in: Callable, workgroups: tuple, media_tmp: Path
) -> None:
    """THE ONE THAT MATTERS. The ids come from the client, so a resident who can read the shared
    folder must not be able to post the id of a Regnskabsgruppen file and have it zipped in beside
    their holiday photographs. Re-checked through visible_files, scoped to the folder."""
    import zipfile
    from io import BytesIO

    regnskab, _ = workgroups
    shared = ArchiveFolder.objects.create(name="Billeder")
    secret = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    mine = make_file(shared, name="ferie.jpg", body=b"mine")
    theirs = make_file(secret, name="budget.pdf", body=b"secret")
    client = login(resident_in("outsider@gahk.dk", None))

    response = client.post(f"/intern/arkiv/mappe/{shared.pk}/hent-valgte", {"ids": [mine.pk, theirs.pk]})

    body = zipfile.ZipFile(BytesIO(b"".join(response.streaming_content)))
    assert body.namelist() == ["ferie.jpg"]


def test_a_selection_over_the_size_cap_is_refused_with_a_message(
    resident_in: Callable, media_tmp: Path
) -> None:
    """The cap is about gunicorn's worker timeout, not about disk - see views.download_selected.
    Refused as a message on the folder, not a 500 twenty seconds into a dead download."""
    from arkiv.views import MAX_SELECTED_BYTES

    folder = ArchiveFolder.objects.create(name="Video")
    big = make_file(folder, name="fest.mov", body=b"x")
    ArchiveFile.objects.filter(pk=big.pk).update(size=MAX_SELECTED_BYTES + 1)
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(f"/intern/arkiv/mappe/{folder.pk}/hent-valgte", {"ids": [big.pk]}, follow=True)

    assert response.status_code == 200
    assert "Grænsen for samlet download" in response.content.decode()


def test_an_empty_selection_says_so_rather_than_sending_an_empty_zip(
    resident_in: Callable, media_tmp: Path
) -> None:
    folder = ArchiveFolder.objects.create(name="Billeder")
    make_file(folder, name="en.jpg")
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(f"/intern/arkiv/mappe/{folder.pk}/hent-valgte", {}, follow=True)

    assert "Vælg mindst én fil" in response.content.decode()


def test_zipping_from_a_folder_you_cannot_see_is_a_404(
    resident_in: Callable, workgroups: tuple, media_tmp: Path
) -> None:
    regnskab, _ = workgroups
    folder = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    file = make_file(folder)
    client = login(resident_in("outsider@gahk.dk", None))

    response = client.post(f"/intern/arkiv/mappe/{folder.pk}/hent-valgte", {"ids": [file.pk]})

    assert response.status_code == 404


def test_the_zip_route_refuses_a_get(resident_in: Callable, media_tmp: Path) -> None:
    """A GET would be a link somebody could paste into a chat thread to start a half-gigabyte
    download for whoever clicked it."""
    folder = ArchiveFolder.objects.create(name="Billeder")
    client = login(resident_in("a@gahk.dk", None))

    assert client.get(f"/intern/arkiv/mappe/{folder.pk}/hent-valgte").status_code == 405


def test_a_row_whose_object_vanished_is_skipped_not_fatal(resident_in: Callable, media_tmp: Path) -> None:
    """A truncated zip with no explanation is worse than a zip missing one file. A missing object
    is an operator problem, not something the resident downloading photographs can act on."""
    import zipfile
    from io import BytesIO

    from arkiv.storage import get_store

    folder = ArchiveFolder.objects.create(name="Billeder")
    good = make_file(folder, name="her.jpg", body=b"present")
    gone = make_file(folder, name="væk.jpg", body=b"deleted from the bucket")
    get_store().delete(gone.key)
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(f"/intern/arkiv/mappe/{folder.pk}/hent-valgte", {"ids": [good.pk, gone.pk]})

    body = zipfile.ZipFile(BytesIO(b"".join(response.streaming_content)))
    assert body.namelist() == ["her.jpg"]


def test_the_thumbnailer_makes_both_sizes_in_one_pass(media_tmp: Path) -> None:
    """One download per original, two objects written. Doing this in two passes would mean pulling
    57,000 photographs out of the bucket twice - the reason both sizes live in one command."""
    from io import BytesIO

    from django.core.management import call_command

    from arkiv.models import preview_key, thumbnail_key
    from arkiv.storage import get_store

    pytest.importorskip("PIL", reason="Pillow is a dev-only dependency")
    from PIL import Image

    raw = BytesIO()
    Image.new("RGB", (2000, 1200), "green").save(raw, format="JPEG")
    folder = ArchiveFolder.objects.create(name="Billeder")
    file = make_file(folder, name="stor.jpg", body=raw.getvalue())
    ArchiveFile.objects.filter(pk=file.pk).update(content_type="image/jpeg")

    call_command("make_arkiv_thumbnails")

    store = get_store()
    assert store.exists(thumbnail_key(file.sha256))
    assert store.exists(preview_key(file.sha256))
    file.refresh_from_db()
    assert file.has_thumbnail and file.has_preview

    # The sizes are actually different, which is the bug a shared Image object would have caused:
    # thumbnail() mutates in place, so resizing once and reusing would make both 320px.
    thumb = Image.open(BytesIO(b"".join(store.chunks(thumbnail_key(file.sha256)))))
    preview = Image.open(BytesIO(b"".join(store.chunks(preview_key(file.sha256)))))
    assert max(thumb.size) == 320
    assert max(preview.size) == 1600


def test_the_zip_is_left_in_the_store_as_an_object(resident_in: Callable, media_tmp: Path) -> None:
    """THE POINT OF BUILDING IT RATHER THAN STREAMING IT. An object can be redirected to, which is
    what takes the recipient's connection off a gunicorn worker - see views.download_selected."""
    import zipfile
    from io import BytesIO

    from arkiv.models import selection_key
    from arkiv.storage import get_store

    folder = ArchiveFolder.objects.create(name="Sommerfest")
    one = make_file(folder, name="en.jpg", body=b"first file bytes")
    two = make_file(folder, name="to.jpg", body=b"second file bytes")
    client = login(resident_in("a@gahk.dk", None))

    client.post(f"/intern/arkiv/mappe/{folder.pk}/hent-valgte", {"ids": [one.pk, two.pk]})

    key = selection_key([(one.name, one.sha256), (two.name, two.sha256)])
    store = get_store()
    assert store.exists(key), "the zip was not left in the store"
    body = zipfile.ZipFile(BytesIO(b"".join(store.chunks(key))))
    assert sorted(body.namelist()) == ["en.jpg", "to.jpg"]
    assert body.testzip() is None, "the stored archive is corrupt"


def test_the_same_selection_is_not_rebuilt(resident_in: Callable, media_tmp: Path) -> None:
    """The morning after sommerfest, the whole kollegium asks for the same folder. One build.

    Proved by planting a sentinel at the key: if the response carries it back, nothing rebuilt.
    """
    from io import BytesIO

    from arkiv.models import selection_key
    from arkiv.storage import get_store

    folder = ArchiveFolder.objects.create(name="Sommerfest")
    one = make_file(folder, name="en.jpg", body=b"first file bytes")
    client = login(resident_in("a@gahk.dk", None))

    get_store().save(selection_key([(one.name, one.sha256)]), BytesIO(b"a previously built zip"))
    response = client.post(f"/intern/arkiv/mappe/{folder.pk}/hent-valgte", {"ids": [one.pk]})

    assert b"".join(response.streaming_content) == b"a previously built zip"


def test_a_different_selection_is_a_different_zip(resident_in: Callable, media_tmp: Path) -> None:
    """What makes reuse safe without any invalidation: the key IS the selection. Add a file, rename
    one, replace one's bytes, and the key moves - so a cached zip can never be the wrong answer."""
    from arkiv.models import selection_key

    folder = ArchiveFolder.objects.create(name="Billeder")
    one = make_file(folder, name="en.jpg", body=b"first")
    two = make_file(folder, name="to.jpg", body=b"second")

    alone = selection_key([(one.name, one.sha256)])
    both = selection_key([(one.name, one.sha256), (two.name, two.sha256)])
    renamed = selection_key([("andet-navn.jpg", one.sha256)])
    rebytes = selection_key([(one.name, two.sha256)])

    assert len({alone, both, renamed, rebytes}) == 4

    # And order does not matter: the same set asked for twice is one object, not two.
    assert both == selection_key([(two.name, two.sha256), (one.name, one.sha256)])


def test_a_selection_you_cannot_fully_see_names_a_different_zip(
    resident_in: Callable, workgroups: tuple, media_tmp: Path
) -> None:
    """The key is derived from the FILTERED list, not from what was posted. Otherwise an outsider
    could post {mine, theirs}, be filtered down to {mine}, and still be handed the object built for
    somebody who could see both."""
    import zipfile
    from io import BytesIO

    regnskab, _ = workgroups
    shared = ArchiveFolder.objects.create(name="Billeder")
    secret = ArchiveFolder.objects.create(name="Regnskab", workgroup=regnskab)
    mine = make_file(shared, name="ferie.jpg", body=b"mine")
    theirs = make_file(secret, name="budget.pdf", body=b"secret")
    client = login(resident_in("outsider@gahk.dk", None))

    response = client.post(f"/intern/arkiv/mappe/{shared.pk}/hent-valgte", {"ids": [mine.pk, theirs.pk]})

    body = zipfile.ZipFile(BytesIO(b"".join(response.streaming_content)))
    assert body.namelist() == ["ferie.jpg"]


def test_the_zip_is_redirected_to_when_there_is_a_bucket(
    resident_in: Callable, media_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE PRODUCTION BRANCH, which the local store cannot reach on its own.

    The whole reason the zip became an object is that a redirect frees the worker immediately and
    lets Hetzner serve the bytes. Tests run against the filesystem backend, where `download_url`
    returns None and the view streams instead - so without this the one behaviour the change exists
    for would be the one behaviour nothing checked.
    """
    from arkiv import views
    from arkiv.models import selection_key
    from arkiv.storage import LocalArchiveStore

    folder = ArchiveFolder.objects.create(name="Sommerfest")
    one = make_file(folder, name="en.jpg", body=b"first file bytes")
    client = login(resident_in("a@gahk.dk", None))

    signed = "https://bucket.fsn1.your-objectstorage.com/arkiv-zip/xx?sig=abc"
    monkeypatch.setattr(LocalArchiveStore, "download_url", lambda self, key, **kw: signed, raising=False)

    response = client.post(f"/intern/arkiv/mappe/{folder.pk}/hent-valgte", {"ids": [one.pk]})

    assert response.status_code == 302
    assert response.headers["Location"] == signed
    # And it still built the object first - a redirect to a key with nothing behind it is a 404
    # wearing a different hat.
    assert views.get_store().exists(selection_key([(one.name, one.sha256)]))


def test_the_response_echoes_the_pages_token_so_the_button_can_reset(
    resident_in: Callable, media_tmp: Path
) -> None:
    """A form POST that ends in a download does not navigate, so the page has no way of its own to
    tell the build finished. The nonce comes back as a cookie the script can poll for."""
    from arkiv.views import ZIP_DONE_COOKIE

    folder = ArchiveFolder.objects.create(name="Billeder")
    one = make_file(folder, name="en.jpg", body=b"bytes")
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(
        f"/intern/arkiv/mappe/{folder.pk}/hent-valgte",
        {"ids": [one.pk], "done_token": "abc123"},
    )

    assert response.cookies[ZIP_DONE_COOKIE].value == "abc123"


@pytest.mark.parametrize(
    "bad",
    ["with space", "a;b", "a\nb", "x" * 65, "a=b", ""],
    ids=["space", "semicolon", "newline", "too long", "equals", "empty"],
)
def test_a_token_that_could_steer_a_header_is_dropped(
    resident_in: Callable, media_tmp: Path, bad: str
) -> None:
    """It goes straight back out in Set-Cookie, so a value carrying a separator would be somebody
    else's directive. Dropped rather than sanitised: the page loses its reset, nothing else."""
    from arkiv.views import ZIP_DONE_COOKIE

    folder = ArchiveFolder.objects.create(name="Billeder")
    one = make_file(folder, name="en.jpg", body=b"bytes")
    client = login(resident_in("a@gahk.dk", None))

    response = client.post(
        f"/intern/arkiv/mappe/{folder.pk}/hent-valgte", {"ids": [one.pk], "done_token": bad}
    )

    assert ZIP_DONE_COOKIE not in response.cookies
    assert response.status_code == 200  # and the download itself is unaffected
