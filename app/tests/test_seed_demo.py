"""Smoke test for the seed_demo dev fixture command: it should run, be idempotent, and produce
data that lights up the app's real queries (active period, roles, derived ølkælder balances)."""

from datetime import date

import pytest
from django.core.management import call_command

from den_hurtige.models import QuickPost
from drive.access import Viewer
from drive.models import Folder
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
def test_seed_demo_fills_drive_for_every_login() -> None:
    """Drive starts empty, so a fresh checkout needs something in every state each part of Drive
    has. One seed for all of them, as a seed run is slow, with the default number of residents:
    some cases are a colleague's change to a login's file, so a group needs more than one member.
    The first test above covers a second --fresh run over Drive's rows."""
    call_command("seed_demo", "--fresh", "--force", verbosity=0)
    seeded = _DriveSeed()

    _check_drive_structure(seeded)
    _check_drive_files(seeded)
    _check_drive_locks(seeded)
    _check_drive_shared(seeded)
    _check_drive_sharing(seeded)
    _check_drive_standing(seeded)
    _check_drive_preview(seeded)
    _check_drive_search(seeded)
    _check_drive_office(seeded)
    _check_drive_readme(seeded)
    _check_drive_text(seeded)


class _DriveSeed:
    def __init__(self) -> None:
        from django.utils import timezone

        from drive.periods import period_containing

        self.today = timezone.localdate()
        self.current = period_containing(self.today)
        self.by_email = {r.email: r for r in Resident.objects.all()}

    def viewer(self, email: str, *, oversees: bool = False) -> Viewer:
        return Viewer.build(self.by_email[email].pk, oversees=oversees, today=self.today)

    def group(self, email: str) -> Folder:
        """The login's group folder this month."""
        residency = self.by_email[email].residencies.get(year=self.today.year, month=self.today.month)
        return Folder.objects.get(kind=Folder.Kind.GROUP, workgroup=residency.workgroup)

    def period(self, email: str, start: date) -> Folder:
        return self.group(email).children.get(period_start=start)


def _check_drive_structure(seeded: _DriveSeed) -> None:
    """Folders some logins may open, some only see the size of, and some not see at all."""
    from drive import services
    from drive.access import Level
    from drive.models import File

    today, current = seeded.today, seeded.current
    now_folder = seeded.period("regnskab@gahk.dk", current.start)
    older = seeded.period("regnskab@gahk.dk", current.previous().previous().start)
    assert File.objects.filter(folder__path__startswith=now_folder.path).count() >= 4
    assert seeded.viewer("regnskab@gahk.dk").level(now_folder) == Level.WRITE
    assert seeded.viewer("regnskab@gahk.dk").level(older) == Level.READ
    # The overseers read earlier periods, and see only the name and size of a current one of a group
    # they are not in themselves.
    formand = seeded.viewer("formand@gahk.dk", oversees=True)
    assert formand.level(older) == Level.READ
    others = [
        seeded.period(email, current.start)
        for email in ("regnskab@gahk.dk", "ak@gahk.dk", "oel@gahk.dk")
        if seeded.group(email) != seeded.group("formand@gahk.dk")
    ]
    assert others
    assert all(formand.level(folder) == Level.META for folder in others)
    # A plain resident sees only the groups they have been in, and their note is in this month's.
    beboer = seeded.viewer("beboer@gahk.dk")
    groups = list(Folder.objects.filter(kind=Folder.Kind.GROUP))
    visible = [group for group in groups if beboer.level(group) > Level.NONE]
    assert 0 < len(visible) < len(groups)
    own = seeded.group("beboer@gahk.dk")
    assert own in visible
    assert File.objects.filter(
        folder__path__startswith=own.path, created_by=seeded.by_email["beboer@gahk.dk"]
    ).exists()

    # Fælles: this year and last year to write in, the year before to read only.
    years = {f.period_start.year: f for f in Folder.objects.filter(kind=Folder.Kind.YEAR)}
    assert beboer.level(years[today.year]) == Level.WRITE
    assert beboer.level(years[today.year - 1]) == Level.WRITE
    assert beboer.level(years[today.year - 2]) == Level.READ
    assert all(File.objects.filter(folder=years[y]).exists() for y in (today.year - 1, today.year - 2))

    # Real bytes of many kinds, so each downloads and opens.
    names = list(File.objects.values_list("name", flat=True))
    assert {n.rsplit(".", 1)[-1] for n in names} >= {"pdf", "xlsx", "docx", "csv", "md", "png", "jpg", "txt"}
    for file in File.objects.all():
        version = file.latest_version()
        with services.storage().open(version.key, "rb") as stored:
            assert len(stored.read()) == file.size > 0


