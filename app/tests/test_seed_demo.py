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
def test_seed_demo_fills_the_worker_jobs_page_in_every_state(client: Client) -> None:
    """No worker has run in a seeded database, and none on a Mac measures memory, so the page would
    be empty: every state it shows comes from the seed, through the page's own queries."""
    from django.urls import reverse
    from django.utils import timezone

    from core.demo import task_id
    from core.models import WorkerJobLog
    from residents.views_admin import JobFilters, _child_jobs, _history_counts, _job_details, _running

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    now = timezone.now()

    running = _running(now)
    live = [job for job in running if not job.over_time_limit]
    assert {job.task for job in live} == {
        "gahkflix.tasks.process_video_file",
        "photo_album.tasks.build_media_derivatives",
    }
    encode = next(job for job in live if job.progress is not None)
    assert encode.task_context.startswith("Video: ")
    assert encode.task_context.endswith("(Revy)")
    assert [job.task for job in running if job.over_time_limit] == ["gahkflix.tasks.recover_stalled"]

    counts = _history_counts(JobFilters(include_children=True))
    assert (counts["FAILURE"], counts["STARTED"]) == (3, 3)
    assert counts["SUCCESS"] > 40

    # Memory: so far while it runs, ffmpeg the largest; near the limit for the one that ran out; and
    # "–" where it was not measured.
    lost = WorkerJobLog.objects.get(task_id=task_id("encode-lost"))
    assert lost.peak_memory > 1400 * 1024 * 1024
    assert lost.peak_program == "ffmpeg"
    assert WorkerJobLog.objects.get(task_id=task_id("encode-unmeasured")).peak_memory is None
    assert WorkerJobLog.objects.get(task_id=task_id("upload/running")).peak_memory is None

    # The import names its ZIP and albums, and lists a thumbnail sub-job per picture.
    album_import, _record = _job_details(task_id("album-import"), now)
    assert album_import.task_context.startswith("ZIP: sommerfest-2026.zip (mappe: 2026); albummer: ")
    children, child_counts = _child_jobs(task_id("album-import"))
    assert child_counts == {"SUCCESS": 17}
    assert all(child.task_context.startswith("Medie: ") for child in children)

    client.force_login(Resident.objects.get(email="formand@gahk.dk"))
    assert client.get(reverse("siteadmin:worker_jobs")).status_code == 200
    page = client.get(reverse("siteadmin:worker_job_detail", args=[task_id("encode-running")]))
    assert "(ffmpeg) · indtil nu" in page.content.decode()

    # Seeding again replaces the jobs rather than adding to them.
    total = WorkerJobLog.objects.count()
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    assert WorkerJobLog.objects.count() == total
