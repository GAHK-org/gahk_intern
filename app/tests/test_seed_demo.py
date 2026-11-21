"""Smoke test for the seed_demo dev fixture command: it should run, be idempotent, and produce
data that lights up the app's real queries (active period, roles, derived ølkælder balances)."""

import pytest
from django.core.management import call_command
from django.test import Client

from den_hurtige.models import QuickPost
from oelkaelder.models import Shopper
from residents.models import Resident, active_period
from residents.permissions import real_roles


@pytest.mark.django_db
def test_seed_demo_populates_and_is_idempotent() -> None:
    # --force because Django's test runner forces DEBUG=False, which the safety guard blocks.
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    # 12 seeded residents, one of whom then moves out and is replaced by a newly created one, so the
    # current month stays full (see _seed_residencies). That keeps a leaver for the regnskab page
    # without leaving the alumneliste a person short.
    first_count = Resident.objects.count()
    assert first_count == 13

    # Re-running with --fresh must not duplicate or raise (idempotent).
    # --force because Django's test runner forces DEBUG=False, which the safety guard blocks.
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    assert Resident.objects.count() == first_count

    # active_period follows the seeded current-month residencies.
    year, month = active_period()
    from django.utils import timezone

    today = timezone.localdate()
    assert (year, month) == (today.year, today.month)

    # Documented logins exist with the expected roles and password.
    formand = Resident.objects.get(email="formand@gahk.dk")
    assert "administrator" in real_roles(formand)
    assert formand.check_password("demo1234")

    # Two alumni in the current month lack an e-conomic kontonr., for trying the regnskab page's
    # "Mangler e-conomic kontonr." list; everyone else has one.
    missing = Resident.objects.filter(economic_account_number__isnull=True)
    assert missing.count() == 2
    assert all(r.residencies.filter(year=year, month=month).exists() for r in missing)

    # Derived ølkælder balance is computable (no crash, integer øre).
    shopper = Shopper.objects.first()
    assert isinstance(shopper.balance_ore, int)

    # The leaver/replacement pair: exactly one resident moved out this month and exactly one new
    # one arrived, so the regnskab page has a fraflytter while the alumneliste stays full.
    from datetime import date

    from residents.models import Residency

    leavers = [r for r in Resident.objects.all() if r.move_out_date is not None]
    assert len(leavers) == 1
    leaver = leavers[0]
    assert not leaver.residencies.filter(year=year, month=month).exists(), "a leaver is off this month"
    assert leaver.residencies.exists(), "a leaver was on earlier months, so there is history to settle"

    this_month = Residency.objects.filter(year=year, month=month)
    # Everyone except the one leaver is on this month's list: a full alumneliste, not one short.
    assert this_month.count() == Resident.objects.count() - 1
    assert not this_month.filter(resident=leaver).exists()
    room_ids = list(this_month.values_list("room_id", flat=True))
    assert len(room_ids) == len(set(room_ids)), "one resident per room this month"
    newcomer = Resident.objects.get(move_in_date=date(year, month, 1))
    assert this_month.filter(resident=newcomer).exists()

    from arkiv.models import ArchiveFile, ArchiveFolder

    shared = ArchiveFolder.objects.get(parent=None, name="Fælles dokumenter")
    billeder = ArchiveFolder.objects.get(parent=shared, name="Billeder")
    summer = ArchiveFolder.objects.get(parent=billeder, name="Sommerfest 2026")
    assert ArchiveFile.objects.filter(folder=summer, name="gruppebillede.jpg").exists()
    assert not ArchiveFolder.objects.filter(parent=None, name="Billeder").exists()


@pytest.mark.django_db
def test_seed_demo_fills_the_board_including_the_two_awkward_cases() -> None:
    """The board needs demo content, but two specific rows are what make it useful locally: a pinned
    post (so the pinned-first layout and the 📌 marker are visible without anyone pinning something)
    and one several years old (the board keeps its archive, so the demo should show a genuinely old
    opslag still sitting there and paginating)."""
    from datetime import timedelta

    from django.utils import timezone

    from opslagstavle.models import Notice

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)

    assert Notice.objects.count() >= 6
    assert Notice.objects.pinned().count() == 1
    assert Notice.objects.pinned().first().pinned_by is not None, "a pin with no attributor"
    # An old post is kept on purpose: the board has no retention, so the demo should show that a
    # years-old opslag is still there rather than that something is about to remove it.
    old = timezone.now() - timedelta(days=365 * 2)
    assert Notice.objects.filter(created_at__lt=old).exists(), "no archive post in the demo board"
    # Every category represented, so the filter chips are all reachable in the demo.
    assert len({n.category for n in Notice.objects.all()}) >= 5
    # Posts embedding a demo album and a demo film, so their widgets show without anyone posting one.
    assert Notice.objects.filter(body__contains="](album:").exists()
    assert Notice.objects.filter(body__contains="](gahkflix:").exists()
    assert not hasattr(Notice.objects, "expired"), (
        "retention was removed from opslagstavlen; a reinstated expired() needs the spec updated too"
    )


@pytest.mark.django_db
def test_seed_demo_leaves_one_tombstone_with_its_replies_intact() -> None:
    """Awkward to reach by hand — deleting a freshly posted message takes the silent path — so the
    seeder makes one, with replies."""
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)

    tombstones = QuickPost.objects.filter(deleted_at__isnull=False)

    assert tombstones.count() == 1
    tombstone = tombstones.get()
    assert tombstone.content == "", "a tombstone holds no content"
    assert tombstone.comments.exists(), "the replies outlive the message"