def _check_drive_files(seeded: _DriveSeed) -> None:
    """The bin, versions and history: each needs something done to a file after it was uploaded."""
    from datetime import timedelta

    from django.utils import timezone

    from drive import services
    from drive.models import BIN_DAYS, Event, File
    from notifications.models import Notification

    regnskab = seeded.by_email["regnskab@gahk.dk"]
    # In the bin: one deleted by a colleague (its creator is told), one about to go for good, and a
    # deleted folder kept for the binned file in it.
    binned = File.objects.filter(deleted_at__isnull=False)
    receipt = binned.get(name="Kvittering 3.jpg")
    assert receipt.deleted_by != regnskab
    assert Notification.objects.filter(recipient=regnskab, url__endswith="/bin").exists()
    assert binned.filter(deleted_at__lt=timezone.now() - timedelta(days=BIN_DAYS - 5)).exists()
    assert binned.filter(folder__deleted_at__isnull=False).exists()
    # Versions: a colleague's version after regnskab@ downloaded the file warns on replacing it, and
    # an old version restored on top.
    budget = File.objects.get(name=f"Budget {seeded.current.name}.xlsx")
    try:
        services.check_stale(budget, regnskab.pk)
        raise AssertionError("replacing the budget should warn that a colleague changed it")
    except services.Stale:
        pass
    assert File.objects.get(name="Kontoplan.xlsx").versions.filter(restored_from=1).exists()
    # History: every kind of line, and ak@ told that somebody renamed their file.
    lines = set(Event.objects.values_list("action", flat=True))
    assert lines >= {
        "created",
        "version",
        "version_restored",
        "renamed",
        "moved",
        "copied",
        "deleted",
        "restored",
    }
    assert Notification.objects.filter(recipient__email="ak@gahk.dk", title__contains="omdøbte").exists()
    assert Folder.objects.filter(deleted_at__isnull=False).count() == 1


def _check_drive_locks(seeded: _DriveSeed) -> None:
    """Locks and the requests page: pending, approved and rejected unlocks and purges."""
    from django.db.models import Q
    from django.utils import timezone

    from drive.models import ApprovalRequest, Event, File, Folder

    requests = ApprovalRequest.objects.all()
    for kind in ApprovalRequest.Kind.values:
        for state in ApprovalRequest.State.values:
            assert requests.filter(kind=kind, state=state).exists(), (kind, state)
    # Pending ones are for admin@ and formand@ to decide: neither of them asked.
    pending = requests.filter(state=ApprovalRequest.State.PENDING)
    assert not pending.filter(requested_by__email__in=["admin@gahk.dk", "formand@gahk.dk"]).exists()
    # Locked by the nightly task and by hand, a locked folder, and a file unlocked for now.
    assert File.objects.filter(locked_at__isnull=False, locked_by__isnull=True).exists()
    assert File.objects.filter(locked_at__isnull=False, locked_by__isnull=False).exists()
    assert Folder.objects.filter(kind=Folder.Kind.FOLDER, locked_at__isnull=False).exists()
    assert File.objects.filter(unlocked_until__gt=timezone.now()).exists()
    lines = set(Event.objects.values_list("action", flat=True))
    assert lines >= {
        "locked",
        "unlock_requested",
        "unlock_decided",
        "purge_requested",
        "purge_decided",
        "purged",
    }
    # The space's activity page has lines with their place.
    assert not Event.objects.filter(Q(space="") | Q(space__isnull=True)).exists()


