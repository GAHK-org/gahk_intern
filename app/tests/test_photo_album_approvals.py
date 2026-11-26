"""Fotogruppen approving what residents upload, and the albums they make (photo_album.approvals)."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from notifications import categories
from notifications.categories import Importance
from notifications.models import Notification
from photo_album import approvals
from photo_album.models import Album, AlbumMerge, Media, MediaStatus
from photo_album.services import store_upload
from photo_album.tasks import process_album_merge
from residents.models import Resident, Role

from .media_samples import image_upload, jpeg_bytes

Capture = Callable[..., AbstractContextManager[list[Callable[[], object]]]]


@pytest.fixture
def anders(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="anders@gahk.dk", first_name="Anders", last_name="And")


@pytest.fixture
def bente(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="bente@gahk.dk", first_name="Bente", last_name="Bo")


@pytest.fixture
def photographer(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="foto@gahk.dk", first_name="Foto", roles=(Role.FOTO,))


def _upload(album: Album, by: Resident, name: str = "a.jpg", *, approved: bool = False) -> Media:
    media = store_upload(
        album=album, uploaded_file=SimpleUploadedFile(name, jpeg_bytes()), resident=by, approved=approved
    ).media
    assert media is not None
    return media


def _request(album: Album, uploader: Resident | None) -> approvals.Request:
    found = approvals.find(album.pk, uploader.pk if uploader else 0)
    assert found is not None
    return found


def _approvals_for(resident: Resident) -> list[Notification]:
    return list(Notification.objects.filter(recipient=resident, category=categories.PHOTO_ALBUM_APPROVALS))


# --- What a request is --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_request_is_one_residents_uploads_to_one_album_tree(anders: Resident, bente: Resident) -> None:
    party = Album.objects.create(folder="2026", name="Sommerfest")
    saturday = Album.objects.create(folder="2026", name="Lørdag", parent=party)
    other = Album.objects.create(folder="2026", name="Julefrokost")
    mine = [
        _upload(party, anders, "a.jpg"),
        _upload(saturday, anders, "b.jpg"),
        _upload(party, anders, "c.jpg"),
    ]
    _upload(party, bente, "d.jpg")
    _upload(party, bente, "e.jpg")
    _upload(other, anders, "f.jpg")
    _upload(party, anders, "approved.jpg", approved=True)

    requests = approvals.pending_requests()

    assert sorted((r.album.name, r.uploader.email, len(r.media)) for r in requests if r.uploader) == [
        ("Julefrokost", "anders@gahk.dk", 1),
        ("Sommerfest", "anders@gahk.dk", 3),
        ("Sommerfest", "bente@gahk.dk", 2),
    ]
    found = _request(party, anders)
    # The root album's own items first, by name, then each sub-album's.
    assert [item.pk for item in found.media] == [mine[0].pk, mine[2].pk, mine[1].pk]


@pytest.mark.django_db
def test_approving_approves_what_was_shown_but_what_was_picked_for_rejection(
    anders: Resident, photographer: Resident
) -> None:
    album = Album.objects.create(folder="2026", name="Sommerfest")
    good, bad, also_good = (_upload(album, anders, name) for name in ("a.jpg", "b.jpg", "c.jpg"))
    pending = _request(album, anders)
    late = _upload(album, anders, "d.jpg")  # arrived while the approver looked

    decision = approvals.approve(
        pending, by=photographer, shown={good.pk, bad.pk, also_good.pk}, rejected={bad.pk}
    )

    assert (decision.approved, decision.rejected, decision.left) == (2, 1, 1)
    for item, status in (
        (good, MediaStatus.APPROVED),
        (also_good, MediaStatus.APPROVED),
        (late, MediaStatus.PENDING),
    ):
        item.refresh_from_db()
        assert item.status == status
    bad.refresh_from_db()
    assert bad.status == MediaStatus.REJECTED and bad.deleted_at is not None  # in the bin
    assert good.approved_by == photographer
    assert [len(r.media) for r in approvals.pending_requests()] == [1]


@pytest.mark.django_db
def test_rejecting_a_request_bins_what_was_shown(anders: Resident, photographer: Resident) -> None:
    album = Album.objects.create(folder="2026", name="Sommerfest")
    items = [_upload(album, anders, name) for name in ("a.jpg", "b.jpg")]

    decision = approvals.reject(_request(album, anders), by=photographer, shown={item.pk for item in items})

    assert decision.rejected == 2
    assert set(Media.objects.values_list("status", flat=True)) == {MediaStatus.REJECTED}
    assert approvals.pending_requests() == []


@pytest.mark.django_db
def test_a_decided_request_cannot_be_decided_again(anders: Resident, photographer: Resident) -> None:
    album = Album.objects.create(folder="2026", name="Nyt", pending=True, created_by=anders)
    pending = _request(album, anders)
    approvals.approve(pending, by=photographer, shown=set(), rejected=set())

    with pytest.raises(ValidationError):
        approvals.approve(pending, by=photographer, shown=set(), rejected=set())


# --- The pages ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_fotogruppen_sees_the_approval_pages(client: Client, anders: Resident) -> None:
    album = Album.objects.create(folder="2026", name="Sommerfest")
    _upload(album, anders)
    client.force_login(anders)

    assert client.get(reverse("photo_album:approvals")).status_code == 403
    assert client.get(reverse("photo_album:approval", args=[album.pk, anders.pk])).status_code == 403
    assert client.get(reverse("photo_album:approval_albums"), {"folder": "2026"}).status_code == 403


@pytest.mark.django_db
def test_the_approval_page_shows_everything_and_approves_all_but_the_picked(
    client: Client, anders: Resident, photographer: Resident, django_capture_on_commit_callbacks: Capture
) -> None:
    album = Album.objects.create(folder="2026", name="Sommerfest")
    good, bad = _upload(album, anders, "a.jpg"), _upload(album, anders, "b.jpg")
    client.force_login(photographer)

    listing = client.get(reverse("photo_album:approvals")).content.decode()
    assert "Anders And" in listing and "Sommerfest" in listing
    url = reverse("photo_album:approval", args=[album.pk, anders.pk])
    page = client.get(url).content.decode()
    for item in (good, bad):
        assert f'name="shown" value="{item.pk}"' in page
        assert f'name="reject" value="{item.pk}"' in page

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url, {"action": "approve", "shown": [good.pk, bad.pk], "reject": [bad.pk], "note": "Uskarpt"}
        )

    assert response.status_code == 302 and response["Location"] == reverse("photo_album:approvals")
    good.refresh_from_db()
    bad.refresh_from_db()
    assert (good.status, bad.status) == (MediaStatus.APPROVED, MediaStatus.REJECTED)
    told = Notification.objects.get(recipient=anders, category=categories.GENERAL)
    assert told.title == "1 af dine uploads til Sommerfest er godkendt"
    assert told.body == "1 blev afvist. Fotogruppen skriver: Uskarpt"
    # Decided: the page sends the next look back to the list.
    assert client.get(url)["Location"] == reverse("photo_album:approvals")


# --- Albums awaiting approval -------------------------------------------------------------------


@pytest.mark.django_db
def test_a_new_album_is_its_makers_and_fotogruppens_until_approved(
    client: Client, anders: Resident, bente: Resident, photographer: Resident
) -> None:
    client.force_login(anders)
    client.post(reverse("photo_album:create_album"), {"folder": "2026", "name": "Kælderfest"})
    album = Album.objects.get(name="Kælderfest")
    upload = reverse("photo_album:upload", args=[album.pk])
    assert client.post(upload, {"uploads": [image_upload("a.jpg")]}).status_code == 302
    assert Media.objects.get().status == MediaStatus.PENDING
    assert "Kælderfest" in client.get(reverse("photo_album:index")).content.decode()
    assert client.get(reverse("photo_album:detail", args=[album.pk])).status_code == 200

    client.force_login(bente)
    assert "Kælderfest" not in client.get(reverse("photo_album:index")).content.decode()
    assert client.get(reverse("photo_album:detail", args=[album.pk])).status_code == 404
    assert client.post(upload, {"uploads": [image_upload("b.jpg")]}).status_code == 403

    client.force_login(photographer)
    assert "Kælderfest" in client.get(reverse("photo_album:index")).content.decode()
    page = client.get(reverse("photo_album:detail", args=[album.pk])).content.decode()
    assert reverse("photo_album:approval", args=[album.pk, anders.pk]) in page
    # Not even Fotogruppen uploads to it: what is in it is the maker's, approved with it.
    assert client.post(upload, {"uploads": [image_upload("c.jpg")]}).status_code == 403


@pytest.mark.django_db
def test_a_new_album_is_approved_under_the_name_and_in_the_folder_chosen(
    client: Client, anders: Resident, photographer: Resident, django_capture_on_commit_callbacks: Capture
) -> None:
    album = Album.objects.create(folder="2026", name="kælderfest", pending=True, created_by=anders)
    item = _upload(album, anders)
    client.force_login(photographer)

    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            reverse("photo_album:approval", args=[album.pk, anders.pk]),
            {
                "action": "approve",
                "shown": [item.pk],
                "album": [album.pk],
                f"album_action_{album.pk}": "keep",
                f"name_{album.pk}": "Kælderfest",
                f"folder_{album.pk}": "2025",
            },
        )

    album.refresh_from_db()
    item.refresh_from_db()
    assert (album.pending, album.name, album.folder) == (False, "Kælderfest", "2025")
    assert item.status == MediaStatus.APPROVED
    told = Notification.objects.get(recipient=anders, category=categories.GENERAL)
    assert told.title == "Dit album Kælderfest er godkendt"
    assert told.body == "kælderfest hedder nu Kælderfest i 2025."


@pytest.mark.django_db
def test_an_album_awaiting_approval_holds_no_name(
    client: Client, anders: Resident, bente: Resident, photographer: Resident
) -> None:
    """Others may make or ask for an album of the same name meanwhile; the approver renames or merges."""
    client.force_login(anders)
    client.post(reverse("photo_album:create_album"), {"folder": "2026", "name": "Foobar"})
    client.force_login(bente)
    client.post(reverse("photo_album:create_album"), {"folder": "2026", "name": "foobar"})
    client.force_login(photographer)
    client.post(reverse("photo_album:create_album"), {"folder": "2026", "name": "FOOBAR"})

    assert sorted(Album.objects.values_list("name", "pending")) == [
        ("FOOBAR", False),
        ("Foobar", True),
        ("foobar", True),
    ]
    # But not one of the name of an approved album, which is the one to upload to; nor twice.
    client.force_login(bente)
    client.post(reverse("photo_album:create_album"), {"folder": "2026", "name": "Foobar"})
    assert Album.objects.count() == 3


@pytest.mark.django_db
def test_a_name_taken_meanwhile_starts_the_page_on_merging_and_refuses_keeping(
    client: Client, anders: Resident, photographer: Resident
) -> None:
    album = Album.objects.create(folder="2026", name="Kælderfest", pending=True, created_by=anders)
    existing = Album.objects.create(folder="2026", name="kælderfest")
    keep, drop = _upload(album, anders, "a.jpg"), _upload(album, anders, "b.jpg")
    url = reverse("photo_album:approval", args=[album.pk, anders.pk])
    client.force_login(photographer)

    page = client.get(url).content.decode()
    assert "Der findes allerede et album, der hedder <strong>kælderfest</strong>" in page
    assert f'name="album_action_{album.pk}" value="merge" checked' in page
    assert f'<option value="{existing.pk}" selected>kælderfest</option>' in page

    response = client.post(
        url,
        {
            "action": "approve",
            "shown": [keep.pk, drop.pk],
            "reject": [drop.pk],
            "album": [album.pk],
            f"album_action_{album.pk}": "keep",
            f"name_{album.pk}": "Kælderfest",
            f"folder_{album.pk}": "2026",
        },
    )

    assert response.status_code == 200  # the page again, with the picks kept
    assert "Der findes allerede et album, der hedder Kælderfest, i 2026." in response.content.decode()
    assert f'value="{drop.pk}" data-approval-reject checked' in response.content.decode()
    album.refresh_from_db()
    assert album.pending
    assert set(Media.objects.values_list("status", flat=True)) == {MediaStatus.PENDING}


@pytest.mark.django_db
def test_a_new_album_merged_into_another_folders_album_is_approved_by_the_merge(
    anders: Resident, photographer: Resident, django_capture_on_commit_callbacks: Capture
) -> None:
    target = Album.objects.create(folder="2025", name="Sommerfest")
    existing = _upload(target, photographer, "old.jpg", approved=True)
    album = Album.objects.create(folder="2026", name="sommerfest!", pending=True, created_by=anders)
    keep, drop = _upload(album, anders, "a.jpg"), _upload(album, anders, "b.jpg")

    with django_capture_on_commit_callbacks(execute=False) as queued:
        decision = approvals.approve(
            _request(album, anders),
            by=photographer,
            shown={keep.pk, drop.pk},
            rejected={drop.pk},
            choices={album.pk: approvals.AlbumChoice(merge_into=target)},
        )

    [(_, into, job)] = decision.merges
    assert into == target
    keep.refresh_from_db()
    assert keep.status == MediaStatus.PENDING  # until it is where it belongs
    assert approvals.pending_requests() == []  # being merged, so not waiting

    for callback in queued:  # the merge job (run at once: tasks are eager in tests), the notification
        callback()

    job.refresh_from_db()
    assert job.state == "ready"
    assert not Album.objects.filter(pk=album.pk).exists()
    keep.refresh_from_db()
    drop.refresh_from_db()
    assert (keep.album, keep.status, keep.approved_by) == (target, MediaStatus.APPROVED, photographer)
    assert (drop.album, drop.status) == (target, MediaStatus.REJECTED)  # in its bin
    assert Media.objects.filter(album=target).count() == 3 and existing.pk
    told = Notification.objects.get(recipient=anders, category=categories.GENERAL)
    assert (told.title, told.body) == (
        "Dine uploads til sommerfest! er godkendt",
        "Det fra sommerfest! lægges i albummet Sommerfest i 2025. 1 blev afvist.",
    )


@pytest.mark.django_db
def test_a_merged_new_album_takes_its_new_sub_albums_along(anders: Resident, photographer: Resident) -> None:
    target = Album.objects.create(folder="2026", name="Sommerfest")
    friday_there = Album.objects.create(folder="2026", name="Fredag", parent=target)
    someone_elses = Album.objects.create(folder="2026", name="Lørdag", parent=target, pending=True)
    album = Album.objects.create(folder="2026", name="Fest", pending=True, created_by=anders)
    friday = Album.objects.create(folder="2026", name="fredag", parent=album, pending=True, created_by=anders)
    saturday = Album.objects.create(
        folder="2026", name="Lørdag", parent=album, pending=True, created_by=anders
    )
    on_friday, on_saturday = _upload(friday, anders, "a.jpg"), _upload(saturday, anders, "b.jpg")
    pending = _request(album, anders)
    assert [each.pk for each in pending.tops] == [album.pk]

    approvals.approve(
        pending,
        by=photographer,
        shown={on_friday.pk, on_saturday.pk},
        rejected=set(),
        choices={album.pk: approvals.AlbumChoice(merge_into=target)},
    )
    job = AlbumMerge.objects.get()
    assert process_album_merge.delay(job.pk).get() is True

    on_friday.refresh_from_db()
    on_saturday.refresh_from_db()
    saturday.refresh_from_db()
    assert (on_friday.album, on_friday.status) == (friday_there, MediaStatus.APPROVED)
    # Not into someone else's album awaiting approval: beside it, approved.
    assert (saturday.parent, saturday.pending) == (target, False)
    assert (on_saturday.album, on_saturday.status) == (saturday, MediaStatus.APPROVED)
    assert someone_elses.pk and not Album.objects.filter(pk__in=[album.pk, friday.pk]).exists()


@pytest.mark.django_db
def test_a_merge_that_fails_puts_the_request_back(anders: Resident, photographer: Resident) -> None:
    target = Album.objects.create(folder="2026", name="Sommerfest")
    album = Album.objects.create(folder="2026", name="Fest", pending=True, created_by=anders)
    item = _upload(album, anders)
    decision = approvals.approve(
        _request(album, anders),
        by=photographer,
        shown={item.pk},
        rejected=set(),
        choices={album.pk: approvals.AlbumChoice(merge_into=target)},
    )
    Album.objects.filter(pk=target.pk).update(manually_locked_at=timezone.now())  # locked meanwhile

    [(_, _, job)] = decision.merges
    assert process_album_merge.delay(job.pk).get() is False

    item.refresh_from_db()
    assert item.status == MediaStatus.PENDING and item.album_id == album.pk
    [again] = approvals.pending_requests()
    [failed] = again.failed_merges
    assert "låst" in failed.error


@pytest.mark.django_db
def test_a_new_album_cannot_be_merged_into_a_locked_one(anders: Resident, photographer: Resident) -> None:
    target = Album.objects.create(folder="2026", name="Sommerfest", manually_locked_at=timezone.now())
    album = Album.objects.create(folder="2026", name="Fest", pending=True, created_by=anders)
    item = _upload(album, anders)

    with pytest.raises(ValidationError):
        approvals.approve(
            _request(album, anders),
            by=photographer,
            shown={item.pk},
            rejected={item.pk},
            choices={album.pk: approvals.AlbumChoice(merge_into=target)},
        )

    item.refresh_from_db()
    assert item.status == MediaStatus.PENDING  # the rejection went with the rest
    assert not AlbumMerge.objects.exists()


@pytest.mark.django_db
def test_rejecting_a_new_album_bins_what_is_in_it_until_restored(
    client: Client, anders: Resident, photographer: Resident, django_capture_on_commit_callbacks: Capture
) -> None:
    album = Album.objects.create(folder="2026", name="Fest", pending=True, created_by=anders)
    empty = Album.objects.create(folder="2026", name="Tom", parent=album, pending=True, created_by=anders)
    item = _upload(album, anders)

    with django_capture_on_commit_callbacks(execute=True):
        approvals.reject(_request(album, anders), by=photographer, shown={item.pk}, note="Privat")

    item.refresh_from_db()
    assert (item.status, item.album_id) == (MediaStatus.REJECTED, album.pk) and item.deleted_at is not None
    assert Album.objects.get(pk=album.pk).pending  # kept, hidden, for its bin
    assert not Album.objects.filter(pk=empty.pk).exists()  # nothing of it to restore
    assert approvals.pending_requests() == []
    told = Notification.objects.get(recipient=anders, category=categories.GENERAL)
    assert (told.title, told.body) == (
        "Dit album Fest blev afvist",
        "Fotogruppen kan fortryde det i 30 dage. Fotogruppen skriver: Privat",
    )
    client.force_login(anders)
    assert (
        "Fotogruppen har afvist albummet."
        in client.get(reverse("photo_album:detail", args=[album.pk])).content.decode()
    )

    client.force_login(photographer)  # changed their mind
    assert "Fra album: Fest" in client.get(reverse("photo_album:album_bin", args=[album.pk])).content.decode()
    client.post(reverse("photo_album:restore", args=[item.pk]))

    [again] = approvals.pending_requests()
    assert (again.album, [each.pk for each in again.media], again.new_album) == (album, [item.pk], True)


@pytest.mark.django_db
def test_rejected_uploads_to_an_album_that_exists_go_to_the_bin(
    anders: Resident, photographer: Resident
) -> None:
    album = Album.objects.create(folder="2026", name="Sommerfest")
    item = _upload(album, anders)

    approvals.reject(_request(album, anders), by=photographer, shown={item.pk})

    item.refresh_from_db()
    assert item.status == MediaStatus.REJECTED and item.deleted_at is not None


@pytest.mark.django_db
def test_the_maker_may_withdraw_a_new_album(client: Client, anders: Resident, bente: Resident) -> None:
    album = Album.objects.create(folder="2026", name="Fest", pending=True, created_by=anders)
    _upload(album, anders)
    withdraw = reverse("photo_album:withdraw_album", args=[album.pk])

    client.force_login(bente)
    assert client.post(withdraw).status_code == 403
    client.force_login(anders)
    assert client.post(withdraw).status_code == 302

    assert not Album.objects.filter(pk=album.pk).exists() and not Media.objects.exists()


@pytest.mark.django_db
def test_a_new_album_is_no_merge_target_and_no_board_embed(anders: Resident) -> None:
    from opslagstavle.embeds import _album_choices, _album_shareable
    from photo_album import merge

    approved = Album.objects.create(folder="2026", name="Fest")
    pending = Album.objects.create(folder="2026", name="Fest 2", pending=True, created_by=anders)

    assert [candidate.album for candidate in merge.merge_targets(approved)] == []
    assert _album_shareable(None, pending.pk) is None  # type: ignore[arg-type]
    assert [choice.pk for choice in _album_choices(None, "Fest")] == [approved.pk]  # type: ignore[arg-type]


# --- Notifications ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_fotogruppen_hears_of_a_request_once(
    client: Client, anders: Resident, bente: Resident, photographer: Resident
) -> None:
    album = Album.objects.create(folder="2026", name="Sommerfest")
    upload = reverse("photo_album:upload", args=[album.pk])

    client.force_login(anders)
    client.post(upload, {"uploads": [image_upload("a.jpg")]})
    client.post(upload, {"uploads": [image_upload("b.jpg")]})  # the same request
    [row] = _approvals_for(photographer)
    assert row.title == "Anders And har uploadet til fotoalbummet"
    assert row.body == "Venter på godkendelse: Sommerfest i 2026."
    assert row.url == reverse("photo_album:approvals")

    client.force_login(bente)
    client.post(upload, {"uploads": [image_upload("c.jpg")]})
    [row] = _approvals_for(photographer)  # folded into the same row
    assert row.title == "Bente Bo og Anders And har uploadet til fotoalbummet"

    client.force_login(photographer)
    client.post(upload, {"uploads": [image_upload("d.jpg")]})  # approved at once: nothing to tell
    assert not _approvals_for(anders) and len(_approvals_for(photographer)) == 1


@pytest.mark.django_db
def test_a_new_album_is_told_about_when_it_is_made_not_again_on_upload(
    client: Client, anders: Resident, photographer: Resident
) -> None:
    client.force_login(anders)
    client.post(reverse("photo_album:create_album"), {"folder": "2026", "name": "Kælderfest"})
    Notification.objects.filter(recipient=photographer).update(seen_at=timezone.now())
    album = Album.objects.get()
    client.post(reverse("photo_album:upload", args=[album.pk]), {"uploads": [image_upload("a.jpg")]})

    [row] = _approvals_for(photographer)
    assert row.body == "Venter på godkendelse: et nyt album, Kælderfest, i 2026."
    assert row.seen_at is not None  # not told again


@pytest.mark.django_db
def test_without_fotogruppen_the_administrators_are_told(
    client: Client, anders: Resident, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Sommerfest")
    client.force_login(anders)

    client.post(reverse("photo_album:upload", args=[album.pk]), {"uploads": [image_upload("a.jpg")]})

    assert len(_approvals_for(administrator)) == 1


@pytest.mark.django_db
def test_the_daily_reminder_names_the_oldest_request(
    anders: Resident, bente: Resident, photographer: Resident
) -> None:
    album = Album.objects.create(folder="2026", name="Sommerfest")
    old = _upload(album, anders)
    _upload(album, bente)  # today's: told about already, not reminded of
    Media.objects.filter(pk=old.pk).update(added_at=timezone.now() - timedelta(days=5))

    assert approvals.remind() == 1

    [row] = _approvals_for(photographer)
    assert row.title == "1 upload venter på godkendelse"
    assert row.body.startswith("Den ældste, fra Anders And til Sommerfest, venter fra ")
    assert row.importance == Importance.NORMAL
    assert row.expires_at <= timezone.now() + timedelta(days=1)


@pytest.mark.django_db
def test_the_reminder_is_urgent_days_before_uploads_go(anders: Resident, photographer: Resident) -> None:
    album = Album.objects.create(folder="2026", name="Sommerfest")
    old = _upload(album, anders)
    Media.objects.filter(pk=old.pk).update(added_at=timezone.now() - timedelta(days=28))

    approvals.remind()

    [row] = _approvals_for(photographer)
    assert row.importance == Importance.HOEJ


@pytest.mark.django_db
def test_no_reminder_without_anything_waiting_a_day(anders: Resident, photographer: Resident) -> None:
    _upload(Album.objects.create(folder="2026", name="Sommerfest"), anders)

    assert approvals.remind() == 0
    assert not _approvals_for(photographer)


# --- Clean-up -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_new_album_left_empty_for_30_days_goes(anders: Resident) -> None:
    old = Album.objects.create(folder="2026", name="Gammel", pending=True, created_by=anders)
    fresh = Album.objects.create(folder="2026", name="Ny", pending=True, created_by=anders)
    used = Album.objects.create(folder="2026", name="Brugt", pending=True, created_by=anders)
    approved = Album.objects.create(folder="2026", name="Godkendt")
    inside = Album.objects.create(folder="2026", name="Indeni", parent=old, pending=True, created_by=anders)
    _upload(used, anders)
    month_ago = timezone.now() - timedelta(days=31)
    Album.objects.filter(pk__in=[old.pk, inside.pk, used.pk, approved.pk]).update(created_at=month_ago)

    assert approvals.purge_expired_albums() == 2  # the sub-album first, then the album

    assert set(Album.objects.values_list("name", flat=True)) == {"Ny", "Brugt", "Godkendt"}
    assert fresh.pk


@pytest.mark.django_db
def test_an_import_neither_minds_nor_adds_to_an_album_awaiting_approval(anders: Resident) -> None:
    import zipfile
    from io import BytesIO

    from photo_album.services import import_zip_album

    waiting = Album.objects.create(folder="2026", name="Sommerfest", pending=True, created_by=anders)
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("Sommerfest/a.jpg", jpeg_bytes())

    for merge in (False, True):  # the import page, and the Dropbox script's import
        imported = import_zip_album(
            archive=SimpleUploadedFile("Sommerfest.zip", payload.getvalue()),
            folder="2026",
            resident=anders,
            merge=merge,
        )
        assert waiting not in imported.albums
    assert not waiting.media.exists()
    assert Album.objects.filter(name="Sommerfest", pending=False).count() == 1


# --- Sub-albums ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_resident_asks_for_a_sub_album_which_only_they_and_fotogruppen_see(
    client: Client, anders: Resident, bente: Resident, photographer: Resident
) -> None:
    party = Album.objects.create(folder="2026", name="Sommerfest")
    client.force_login(anders)
    assert "Nyt underalbum" in client.get(reverse("photo_album:detail", args=[party.pk])).content.decode()
    client.post(reverse("photo_album:create_sub_album", args=[party.pk]), {"name": "Fredag"})
    friday = Album.objects.get(name="Fredag")
    assert friday.pending and friday.parent == party and friday.created_by == anders
    client.post(reverse("photo_album:upload", args=[friday.pk]), {"uploads": [image_upload("a.jpg")]})

    other = Client()  # not carrying Anders's messages along
    other.force_login(bente)
    assert "Fredag" not in other.get(reverse("photo_album:detail", args=[party.pk])).content.decode()
    assert other.get(reverse("photo_album:detail", args=[friday.pk])).status_code == 404

    client.force_login(photographer)
    assert "Fredag" in client.get(reverse("photo_album:detail", args=[party.pk])).content.decode()
    [pending] = approvals.pending_requests()
    assert (pending.album, pending.uploader, [each.pk for each in pending.tops]) == (
        party,
        anders,
        [friday.pk],
    )
    approvals.approve(pending, by=photographer, shown={pending.media[0].pk}, rejected=set())
    friday.refresh_from_db()
    assert not friday.pending and Media.objects.get().status == MediaStatus.APPROVED


@pytest.mark.django_db
def test_sub_albums_of_an_album_awaiting_approval_are_its_makers(
    client: Client, anders: Resident, photographer: Resident
) -> None:
    album = Album.objects.create(folder="2026", name="Fest", pending=True, created_by=anders)
    url = reverse("photo_album:create_sub_album", args=[album.pk])

    client.force_login(photographer)
    assert client.post(url, {"name": "Fredag"}).status_code == 403
    client.force_login(anders)
    client.post(url, {"name": "Fredag"})
    assert Album.objects.get(name="Fredag").pending
    client.post(url, {"name": "fredag"})  # their own, twice
    assert Album.objects.filter(name__iexact="fredag").count() == 1

    pending = _request(album, anders)
    approvals.approve(pending, by=photographer, shown=set(), rejected=set())
    assert not Album.objects.filter(pending=True).exists()


@pytest.mark.django_db
def test_new_sub_albums_are_decided_each_on_their_own(anders: Resident, photographer: Resident) -> None:
    """Two merges into one folder at once: approvals do not wait for each other."""
    party = Album.objects.create(folder="2026", name="Sommerfest")
    weekend = Album.objects.create(folder="2026", name="Weekend", parent=party)
    friday = Album.objects.create(folder="2026", name="Fredag", parent=party, pending=True, created_by=anders)
    saturday = Album.objects.create(
        folder="2026", name="Lørdag", parent=party, pending=True, created_by=anders
    )
    sunday = Album.objects.create(folder="2026", name="Søndag", parent=party, pending=True, created_by=anders)
    items = [_upload(each, anders, f"{each.name}.jpg") for each in (friday, saturday, sunday)]
    pending = _request(party, anders)

    approvals.approve(
        pending,
        by=photographer,
        shown={item.pk for item in items},
        rejected=set(),
        choices={
            friday.pk: approvals.AlbumChoice(merge_into=weekend),
            saturday.pk: approvals.AlbumChoice(merge_into=party),
            sunday.pk: approvals.AlbumChoice(name="Søndag formiddag"),
        },
    )
    for job in AlbumMerge.objects.order_by("pk"):
        assert process_album_merge.delay(job.pk).get() is True

    assert [Media.objects.get(pk=item.pk).album_id for item in items] == [weekend.pk, party.pk, sunday.pk]
    sunday.refresh_from_db()
    assert (sunday.name, sunday.pending) == ("Søndag formiddag", False)
    assert set(Media.objects.values_list("status", flat=True)) == {MediaStatus.APPROVED}


@pytest.mark.django_db
def test_a_new_sub_album_is_told_about(client: Client, anders: Resident, photographer: Resident) -> None:
    party = Album.objects.create(folder="2026", name="Sommerfest")
    client.force_login(anders)

    client.post(reverse("photo_album:create_sub_album", args=[party.pk]), {"name": "Fredag"})
    friday = Album.objects.get(name="Fredag")
    client.post(reverse("photo_album:upload", args=[friday.pk]), {"uploads": [image_upload("a.jpg")]})

    [row] = _approvals_for(photographer)
    assert row.body == "Venter på godkendelse: et nyt underalbum, Fredag, i Sommerfest (2026)."