@pytest.mark.django_db
def test_seed_demo_gives_the_logins_notifications_in_every_state() -> None:
    """The bell and the notifications page need something to show on a fresh checkout."""
    from notifications import services
    from notifications.categories import Importance
    from notifications.models import Notification

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    beboer = Resident.objects.get(email="beboer@gahk.dk")
    mine = Notification.objects.filter(recipient=beboer)

    assert services.unseen_count(beboer.pk) == 4
    assert mine.visible().filter(importance=Importance.KRITISK).exists()
    assert mine.visible().filter(seen_at__isnull=False).count() == 2
    assert any(len(n.actors) == 2 for n in mine)  # "Mette og Jonas svarede"
    assert services.purge_expired() == 5  # one long-expired row per documented login
    assert beboer.notification_preferences.filter(push=False).exists()

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)  # still idempotent
    assert Notification.objects.filter(recipient__email="beboer@gahk.dk").count() == 7


@pytest.mark.django_db
def test_seed_demo_gives_the_godkend_page_a_request_of_every_kind(client: Client) -> None:
    """Fotogruppen's approval page is empty unless residents outside it upload, so the seed does it:
    for formand@ to approve and beboer@ to see their own waiting album."""
    from django.urls import reverse
    from django.utils import timezone

    from notifications.categories import PHOTO_ALBUM_APPROVALS, Importance
    from notifications.models import Notification
    from photo_album import approvals
    from photo_album.models import Album, Media, MediaStatus

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    formand = Resident.objects.get(email="formand@gahk.dk")
    beboer = Resident.objects.get(email="beboer@gahk.dk")
    requests = approvals.pending_requests()
    by_album: dict[str, list[approvals.Request]] = {}
    for request in requests:
        by_album.setdefault(request.album.name, []).append(request)
    assert sorted(by_album) == ["Brætspilsaften", "Halloween", "Juleklip", "Sommerfest", "Studietur"]

    # Two residents' uploads to one album: two requests, one with a new sub-album, one over two
    # levels of sub-albums.
    sommerfest = {request.uploader: request for request in by_album["Sommerfest"]}
    assert len(sommerfest) == 2
    bente, other = sommerfest[beboer], next(r for u, r in sommerfest.items() if u != beboer)
    assert {item.album.name for item in bente.media} == {"Sommerfest", "Lørdag", "Natten"}
    assert [album.name for album in other.tops] == ["Søndag"] and other.media
    # A sub-album's uploads, the oldest request, close enough to the purge to make the reminder Høj.
    assert requests[0].album.name == "Studietur"
    assert {item.album.name for item in requests[0].media} == {"Prag"}
    assert requests[0].expires_at - timezone.now() <= approvals.URGENT_BEFORE
    # beboer@'s own new album, its sub-album inside, and an approved one of its name made since.
    (halloween,) = by_album["Halloween"]
    assert halloween.uploader == beboer
    assert [album.name for album in halloween.new_albums] == ["Halloween", "Kostumer"]
    assert Album.objects.filter(name="Halloween", parent=None, folder="2026", pending=False).exists()
    # An album asked for with nothing in it yet, and one whose approval's merge failed.
    (empty,) = by_album["Brætspilsaften"]
    assert empty.new_album and not empty.media
    (failed,) = by_album["Juleklip"]
    assert failed.failed_merges and failed.media
    # A rejection's leftovers: binned items, and a pending album holding only those, in no request.
    rejected = Media.objects.filter(status=MediaStatus.REJECTED, deleted_at__isnull=False)
    assert {item.album.name for item in rejected} == {"Tagterrassen", "Hyttetur"}
    assert Album.objects.get(name="Tagterrassen").pending

    # The page, for an administrator: the list, and the name clash on beboer@'s request.
    client.force_login(formand)
    page = client.get(reverse("photo_album:approvals"))
    assert page.status_code == 200
    assert beboer.full_name in page.content.decode()
    clash = client.get(reverse("photo_album:approval", args=[halloween.album.pk, beboer.pk]))
    assert "approval-clash" in clash.content.decode()
    assert (
        "mislykkedes"
        in client.get(
            reverse("photo_album:approval", args=[failed.album.pk, failed.uploader_pk])
        ).content.decode()
    )
    approvals_rows = Notification.objects.filter(recipient=formand, category=PHOTO_ALBUM_APPROVALS)
    assert approvals_rows.filter(importance=Importance.HOEJ).exists(), "the reminder, Høj"
    assert approvals_rows.exclude(group_key="").get().actors

    # A pending album is its maker's (and Fotogruppen's) alone.
    client.force_login(beboer)
    assert client.get(reverse("photo_album:detail", args=[halloween.album.pk])).status_code == 200
    client.force_login(Resident.objects.get(email="ak@gahk.dk"))
    assert client.get(reverse("photo_album:detail", args=[halloween.album.pk])).status_code == 404

    # A re-seed adds nothing.
    from photo_album import demo

    notifications = Notification.objects.count()
    assert demo.seed(list(Resident.objects.order_by("pk")), timezone.now(), None) == 0  # type: ignore[arg-type]
    assert len(approvals.pending_requests()) == len(requests)
    assert Notification.objects.count() == notifications