def _check_drive_shared(seeded: _DriveSeed) -> None:
    """Delte mapper: one per moderator login, a member's view and an outsider's, one near its expiry,
    one archived, and every kind of member change in the activity."""
    from drive.access import Level
    from drive.models import ApprovalRequest, Event, Folder, SharedFolder

    tops = {f.name: f for f in Folder.objects.filter(kind=Folder.Kind.SHARED)}
    theme, trip, harvest = tops["Temafest"], tops["Studietur"], tops["Høstfest"]
    assert seeded.viewer("beboer@gahk.dk").moderates(theme.shared_id)
    assert seeded.viewer("formand@gahk.dk", oversees=True).level(trip) >= Level.READ
    # admin@ is in none of them: the number, size and count only.
    admin = seeded.viewer("admin@gahk.dk", oversees=True)
    assert admin.level(theme) == admin.level(trip) == Level.META
    # Archived: readable by everyone, under Delte mapper/<year>.
    assert harvest.shared.archived_at is not None
    assert harvest.parent.kind == Folder.Kind.ARCHIVE_YEAR
    assert seeded.viewer("beboer@gahk.dk").level(harvest) == Level.READ
    # Warned about its expiry, and a purge waiting for beboer@ as moderator.
    assert SharedFolder.objects.filter(warned_for__isnull=False, archived_at__isnull=True).exists()
    assert ApprovalRequest.objects.filter(shared=theme.shared, state=ApprovalRequest.State.PENDING).exists()
    lines = set(Event.objects.filter(shared=theme.shared).values_list("action", flat=True))
    assert lines >= {"member_added", "member_removed", "member_left", "role_changed", "expiry_changed"}
    assert Event.objects.filter(action="archived").exists()


def _check_drive_sharing(seeded: _DriveSeed) -> None:
    """`Delt med mig` for the logins, `Delt med <gruppe>` for beboer@'s group, and both kinds of
    edit share used."""
    from drive import sharing
    from drive.access import Level
    from drive.models import Event, File, Share

    for email in ("ak@gahk.dk", "oel@gahk.dk", "regnskab@gahk.dk"):
        assert sharing.shared_with_me(seeded.viewer(email)), email
    assert not Share.objects.filter(resident__email="beboer@gahk.dk").exists()  # see drive.demo
    beboer = seeded.viewer("beboer@gahk.dk")
    for_group = Share.objects.filter(workgroup__isnull=False)
    assert for_group.filter(file__folder__period_start__isnull=True).exists()
    assert for_group.filter(file__folder__period_start__isnull=False).exists()
    shared_files = [row.file for row in for_group if row.file is not None]
    assert any(beboer.file_level(file) >= Level.READ for file in shared_files)
    # Edit shares used: a version added to a file, and a file added in a folder, by the recipients.
    plan = File.objects.get(name="Arbejdsplan.csv")
    assert plan.versions.filter(created_by=seeded.by_email["regnskab@gahk.dk"]).exists()
    assert File.objects.filter(
        name="Kvittering Ølkælderen.jpg", created_by=seeded.by_email["oel@gahk.dk"]
    ).exists()
    assert Event.objects.filter(action="unshared").exists()


def _check_drive_standing(seeded: _DriveSeed) -> None:
    """Fællesmøder across two years, written by admins and Inspektionen only, and a group's standing
    folders defined for next period and stopped."""
    from drive import standing
    from drive.models import StandingFolder

    meetings = StandingFolder.objects.get(name="Fællesmøder")
    copies = standing.copies(meetings)
    assert {copy.period_start.year for copy in copies} == {seeded.today.year, seeded.today.year - 1}
    beboer = seeded.viewer("beboer@gahk.dk")
    entries = standing.combined(beboer, meetings)
    assert len({entry.copy.pk for entry in entries}) == 2
    current = next(copy for copy in copies if copy.period_start.year == seeded.today.year)
    assert not beboer.can_create_in(current)
    assert seeded.viewer("formand@gahk.dk", oversees=True).can_create_in(current)
    groups = StandingFolder.objects.filter(space="group")
    assert groups.filter(stopped_at__isnull=False).exists()
    assert groups.filter(stopped_at__isnull=True, starts_on__gt=seeded.today).exists()


def _check_drive_preview(seeded: _DriveSeed) -> None:
    """A file of every kind the preview knows (presentations come with live editing's blank one),
    images with their thumbnails made, one that cannot be shown, and the text cases."""
    from drive import preview
    from drive.models import File, ImagePreview

    kinds = {preview.kind_of(name) for name in File.objects.values_list("name", flat=True)}
    assert kinds >= set(preview.Kind) - {preview.Kind.PRESENTATION}
    previews = ImagePreview.objects.all()
    assert previews.filter(state=ImagePreview.State.READY).count() >= 10
    assert not previews.filter(state=ImagePreview.State.PENDING).exists()
    assert previews.filter(state=ImagePreview.State.FAILED).count() == 1
    # Excel's CSV: semicolons in Windows' encoding, read as a table with its Danish letters.
    till = File.objects.get(name="Kasseopgørelse.csv")
    assert ["", "I alt", "", "631,00"] in preview.text(till.latest_version(), till.name).rows
    assert "Beløb" in preview.text(till.latest_version(), till.name).text
    sales = File.objects.get(name="Salgshistorik.csv")
    assert preview.text(sales.latest_version(), sales.name).more_rows


def _check_drive_search(seeded: _DriveSeed) -> None:
    """Search finds a name in several places, filtered by who may see them, and a folder has enough
    files to sort."""
    from drive import search
    from drive.models import Folder

    def found(email: str, text: str, **filters: str) -> set[str]:
        hits = search.search(seeded.viewer(email, oversees=email == "formand@gahk.dk"), text, **filters).hits
        return {hit.where for hit in hits}

    regnskab = found("regnskab@gahk.dk", "referat")
    assert len(regnskab) >= 3
    assert len(found("regnskab@gahk.dk", "budget")) > len(found("beboer@gahk.dk", "budget"))
    assert found("oel@gahk.dk", "referat", space="group")
    assert Folder.objects.get(name="Indkøb").files.filter(deleted_at__isnull=True).count() >= 12


def _check_drive_office(seeded: _DriveSeed) -> None:
    """Live editing's cases: every office kind to open, a version with its live session's editors,
    and an older format to view and copy."""
    from drive import office, preview
    from drive.models import File, FileVersion

    names = list(File.objects.values_list("name", flat=True))
    assert {preview.kind_of(name) for name in names} >= set(preview.Kind)
    assert {office.document_type(name) for name in names} >= {"word", "cell", "slide"}
    assert any(office.document_type(name) and not office.editable(name) for name in names)
    assert any(version.editors.count() >= 3 for version in FileVersion.objects.filter(editors__isnull=False))


def _check_drive_readme(seeded: _DriveSeed) -> None:
    """READMEs in three cases of the name, one with HTML that must not come through."""
    from drive import preview
    from drive.models import File

    readmes = File.objects.filter(name_key=preview.README, deleted_at__isnull=True)
    assert {file.name for file in readmes} == {"README.md", "Readme.md", "readme.md"}
    for file in readmes:
        shown = preview.readme(file.latest_version())
        assert shown is not None and shown.html
        assert "<span" not in shown.html


def _check_drive_text(seeded: _DriveSeed) -> None:
    """Files made with `Ny fil` and written in the editor, a Windows file edited with its line endings
    kept, and a text too long for the editor."""
    from drive import services, text
    from drive.models import File

    def stored(file: File) -> bytes:
        with services.storage().open(file.latest_version().key, "rb") as source:
            return source.read()

    note, table = File.objects.get(name="Huskeliste.md"), File.objects.get(name="Kassebeholdning.csv")
    for made in (note, table):
        assert made.versions.get(number=1).size == 0
        assert made.latest_version().number == 2
    assert stored(table).startswith(b"\xef\xbb\xbf")
    keys = File.objects.get(name="Nøgleliste.txt")
    assert keys.versions.count() == 2 and b"\r\n" in stored(keys)
    assert not text.editable(File.objects.get(name="Varmeanlæg fuld log.log"))
