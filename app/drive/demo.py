"""Demo Drive for `manage.py seed_demo`: folders and real files in every space, for every login.

What each documented login (password demo1234) finds:

  * admin@ and formand@ oversee (administrator holds Inspektionen's role): every group's
    `Uden periode` and earlier periods to read, and the name, size and file count of the current
    period of each group they are not in this month, without its contents. Only they could make
    Fælles' first-level folders (`Vejledninger`, `Skabeloner`), so formand@ made them.
  * regnskab@, ak@ and oel@ write their own group's `Uden periode` and current period, and the
    previous period they were in; they read the period before that. Other groups they see only
    when the residency seed put them there in one of its months.
  * beboer@ has a note in this month's group (whichever the residency seed put them in) and sees
    only the groups they have been in over the seeded months, not the others.
  * Everyone living here reads all of Fælles and writes in `Uden årstal` below its first level, this
    year and last year. The year before last is there to be read-only.

Then what happens to files after they are uploaded (`_seed_changes`): a colleague's new version of
regnskab@'s budget after regnskab@ downloaded it (so replacing it warns), an old version restored, a
copy, a move, a rename that tells ak@, and the bin - a receipt a colleague deleted (regnskab@ is
told), a file three days from going for good, and beboer@'s deleted folder kept for the file in it.

Delte mapper (`_seed_shared`): one moderated by beboer@ with every kind of member change, one by
oel@ and regnskab@ about to expire, and an expired one archived for everyone to read.

Shares (`_seed_sharing`): `Delt med mig` for ak@ (read), oel@ (edit on a folder partway down,
used) and regnskab@ (edit on a file, a version added), and `Delt med <gruppe>` in beboer@'s group's
current period and `Uden periode`.

Standing folders (`_seed_standing`): `Fællesmøder` with meetings in this year and last, written only
by Inspektionen and admins, and in groups one just defined and one stopped. Opening any `Uden
periode` or `Uden årstal` a login can write in asks the current-period question.

Previews (`_seed_previews`): a file of every kind the preview knows, from an animated GIF and an
iPhone's HEIC to a video, a sound, an Excel CSV and one too long to show whole, plus a scan too
broken to preview. The viewer copies and thumbnails are built at the end, as a worker would.

Search and sorting (`_seed_search`): a folder of a dozen receipts by two people, and minutes of the
same name in several periods and groups.

Last, locks (`_seed_locks`), as they stop changes: what the nightly task locked, regnskab@'s
closed accounts and `Årsafslutning` folder locked by hand, and the requests page for admin@ and
formand@ with an unlock and a purge in each state - pending, approved (the logo is unlocked for now
and changed meanwhile), and rejected.

The system only makes the current period and year (drive.structure: no back-filling), so the
earlier periods and years here are made by hand, as they would have been made in their time.

Files go through drive.services as an upload would, each step run at the time it happened (`_at`),
so sizes, versions and "last changed by" agree, and the bytes are real: an image opens, a PDF
renders and a spreadsheet opens in Excel.
"""

import csv
import mimetypes
import random
import subprocess  # nosec B404 - ffmpeg for the demo's clip and sound
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from core.models import Workgroup
from notifications.models import Notification
from residents.models import Residency, Resident, Role
from residents.permissions import has_active_role

from . import locks, operations, preview, services, shared, sharing, standing
from .access import Viewer
from .models import (
    ApprovalRequest,
    Event,
    File,
    FileVersion,
    Folder,
    ImagePreview,
    SharedFolder,
    SharedMember,
    Space,
    StandingFolder,
    ZipDownload,
)
from .periods import Period, period_containing
from .structure import COMMON_TIMELESS, _standing, ensure_structure, folder_name

LOGINS = (
    "admin@gahk.dk",
    "formand@gahk.dk",
    "ak@gahk.dk",
    "oel@gahk.dk",
    "regnskab@gahk.dk",
    "beboer@gahk.dk",
)


@contextmanager
def _at(when: datetime) -> Iterator[None]:
    """Run a step as if it were `when`. Drive stamps its rows with timezone.now() in many places
    (created_at, changed_at, the version's time), so this keeps them all in agreement rather than
    rewriting each column afterwards. Viewers are built outside it: their period is today's."""
    with mock.patch("django.utils.timezone.now", return_value=when):
        yield


class _Seeder:
    def __init__(self, residents: list[Resident], now: datetime) -> None:
        self.now = now
        self.today = timezone.localdate(now)
        self.by_email = {r.email: r for r in residents}

    def login(self, email: str) -> Resident:
        return self.by_email[email]

    def viewer(self, resident: Resident, when: datetime) -> Viewer:
        """What the resident could do on the day: an earlier period was the current one then."""
        oversees = has_active_role(resident, Role.INSPEKTION)
        # Built afresh each time: a Viewer keeps the locks it has looked up.
        return Viewer.build(resident.pk, oversees=oversees, today=timezone.localdate(when))

    def ago(self, days: int, *, not_before: date | None = None) -> datetime:
        """`days` before now, in working hours, but not before the folder's period began (early in a
        new period) nor after now (late at night)."""
        day = timezone.localdate(self.now - timedelta(days=days))
        when = min(_noon(day) + timedelta(hours=days * 5 % 9 - 3), self.now)
        return max(when, _noon(not_before)) if not_before is not None else when

    # --- folders ---------------------------------------------------------------------------------

    def folder(self, parent: Folder, name: str, by: Resident, when: datetime) -> Folder:
        viewer = self.viewer(by, when)
        with _at(when):
            return services.create_folder(viewer, parent, name, by)

    def path(self, parent: Folder, names: str, by: Resident, when: datetime) -> Folder:
        for name in names.split("/"):
            parent = self.folder(parent, name, by, when)
        return parent

    # --- files -----------------------------------------------------------------------------------

    def upload(self, folder: Folder, name: str, data: bytes, by: Resident, when: datetime) -> File:
        """Store the bytes and add the file, or a new version of it when the name is there."""
        key = services.new_key()
        services.store_local(key, ContentFile(data))
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        viewer = self.viewer(by, when)
        with _at(when):
            added = services.add_upload(
                viewer,
                folder,
                name=name,
                key=key,
                size=len(data),
                content_type=content_type,
                on_conflict=services.REPLACE,
                resident=by,
            )
        return added.file

    # --- changes ---------------------------------------------------------------------------------

    def rename(self, file: File, name: str, by: Resident, when: datetime) -> None:
        viewer = self.viewer(by, when)
        with _at(when):
            operations.rename_file(viewer, file, name, by)

    def move(self, file: File, destination: Folder, by: Resident, when: datetime) -> None:
        viewer = self.viewer(by, when)
        with _at(when):
            operations.move(viewer, operations.select(file.folder, [file.uuid], []), destination, by)

    def copy(self, file: File, destination: Folder, by: Resident, when: datetime) -> None:
        viewer = self.viewer(by, when)
        with _at(when):
            operations.copy(viewer, operations.select(file.folder, [file.uuid], []), destination, by)

    def delete(self, item: File | Folder, by: Resident, when: datetime) -> None:
        viewer = self.viewer(by, when)
        source = item.folder if isinstance(item, File) else item.parent
        assert source is not None  # noqa: S101  # nosec B101 - the demo deletes no root
        files, folders = ([item.uuid], []) if isinstance(item, File) else ([], [item.uuid])
        with _at(when):
            operations.delete(viewer, operations.select(source, files, folders), by)
        item.refresh_from_db()

    def restore(self, file: File, by: Resident, when: datetime) -> None:
        viewer = self.viewer(by, when)
        with _at(when):
            operations.restore(viewer, file, by)

    def restore_version(self, file: File, number: int, by: Resident, when: datetime) -> None:
        viewer = self.viewer(by, when)
        with _at(when):
            operations.restore_version(viewer, file, number, by)

    def download(self, file: File, by: Resident, when: datetime) -> None:
        """What the download view remembers, for the warning when somebody else changed it since."""
        with _at(when):
            services.remember_download(file, by.pk, file.latest_version().number)

    # --- locks and requests ------------------------------------------------------------------------

    def lock(self, item: File | Folder, by: Resident, when: datetime) -> None:
        viewer = self.viewer(by, when)
        with _at(when):
            if isinstance(item, File):
                locks.lock_file(viewer, item, by)
            else:
                locks.lock_folder(viewer, item, by)

    def ask_unlock(
        self, item: File | Folder, days: int, reason: str, by: Resident, when: datetime
    ) -> ApprovalRequest:
        viewer = self.viewer(by, when)
        with _at(when):
            return locks.request_unlock(viewer, item, days, reason, by)

    def ask_purge(
        self, file: File, number: int | None, reason: str, by: Resident, when: datetime
    ) -> ApprovalRequest:
        viewer = self.viewer(by, when)
        with _at(when):
            return locks.request_purge(viewer, file, number, reason, by)

    def decide(
        self, request: ApprovalRequest, *, approve: bool, reason: str, by: Resident, when: datetime
    ) -> None:
        viewer = self.viewer(by, when)
        with _at(when):
            locks.decide(viewer, request, approve=approve, reason=reason, actor=by)

    # --- Delte mapper -------------------------------------------------------------------------------

    def share_folder(
        self,
        name: str,
        by: Resident,
        members: list[Resident],
        expires_on: date,
        on_expiry: str,
        when: datetime,
    ) -> Folder:
        viewer = self.viewer(by, when)
        with _at(when):
            return shared.create(
                viewer,
                name=name,
                members=[m.pk for m in members],
                expires_on=expires_on,
                on_expiry=on_expiry,
                actor=by,
            )

    def member(
        self, folder: Folder, change: str, who: Resident, by: Resident, when: datetime, role: str = ""
    ) -> None:
        """`add`, `role` or `remove` a member; `remove` with `who` == `by` is leaving."""
        viewer = self.viewer(by, when)
        assert folder.shared is not None  # noqa: S101  # nosec B101 - a Delt mappe's top folder
        with _at(when):
            if change == "add":
                shared.add_member(viewer, folder.shared, who.pk, role, by)
            elif change == "role":
                shared.set_role(viewer, folder.shared, who.pk, role, by)
            else:
                shared.remove_member(viewer, folder.shared, who.pk, by)

    def expiry(self, folder: Folder, expires_on: date, on_expiry: str, by: Resident, when: datetime) -> None:
        viewer = self.viewer(by, when)
        assert folder.shared is not None  # noqa: S101  # nosec B101 - a Delt mappe's top folder
        with _at(when):
            shared.set_expiry(viewer, folder.shared, expires_on, on_expiry, by)

    # --- sharing ------------------------------------------------------------------------------------

    def share(
        self,
        item: File | Folder,
        by: Resident,
        when: datetime,
        *,
        edit: bool = False,
        resident: Resident | None = None,
        group: Workgroup | None = None,
    ) -> None:
        viewer = self.viewer(by, when)
        with _at(when):
            sharing.share(
                viewer,
                file=item if isinstance(item, File) else None,
                folder=item if isinstance(item, Folder) else None,
                resident_id=resident.pk if resident else None,
                workgroup_id=group.pk if group else None,
                access=sharing.EDIT if edit else sharing.READ,
                actor=by,
            )

    def unshare(self, item: File, recipient: Resident, by: Resident, when: datetime) -> None:
        viewer = self.viewer(by, when)
        with _at(when):
            sharing.stop(viewer, item.shares.get(resident=recipient), by)

    def add_version(self, file: File, data: bytes, by: Resident, when: datetime) -> None:
        """From the file's own page, as one it is shared with to edit."""
        key = services.new_key()
        services.store_local(key, ContentFile(data))
        viewer = self.viewer(by, when)
        with _at(when):
            services.add_version(
                viewer, file, key=key, size=len(data), content_type=file.content_type, resident=by
            )


def _noon(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(12)))


def _group_of(resident: Resident, today: date) -> Workgroup | None:
    residency = (
        Residency.objects.filter(resident=resident, year=today.year, month=today.month)
        .select_related("workgroup")
        .first()
    )
    return residency.workgroup if residency else None


def _colleague(workgroup: Workgroup, login: Resident, today: date) -> Resident:
    """Another of this month's members, not one of the logins, so a login sees somebody else's
    changes to their files; the login themselves when they are the group's only member."""
    others = (
        Resident.objects.filter(
            residencies__year=today.year, residencies__month=today.month, residencies__workgroup=workgroup
        )
        .exclude(email__in=LOGINS)
        .order_by("pk")
    )
    return others.first() or login


def _in(folder: Folder, name: str) -> File:
    return File.objects.get(folder=folder, name=name, deleted_at__isnull=True)


def _group_folder(workgroup: Workgroup, kind: str, period: Period | None = None) -> Folder:
    """A group's timeless or period folder; an earlier period is made as the system would have."""
    fields = {"period_start": period.start} if period else {}
    found = Folder.objects.filter(workgroup=workgroup, kind=kind, **fields).first()
    if found is not None:
        return found
    group = Folder.objects.get(workgroup=workgroup, kind=Folder.Kind.GROUP)
    assert period is not None  # noqa: S101  # nosec B101 - the timeless folder is always made
    folder = Folder(
        parent=group,
        name=period.name,
        kind=Folder.Kind.PERIOD,
        space=Space.GROUP,
        workgroup=workgroup,
        period_start=period.start,
    )
    folder.save()
    return folder


def _year_folder(year: int) -> Folder:
    found = Folder.objects.filter(kind=Folder.Kind.YEAR, period_start=date(year, 1, 1)).first()
    if found is not None:
        return found
    folder = Folder(
        parent=Folder.objects.get(kind=Folder.Kind.COMMON_ROOT),
        name=str(year),
        kind=Folder.Kind.YEAR,
        space=Space.COMMON,
        period_start=date(year, 1, 1),
    )
    folder.save()
    return folder


def seed(residents: list[Resident], now: datetime, _rng: random.Random) -> int:
    """Returns the number of files made. Wipe first (`wipe`): names are unique within a folder, and a
    second run would add versions instead."""
    if not all(email in {r.email for r in residents} for email in LOGINS):
        return 0
    today = timezone.localdate(now)
    ensure_structure(today)
    s = _Seeder(residents, now)
    _seed_common(s)
    _seed_groups(s)
    _seed_changes(s)
    _seed_shared(s)
    _seed_sharing(s)
    _seed_standing(s)
    _seed_previews(s)
    _seed_search(s)
    # Last: a lock stops the changes above.
    _seed_locks(s)
    # The viewer copies and thumbnails the "thumbnails" queue makes, made here so the lists show them
    # without a worker running. The queued builds find them done.
    # A failed build is tried again (the nightly requeue) until it gives up, as the broken scan does.
    for key in ImagePreview.objects.filter(state=ImagePreview.State.PENDING).values_list("key", flat=True):
        for _ in range(preview.MAX_ATTEMPTS):
            if (
                preview.build(key)
                or not ImagePreview.objects.filter(key=key, state=ImagePreview.State.PENDING).exists()
            ):
                break
    # What the nightly purge (notifications.services.purge_expired) would have removed by now: the
    # notifications of the steps longest ago, such as the invitation to a Delt mappe in September.
    Notification.objects.filter(
        url__startswith=reverse("drive:home"), expires_at__lt=now - timedelta(days=30)
    ).delete()
    return File.objects.count()


def _seed_common(s: _Seeder) -> None:
    formand, beboer = s.login("formand@gahk.dk"), s.login("beboer@gahk.dk")
    ak, oel = s.login("ak@gahk.dk"), s.login("oel@gahk.dk")
    timeless = Folder.objects.get(kind=Folder.Kind.TIMELESS, space=Space.COMMON, name=COMMON_TIMELESS)

    # Only admins and Inspektionen make the first level of `Uden årstal`; anyone may fill it.
    guides = s.folder(timeless, "Vejledninger", formand, s.ago(200))
    s.upload(guides, "Husorden.md", _HOUSE_RULES.encode(), formand, s.ago(200))
    s.upload(guides, "Telefonliste.csv", _csv(_PHONE_LIST), formand, s.ago(150))
    s.upload(guides, "Affaldssortering.png", _png("Affaldssortering", "#517544", (1200, 800)), oel, s.ago(90))
    s.upload(guides, "Vaskeriet.pdf", _pdf("Vaskeriet", _LAUNDRY), ak, s.ago(60))
    templates = s.folder(timeless, "Skabeloner", formand, s.ago(190))
    s.upload(templates, "Referatskabelon.docx", _docx("Referat", _MINUTES_TEMPLATE), formand, s.ago(190))
    s.upload(templates, "Budgetskabelon.xlsx", _xlsx(_BUDGET_TEMPLATE), formand, s.ago(185))
    # A plain resident's folder one level down, which they may make.
    expenses = s.folder(templates, "Udlæg", beboer, s.ago(40))
    s.upload(expenses, "Udlægsseddel.pdf", _pdf("Udlægsseddel", _EXPENSE_FORM), beboer, s.ago(40))

    this_year = _year_folder(s.today.year)
    start = date(s.today.year, 1, 1)
    party = s.folder(this_year, "Julefrokost", beboer, s.ago(20, not_before=start))
    s.upload(party, "Indkøbsliste.csv", _csv(_SHOPPING), beboer, s.ago(20, not_before=start))
    s.upload(
        party, "Plakat.png", _png("Julefrokost", "#8a4e70", (900, 1270)), oel, s.ago(12, not_before=start)
    )
    s.upload(
        this_year,
        "Husmøde referat.docx",
        _docx("Husmøde", _HOUSE_MEETING),
        formand,
        s.ago(30, not_before=start),
    )

    # Last year is still writable; the year before is read-only to every resident.
    last_year = s.today.year - 1
    s.upload(
        _year_folder(last_year),
        f"Generalforsamling {last_year}.pdf",
        _pdf(f"Generalforsamling {last_year}", _GENERAL_MEETING),
        formand,
        _noon(date(last_year, 4, 20)),
    )
    s.upload(
        _year_folder(last_year - 1),
        f"Årsberetning {last_year - 1}.pdf",
        _pdf(f"Årsberetning {last_year - 1}", _ANNUAL_REPORT),
        formand,
        _noon(date(last_year - 1, 12, 1)),
    )


def _seed_groups(s: _Seeder) -> None:
    current = period_containing(s.today)
    previous, older = current.previous(), current.previous().previous()
    starts = current.start

    regnskab = s.login("regnskab@gahk.dk")
    if (group := _group_of(regnskab, s.today)) is not None:
        timeless = _group_folder(group, Folder.Kind.TIMELESS)
        s.upload(timeless, "Kontoplan.xlsx", _xlsx(_ACCOUNTS), regnskab, s.ago(300))
        s.upload(timeless, "Sådan bogfører vi.md", _BOOKKEEPING.encode(), regnskab, s.ago(280))
        now_folder = _group_folder(group, Folder.Kind.PERIOD, current)
        s.upload(
            now_folder, f"Budget {current.name}.xlsx", _xlsx(_BUDGET), regnskab, s.ago(25, not_before=starts)
        )
        # Nested, so the breadcrumbs and subtree counts have depth to show.
        receipts = s.path(now_folder, "Bilag/Indkøb", regnskab, s.ago(22, not_before=starts))
        for number, colour in enumerate(["#dd6b43", "#d8a32f", "#315b8d"], start=1):
            s.upload(
                receipts,
                f"Kvittering {number}.jpg",
                _jpg(f"Kvittering {number}", colour, (800, 1100)),
                regnskab,
                s.ago(22 - number * 3, not_before=starts),
            )
        prev_folder = _group_folder(group, Folder.Kind.PERIOD, previous)
        s.upload(
            prev_folder,
            f"Regnskab {previous.name}.pdf",
            _pdf(f"Regnskab {previous.name}", _ACCOUNTS_REPORT),
            regnskab,
            _noon(previous.start + timedelta(days=150)),
        )
        old_folder = _group_folder(group, Folder.Kind.PERIOD, older)
        s.upload(
            old_folder,
            f"Regnskab {older.name}.pdf",
            _pdf(f"Regnskab {older.name}", _ACCOUNTS_REPORT),
            regnskab,
            _noon(older.start + timedelta(days=150)),
        )

    ak = s.login("ak@gahk.dk")
    if (group := _group_of(ak, s.today)) is not None:
        s.upload(
            _group_folder(group, Folder.Kind.TIMELESS), "Værktøjsliste.xlsx", _xlsx(_TOOLS), ak, s.ago(240)
        )
        now_folder = _group_folder(group, Folder.Kind.PERIOD, current)
        s.upload(now_folder, "Arbejdsplan.csv", _csv(_WORK_PLAN), ak, s.ago(18, not_before=starts))
        s.upload(
            now_folder, "AK-møde referat.docx", _docx("AK-møde", _AK_MEETING), ak, s.ago(9, not_before=starts)
        )

    oel = s.login("oel@gahk.dk")
    if (group := _group_of(oel, s.today)) is not None:
        s.upload(
            _group_folder(group, Folder.Kind.TIMELESS),
            "Logo.png",
            _png("Ølkælderen", "#d8a32f", (600, 600)),
            oel,
            s.ago(400),
        )
        now_folder = _group_folder(group, Folder.Kind.PERIOD, current)
        s.upload(now_folder, "Prisliste.csv", _csv(_PRICES), oel, s.ago(15, not_before=starts))
        s.upload(now_folder, "Indkøb.xlsx", _xlsx(_PURCHASES), oel, s.ago(5, not_before=starts))
        s.upload(
            _group_folder(group, Folder.Kind.PERIOD, previous),
            f"Status {previous.name}.pdf",
            _pdf(f"Status {previous.name}", _CELLAR_STATUS),
            oel,
            _noon(previous.start + timedelta(days=170)),
        )

    beboer = s.login("beboer@gahk.dk")
    if (group := _group_of(beboer, s.today)) is not None:
        s.upload(
            _group_folder(group, Folder.Kind.PERIOD, current),
            f"Noter fra {folder_name(group)}.txt",
            _NOTES.encode(),
            beboer,
            s.ago(3, not_before=starts),
        )


def _seed_changes(s: _Seeder) -> None:
    """Versions, the bin and the history: renamed, moved, copied, deleted, restored. Each case a
    login would otherwise have to make by hand, at a time that makes sense, and by somebody else
    where the point is the creator's notification or the stale-download warning."""
    current = period_containing(s.today)
    starts = current.start
    regnskab = s.login("regnskab@gahk.dk")
    if (group := _group_of(regnskab, s.today)) is not None:
        colleague = _colleague(group, regnskab, s.today)
        now_folder = _group_folder(group, Folder.Kind.PERIOD, current)
        timeless = _group_folder(group, Folder.Kind.TIMELESS)
        # Downloaded by regnskab@, then changed by a colleague: replacing it now warns who changed it
        # since (services.check_stale). Version 1 and 2 to compare on the file's page.
        budget = _in(now_folder, f"Budget {current.name}.xlsx")
        s.download(budget, regnskab, s.ago(20, not_before=starts))
        s.upload(now_folder, budget.name, _xlsx(_BUDGET_REVISED), colleague, s.ago(8, not_before=starts))
        # Three versions, the last a restored first one: "Gendannede version 1 ... som version 3".
        accounts = _in(timeless, "Kontoplan.xlsx")
        s.upload(
            timeless, accounts.name, _xlsx([*_ACCOUNTS, [2300, "Diverse", "Udgift"]]), colleague, s.ago(100)
        )
        s.restore_version(accounts, 1, regnskab, s.ago(90))
        # A copy from Fælles: a new file with its own creator and no history but "Kopierede".
        template = File.objects.get(name="Budgetskabelon.xlsx", folder__space=Space.COMMON)
        s.copy(template, now_folder, regnskab, s.ago(26, not_before=starts))
        # Deleted by the colleague: in the bin, and regnskab@ is told, with a link to restore it.
        receipts = Folder.objects.get(parent__parent=now_folder, name="Indkøb")
        s.delete(_in(receipts, "Kvittering 3.jpg"), colleague, s.ago(2, not_before=starts))

    oel = s.login("oel@gahk.dk")
    if (group := _group_of(oel, s.today)) is not None:
        now_folder = _group_folder(group, Folder.Kind.PERIOD, current)
        # Binned 27 days ago: the bin says it goes for good in 3 days.
        old = s.upload(
            now_folder, "Gammel prisliste.csv", _csv(_PRICES[:3]), oel, s.ago(40, not_before=starts)
        )
        s.delete(old, oel, s.ago(27, not_before=starts))
        # Moved within the group: "Flyttede ... fra ... / 2026-27 Vinter til ... / Uden periode".
        suppliers = s.upload(
            now_folder, "Leverandører.xlsx", _xlsx(_SUPPLIERS), oel, s.ago(30, not_before=starts)
        )
        s.move(suppliers, _group_folder(group, Folder.Kind.TIMELESS), oel, s.ago(29, not_before=starts))

        # Deleted by mistake and restored from the bin straight away.
        prices = _in(now_folder, "Prisliste.csv")
        s.delete(prices, oel, s.ago(14, not_before=starts))
        s.restore(prices, oel, s.ago(14, not_before=starts) + timedelta(minutes=3))

    # Renamed by somebody else than its creator, so ak@ is told.
    guide = File.objects.get(name="Vaskeriet.pdf")
    s.rename(guide, "Vaskeriet – åbningstider.pdf", oel, s.ago(10))

    # A deleted folder kept for the binned file in it: restoring the file brings the folder back.
    beboer = s.login("beboer@gahk.dk")
    year_start = date(s.today.year, 1, 1)
    party = s.folder(_year_folder(s.today.year), "Sommerfest", beboer, s.ago(70, not_before=year_start))
    program = s.upload(
        party, "Program.pdf", _pdf("Sommerfest", _PARTY_PROGRAMME), beboer, s.ago(70, not_before=year_start)
    )
    s.delete(program, beboer, s.ago(5, not_before=year_start))
    s.delete(party, beboer, s.ago(5, not_before=year_start))


def _seed_shared(s: _Seeder) -> None:
    """Delte mapper. Only members see one: beboer@ moderates `Temafest`, oel@ `Studietur` (with
    regnskab@, and formand@ as a member), and admin@, in neither, sees each as `Delt mappe #000N`
    with its size and file count. `Høstfest` has expired and been archived to `Delte mapper/<year>`,
    readable by everyone under its name."""
    beboer, oel, ak = s.login("beboer@gahk.dk"), s.login("oel@gahk.dk"), s.login("ak@gahk.dk")
    regnskab, formand = s.login("regnskab@gahk.dk"), s.login("formand@gahk.dk")
    # The first of the earliest month seed_demo gives residencies (this one and the two before).
    first_month = s.today.replace(day=1)
    for _ in range(2):
        first_month = (first_month - timedelta(days=1)).replace(day=1)
    # Residents who lived here then and now, as a Delt mappe only takes those living here.
    others = list(
        Resident.objects.exclude(email__in=LOGINS)
        .filter(residencies__year=first_month.year, residencies__month=first_month.month)
        .filter(residencies__year=s.today.year, residencies__month=s.today.month)
        .order_by("pk")[:4]
    )
    if len(others) < 4:  # seed_demo --residents with hardly anyone beyond the logins
        return
    archive, delete = SharedFolder.OnExpiry.ARCHIVE, SharedFolder.OnExpiry.DELETE

    # Made early in the seeded months, expired ten days ago and archived by the daily task.
    first_month += timedelta(days=2)
    expired = s.today - timedelta(days=10)
    harvest = s.share_folder(
        "Høstfest", ak, [oel, others[0], others[1]], expired, archive, _noon(first_month)
    )
    s.upload(harvest, "Indkøb.xlsx", _xlsx(_HARVEST), ak, _noon(first_month + timedelta(days=4)))
    s.upload(
        harvest,
        "Høstfest.jpg",
        _jpg("Høstfest", "#b0702a", (1600, 1067)),
        oel,
        _noon(first_month + timedelta(days=20)),
    )
    with _at(_noon(expired + timedelta(days=1))):
        shared.run_expiry(expired + timedelta(days=1))

    # Every kind of member change in the activity: invited, made read-only, removed, left, added late,
    # and the expiry moved.
    theme = s.share_folder(
        "Temafest",
        beboer,
        [oel, ak, *others[:3]],
        shared.default_expiry(s.today - timedelta(days=45)),
        archive,
        s.ago(45),
    )
    s.member(theme, "role", others[2], beboer, s.ago(44), role=SharedMember.Role.READ)
    s.upload(theme, "Plan.docx", _docx("Temafest", _THEME_PLAN), beboer, s.ago(44))
    s.upload(theme, "Gammel plan.docx", _docx("Temafest (udkast)", _THEME_PLAN[:1]), oel, s.ago(43))
    s.member(theme, "remove", others[1], beboer, s.ago(30))
    s.upload(theme, "Playliste.txt", _PLAYLIST.encode(), oel, s.ago(30))
    decorations = s.folder(theme, "Pynt", ak, s.ago(25))
    s.upload(decorations, "Inspiration.jpg", _jpg("Inspiration", "#315b8d", (1200, 800)), ak, s.ago(25))
    s.member(theme, "remove", others[0], others[0], s.ago(20))
    s.member(theme, "add", regnskab, beboer, s.ago(15), role=SharedMember.Role.EDIT)
    s.expiry(theme, s.today + timedelta(days=200), archive, beboer, s.ago(10))
    # A purge in a Delt mappe is its moderators' to decide: beboer@ has one waiting.
    draft = _in(theme, "Gammel plan.docx")
    s.delete(draft, oel, s.ago(35))
    s.ask_purge(draft, None, "Udkastet skal ikke kunne findes igen.", oel, s.ago(9))

    # Two moderators, and an expiry close enough that the daily task has warned them.
    trip = s.share_folder(
        "Studietur", oel, [regnskab, formand, others[3]], s.today + timedelta(days=10), delete, s.ago(60)
    )
    s.member(trip, "role", regnskab, oel, s.ago(59), role=SharedMember.Role.MODERATOR)
    s.upload(trip, "Program.pdf", _pdf("Studietur", _TRIP), oel, s.ago(58))
    s.upload(trip, "Budget.xlsx", _xlsx(_TRIP_BUDGET), regnskab, s.ago(50))
    with _at(s.ago(0)):
        shared.run_expiry(s.today)


def _seed_sharing(s: _Seeder) -> None:
    """Shares, so `Delt med mig` has something for ak@, oel@ and regnskab@, and beboer@'s group has
    `Delt med <gruppe>` in both its current period and `Uden periode`. beboer@ gets nothing shared in
    person: a share with a resident notifies them, and notifications.demo sets beboer@'s bell."""
    current = period_containing(s.today)
    starts = current.start
    regnskab, ak, oel = s.login("regnskab@gahk.dk"), s.login("ak@gahk.dk"), s.login("oel@gahk.dk")
    regnskab_group, ak_group, oel_group = (_group_of(login, s.today) for login in (regnskab, ak, oel))
    if regnskab_group is None or ak_group is None or oel_group is None:
        return
    beboer_group = _group_of(s.login("beboer@gahk.dk"), s.today)

    def target(sharer: Workgroup) -> Workgroup:
        """beboer@'s group, or another one when it is the sharer's own."""
        if beboer_group is not None and beboer_group != sharer:
            return beboer_group
        other = Workgroup.objects.exclude(pk=sharer.pk).order_by("name").first()
        assert other is not None  # noqa: S101  # nosec B101 - seed_demo makes many groups
        return other

    regnskab_now = _group_folder(regnskab_group, Folder.Kind.PERIOD, current)
    # Read: ak@ finds the budget under `Delt med mig`.
    s.share(
        _in(regnskab_now, f"Budget {current.name}.xlsx"),
        regnskab,
        s.ago(6, not_before=starts),
        resident=ak,
    )
    # Edit, on a folder partway down: oel@ adds Ølkælderen's receipts, and sees the folders above
    # `Bilag` as names only.
    receipts = Folder.objects.get(parent=regnskab_now, name="Bilag")
    s.share(receipts, regnskab, s.ago(12, not_before=starts), edit=True, resident=oel)
    s.upload(
        receipts.children.get(name="Indkøb"),
        "Kvittering Ølkælderen.jpg",
        _jpg("Kvittering Ølkælderen", "#517544", (800, 1100)),
        oel,
        s.ago(4, not_before=starts),
    )
    # Shared and then stopped, for the history's "Stoppede delingen".
    accounts = _in(_group_folder(regnskab_group, Folder.Kind.TIMELESS), "Kontoplan.xlsx")
    s.share(accounts, regnskab, s.ago(60), resident=oel)
    s.unshare(accounts, oel, regnskab, s.ago(50))

    # Edit, on a file: regnskab@ adds a version from the file's page.
    ak_now = _group_folder(ak_group, Folder.Kind.PERIOD, current)
    plan = _in(ak_now, "Arbejdsplan.csv")
    s.share(plan, ak, s.ago(7, not_before=starts), edit=True, resident=regnskab)
    s.add_version(
        plan, _csv([*_WORK_PLAN, ["49", "Opsæt julebelysning", "2"]]), regnskab, s.ago(5, not_before=starts)
    )

    # With a group: from a current period it lands in the recipient's current period, from
    # `Uden periode` in theirs.
    oel_now = _group_folder(oel_group, Folder.Kind.PERIOD, current)
    s.share(
        _in(oel_now, "Prisliste.csv"),
        oel,
        s.ago(11, not_before=starts),
        group=target(oel_group),
    )
    tools = _in(_group_folder(ak_group, Folder.Kind.TIMELESS), "Værktøjsliste.xlsx")
    s.share(tools, ak, s.ago(100), group=target(ak_group))


def _seed_standing(s: _Seeder) -> None:
    """Standing folders. `Fællesmøder` in Fælles, written only by Inspektionen and admins, with a
    meeting folder per date in this year's and last year's copy, so `Fællesmøder (alle år)` spans two
    years and beboer@ can read but not add. In groups: one AK has just defined (it starts next
    period) and one Regnskabsgruppen defined and stopped."""
    formand = s.login("formand@gahk.dk")
    this_year, last_year = s.today.year, s.today.year - 1
    inspection = Workgroup.objects.filter(name="Inspektionen").first()
    # Defined before last year began, so it applies from then: a definition today would apply from
    # next year only (standing.next_start). Made as the row it would have been, and its copies as
    # the system makes them - last year's by hand, as that year's folder is (`_year_folder`).
    with _at(_noon(date(last_year - 1, 12, 10))):
        meetings = StandingFolder.objects.create(
            space=Space.COMMON,
            name="Fællesmøder",
            item_label="Fællesmøde",
            limited=True,
            starts_on=date(last_year, 1, 1),
            created_by=formand,
        )
    if inspection is not None:
        meetings.writer_groups.set([inspection])
    _standing(_year_folder(last_year), [meetings])
    ensure_structure(s.today)
    latest = s.today - timedelta(days=18)
    for day in [date(last_year, 3, 12), date(last_year, 10, 8), date(this_year, 3, 5), latest]:
        if day > latest:
            continue  # early in the year: the March meeting has not been held yet
        # Made a week ahead with the agenda; the minutes follow two days after.
        before, after = _noon(day - timedelta(days=7)), _noon(day + timedelta(days=2))
        viewer = s.viewer(formand, before)
        with _at(before):
            meeting = standing.create_item(viewer, meetings, day, formand)
        title = f"{day:%-d/%-m %Y}"
        s.upload(meeting, "Dagsorden.pdf", _pdf(f"Dagsorden {title}", _MEETING_AGENDA), formand, before)
        if after < s.now:
            s.upload(meeting, "Referat.docx", _docx(f"Referat {title}", _MEETING_MINUTES), formand, after)

    ak, regnskab = s.login("ak@gahk.dk"), s.login("regnskab@gahk.dk")
    if (group := _group_of(ak, s.today)) is not None:
        root = Folder.objects.get(kind=Folder.Kind.GROUP, workgroup=group)
        viewer = s.viewer(ak, s.ago(3))
        with _at(s.ago(3)):
            standing.define(
                viewer,
                root,
                name="Erfaringsrapporter",
                item_label="",
                limited=False,
                writer_groups=[],
                actor=ak,
            )
    if (group := _group_of(regnskab, s.today)) is not None:
        root = Folder.objects.get(kind=Folder.Kind.GROUP, workgroup=group)
        viewer = s.viewer(regnskab, s.ago(20))
        with _at(s.ago(20)):
            item = standing.define(
                viewer,
                root,
                name="Kvartalsrapporter",
                item_label="Kvartal",
                limited=False,
                writer_groups=[],
                actor=regnskab,
            )
        viewer = s.viewer(regnskab, s.ago(10))
        with _at(s.ago(10)):
            standing.stop(viewer, item, root, regnskab)


def _seed_previews(s: _Seeder) -> None:
    """A file of every kind the preview knows, where it would be: images (an animated GIF, a WebP, an
    iPhone's HEIC, and a scan too broken to show, which falls back to its icon and a download), a
    video and a sound, text, a log and JSON, an Excel CSV in Windows' encoding with semicolons, a CSV
    longer than the 2,000 rows shown, a ZIP and a drawing nothing opens. With the PDFs, office files,
    CSV and markdown above, every icon and every viewer has a file."""
    current = period_containing(s.today)
    starts = current.start
    beboer, oel, ak = s.login("beboer@gahk.dk"), s.login("oel@gahk.dk"), s.login("ak@gahk.dk")
    regnskab, formand = s.login("regnskab@gahk.dk"), s.login("formand@gahk.dk")
    year_start = date(s.today.year, 1, 1)
    clip, sound = _media()

    party = Folder.objects.get(parent=_year_folder(s.today.year), name="Julefrokost", deleted_at__isnull=True)
    s.upload(party, "Stemning.gif", _gif("Julefrokost"), beboer, s.ago(11, not_before=year_start))
    s.upload(party, "Sidste år.mp4", clip, oel, s.ago(10, not_before=year_start))
    s.upload(party, "Fællessang.mp3", sound, oel, s.ago(10, not_before=year_start))
    guides = Folder.objects.get(name="Vejledninger", kind=Folder.Kind.FOLDER)
    s.upload(
        guides,
        "Kort over kollegiet.webp",
        _image("Kort over kollegiet", "#1f6c64", (1400, 900), "WEBP"),
        formand,
        s.ago(70),
    )

    if (group := _group_of(ak, s.today)) is not None:
        now_folder = _group_folder(group, Folder.Kind.PERIOD, current)
        s.upload(
            now_folder, "Taget mod gården.heic", _heic("Taget mod gården"), ak, s.ago(16, not_before=starts)
        )
        s.upload(now_folder, "Varmeanlæg.log", _HEATING_LOG.encode(), ak, s.ago(13, not_before=starts))
        s.upload(now_folder, "Termostater.json", _THERMOSTATS.encode(), ak, s.ago(13, not_before=starts))
        s.upload(
            now_folder, "Plantegning.dwg", b"AC1032" + bytes(range(256)) * 8, ak, s.ago(12, not_before=starts)
        )

    if (group := _group_of(oel, s.today)) is not None:
        now_folder = _group_folder(group, Folder.Kind.PERIOD, current)
        s.upload(now_folder, "Kasseopgørelse.csv", _excel_csv(_TILL), oel, s.ago(6, not_before=starts))
        s.upload(
            _group_folder(group, Folder.Kind.TIMELESS),
            "Salgshistorik.csv",
            _csv(_sales(s.today)),
            oel,
            s.ago(35),
        )

    if (group := _group_of(regnskab, s.today)) is not None:
        now_folder = _group_folder(group, Folder.Kind.PERIOD, current)
        receipts = Folder.objects.get(parent__parent=now_folder, name="Indkøb")
        s.upload(
            receipts,
            "Ulæselig scanning.jpg",
            b"\xff\xd8\xff\xe0" + b"not really a JPEG" * 40,
            regnskab,
            s.ago(1, not_before=starts),
        )
        previous = _group_folder(group, Folder.Kind.PERIOD, current.previous())
        s.upload(
            previous,
            f"Bilag {current.previous().name}.zip",
            _zip(_SCANS),
            regnskab,
            _noon(current.start - timedelta(days=5)),
        )


def _seed_search(s: _Seeder) -> None:
    """Enough for search and sorting to mean something: a dozen receipts by two people, of different
    sizes and days, numbered into two digits as receipts are; and minutes of the same name in other
    periods and groups, so search's period and space filters have something to narrow."""
    current = period_containing(s.today)
    starts = current.start
    regnskab = s.login("regnskab@gahk.dk")
    if (group := _group_of(regnskab, s.today)) is not None:
        colleague = _colleague(group, regnskab, s.today)
        now_folder = _group_folder(group, Folder.Kind.PERIOD, current)
        receipts = Folder.objects.get(parent__parent=now_folder, name="Indkøb")
        for number in range(4, 13):
            by = regnskab if number % 3 else colleague
            size = (600 + number * 97 % 700, 800 + number * 53 % 900)
            colour = ["#dd6b43", "#d8a32f", "#315b8d", "#517544", "#8a4e70"][number % 5]
            when = s.ago(number * 7 % 19 + 1, not_before=starts)
            s.upload(
                receipts, f"Kvittering {number}.jpg", _jpg(f"Kvittering {number}", colour, size), by, when
            )
    for email, name in (("ak@gahk.dk", "AK-møde referat.docx"), ("oel@gahk.dk", "Mødereferat.docx")):
        login = s.login(email)
        if (group := _group_of(login, s.today)) is None:
            continue
        previous = _group_folder(group, Folder.Kind.PERIOD, current.previous())
        s.upload(
            previous,
            name,
            _docx(name.removesuffix(".docx"), _AK_MEETING[:1]),
            login,
            _noon(current.start - timedelta(days=40)),
        )
    if (group := _group_of(s.login("oel@gahk.dk"), s.today)) is not None:
        oel = s.login("oel@gahk.dk")
        s.upload(
            _group_folder(group, Folder.Kind.PERIOD, current),
            "Mødereferat.docx",
            _docx("Mødereferat", _AK_MEETING[1:]),
            oel,
            s.ago(4, not_before=starts),
        )


def _seed_locks(s: _Seeder) -> None:
    """Locks, and requests in every state: the requests page (admin@, formand@) has two pending, two
    approved and two rejected, and whoever asked for a decided one has been told the outcome."""
    current = period_containing(s.today)
    previous = current.previous()
    formand = s.login("formand@gahk.dk")
    # The nightly lock, a week ago: what had no new version for a year (the year before last's
    # report, last year's minutes, Ølkælderen's logo).
    with _at(s.ago(7)):
        locks.auto_lock(s.ago(7))

    regnskab = s.login("regnskab@gahk.dk")
    if (group := _group_of(regnskab, s.today)) is not None:
        prev_folder = _group_folder(group, Folder.Kind.PERIOD, previous)
        closed = previous.next().start - timedelta(days=3)
        # The accounts locked by hand when the period closed, and a pending request to unlock them.
        report = _in(prev_folder, f"Regnskab {previous.name}.pdf")
        s.lock(report, regnskab, _noon(closed))
        s.ask_unlock(report, 7, "Revisor har fundet en fejl i afskrivningerne.", regnskab, s.ago(1))
        # A locked folder: nothing can be added to it, and its files can't be renamed or deleted.
        closing = s.folder(prev_folder, "Årsafslutning", regnskab, _noon(closed - timedelta(days=10)))
        s.upload(closing, "Balance.xlsx", _xlsx(_BALANCE), regnskab, _noon(closed - timedelta(days=10)))
        s.upload(
            closing,
            "Revisorpåtegning.pdf",
            _pdf("Revisorpåtegning", _AUDIT),
            regnskab,
            _noon(closed - timedelta(days=5)),
        )
        s.lock(closing, regnskab, _noon(closed))
        # Asked to purge a colleague's old version, and refused.
        timeless = _group_folder(group, Folder.Kind.TIMELESS)
        accounts = _in(timeless, "Kontoplan.xlsx")
        request = s.ask_purge(accounts, 2, "Version 2 var en fejl.", regnskab, s.ago(6))
        s.decide(
            request,
            approve=False,
            reason="Version 2 viser, hvad der blev ændret og hvorfor; den bevares.",
            by=formand,
            when=s.ago(5),
        )

    oel = s.login("oel@gahk.dk")
    if (group := _group_of(oel, s.today)) is not None:
        # The auto-locked logo, unlocked for two weeks on request, and changed while it is.
        logo = _in(_group_folder(group, Folder.Kind.TIMELESS), "Logo.png")
        request = s.ask_unlock(logo, 14, "Nyt logo til julefrokosten.", oel, s.ago(4))
        s.decide(request, approve=True, reason="", by=formand, when=s.ago(3))
        s.upload(logo.folder, logo.name, _png("Ølkælderen (ny)", "#b0702a", (600, 600)), oel, s.ago(2))
        # A binned file asked to go for good before its 30 days, waiting for a decision.
        old = File.objects.get(
            folder=_group_folder(group, Folder.Kind.PERIOD, current), name="Gammel prisliste.csv"
        )
        s.ask_purge(old, None, "Forkerte priser; den må ikke kunne gendannes.", oel, s.ago(2))

    ak = s.login("ak@gahk.dk")
    if (group := _group_of(ak, s.today)) is not None:
        # An old version purged on request: the history keeps "Version 1 ... slettet permanent af
        # Astrid Krydsen, godkendt af Frederik Formand".
        minutes = _in(_group_folder(group, Folder.Kind.PERIOD, current), "AK-møde referat.docx")
        s.upload(minutes.folder, minutes.name, _docx("AK-møde", _AK_MEETING[1:]), ak, s.ago(8))
        request = s.ask_purge(minutes, 1, "Version 1 havde et medlems CPR-nummer med.", ak, s.ago(7))
        s.decide(request, approve=True, reason="", by=formand, when=s.ago(6))
    # Last year's minutes are still writable, so ak@ may ask to unlock them - and is refused.
    general = File.objects.get(name=f"Generalforsamling {s.today.year - 1}.pdf")
    request = s.ask_unlock(general, 3, "Vil rette en stavefejl.", ak, s.ago(4))
    s.decide(
        request,
        approve=False,
        reason="Referatet er godkendt på generalforsamlingen og skal ikke ændres.",
        by=formand,
        when=s.ago(3),
    )


def wipe() -> None:
    """Every Drive row, and the bytes they point at. Folders go from the leaves up: a parent is
    PROTECTed by its children."""
    store = services.storage()
    for key in set(FileVersion.objects.values_list("key", flat=True)):
        store.delete(key)
    for row in ImagePreview.objects.all():
        for key in (row.viewer_key, row.thumbnail_key):
            if key:
                store.delete(key)
    ImagePreview.objects.all().delete()
    for archive in ZipDownload.objects.exclude(archive_key=""):
        store.delete(archive.archive_key)
    ZipDownload.objects.all().delete()
    # History and requests outlive their items (SET_NULL), so they go by themselves.
    Event.objects.all().delete()
    ApprovalRequest.objects.all().delete()
    File.objects.all().delete()
    while Folder.objects.exists():
        Folder.objects.filter(children__isnull=True).delete()
    SharedFolder.objects.all().delete()  # after its folders, which PROTECT it
    StandingFolder.objects.all().delete()  # likewise


# --- Contents ------------------------------------------------------------------------------------


def _png(title: str, colour: str, size: tuple[int, int]) -> bytes:
    return _image(title, colour, size, "PNG")


def _jpg(title: str, colour: str, size: tuple[int, int]) -> bytes:
    return _image(title, colour, size, "JPEG")


def _image(title: str, colour: str, size: tuple[int, int], kind: str) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, colour)
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 24, size[0] - 24, size[1] - 24), outline="white", width=6)
    draw.text((56, 56), title, fill="white", font_size=min(size) // 10)
    output = BytesIO()
    image.save(output, format=kind)
    return output.getvalue()


def _gif(title: str) -> bytes:
    """Animated: the viewer copy of a GIF stays a GIF."""
    from PIL import Image, ImageDraw

    frames = []
    for colour in ("#dd6b43", "#d8a32f", "#517544", "#315b8d"):
        frame = Image.new("RGB", (480, 320), colour)
        ImageDraw.Draw(frame).text((40, 40), title, fill="white", font_size=40)
        frames.append(frame)
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=400, loop=0)
    return output.getvalue()


def _heic(title: str) -> bytes:
    """What an iPhone sends; only Safari shows it, so the viewer copy is what everyone sees."""
    import pillow_heif

    pillow_heif.register_heif_opener()
    return _image(title, "#8a4e70", (1600, 1200), "HEIF")


def _media() -> tuple[bytes, bytes]:
    """A three-second clip and sound of ffmpeg's test pattern and tone (as gahkflix.demo makes)."""
    from imageio_ffmpeg import get_ffmpeg_exe

    with TemporaryDirectory(prefix="drive-demo-") as directory:
        clip, sound = Path(directory) / "clip.mp4", Path(directory) / "sound.mp3"
        for args in (
            ["-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=25", "-c:v", "libx264",
             "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(clip)],
            ["-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "libmp3lame", str(sound)],
        ):  # fmt: skip
            subprocess.run(  # noqa: S603  # nosec B603 - our own arguments
                [get_ffmpeg_exe(), "-v", "error", "-y", *args],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return clip.read_bytes(), sound.read_bytes()


def _excel_csv(rows: list[list[str]]) -> bytes:
    """As a Danish Excel saves "CSV": semicolons, CRLF and Windows' Western encoding."""
    output = StringIO()
    csv.writer(output, delimiter=";", lineterminator="\r\n").writerows(rows)
    return output.getvalue().encode("cp1252")


def _sales(today: date) -> list[list[str]]:
    """One row a day for seven years: more than the 2,000 rows the preview shows."""
    rows = [["Dato", "Øl", "Sodavand", "Omsætning (kr.)"]]
    for back in range(7 * 365, 0, -1):
        day = today - timedelta(days=back)
        beer, soda = 20 + back * 7 % 31, 4 + back * 3 % 9
        rows.append([day.isoformat(), str(beer), str(soda), str(beer * 7 + soda * 5)])
    return rows


def _zip(names: list[str]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, name in enumerate(names):
            archive.writestr(
                zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)),
                _jpg(name, ["#dd6b43", "#d8a32f", "#315b8d"][index % 3], (800, 1100)),
            )
    return output.getvalue()


def _pdf(title: str, lines: list[str]) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_creation_date(datetime(2026, 1, 1, tzinfo=UTC))  # the same bytes every run
    pdf.add_page()
    pdf.set_font("helvetica", "B", 20)
    pdf.cell(text=title, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("helvetica", size=12)
    for line in lines:
        pdf.multi_cell(0, 7, text=line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def _csv(rows: list[list[str]]) -> bytes:
    output = StringIO()
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue().encode()


def _xlsx(rows: list[list[object]]) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    assert sheet is not None  # noqa: S101  # nosec B101 - a new workbook has a sheet
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    book.save(output)
    return output.getvalue()


_DOCX_PARTS = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    ),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    ),
}


def _docx(title: str, paragraphs: list[str]) -> bytes:
    """The smallest Word document Word opens: three parts, one paragraph per line, the title bold."""
    runs = [f'<w:r><w:rPr><w:b/><w:sz w:val="32"/></w:rPr><w:t>{escape(title)}</w:t></w:r>']
    runs += [f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>' for text in paragraphs]
    body = "".join(f"<w:p>{run}</w:p>" for run in runs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in [*_DOCX_PARTS.items(), ("word/document.xml", document)]:
            archive.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), text)
    return output.getvalue()


_HOUSE_RULES = """# Husorden

Kort udgave af husordenen. Den fulde står i vedtægterne.

## Ro

- Der er ro på gangene **kl. 23–7** på hverdage.
- Fester i fællesrummet slutter kl. 2 og meldes på opslagstavlen.

## Køkkenet

1. Vask op efter dig samme dag.
2. Mærk dine ting i køleskabet med navn og dato.
3. Tøm skraldespanden, når den er fuld.

| Hvad | Hvor |
| --- | --- |
| Pap | Containeren i gården |
| Glas | Kælderen ved cykelrummet |
"""

_BOOKKEEPING = """# Sådan bogfører vi

1. Læg bilaget i `Bilag` i den periode, udgiften hører til.
2. Navngiv det med dato og beløb, fx `2026-11-03 Netto 412,50.jpg`.
3. Bogfør i e-conomic samme uge.

Spørgsmål? Skriv til regnskabsgruppen.
"""

_NOTES = """Noter fra gruppemødet

- Fordel opgaverne for resten af perioden
- Bestil nøgler til de nye
- Næste møde om to uger
"""

_PHONE_LIST = [
    ["Navn", "Rolle", "Telefon"],
    ["Vicevært", "Nøgler og reparationer", "33 12 34 56"],
    ["Inspektionen", "Husets drift", "33 12 34 57"],
    ["Vaskeriet", "Fejl på maskinerne", "70 20 30 40"],
]
_SHOPPING = [
    ["Vare", "Antal", "Ansvarlig"],
    ["Sild", "4 glas", "Bente"],
    ["Rugbrød", "6", "Ole"],
    ["Snaps", "2 flasker", "Astrid"],
    ["Risalamande", "30 portioner", "Frederik"],
]
_WORK_PLAN = [
    ["Uge", "Opgave", "Krydser"],
    ["45", "Rens tagrender", "3"],
    ["46", "Mal cykelkælderen", "4"],
    ["47", "Ryd haven for blade", "2"],
    ["48", "Juletræ i gården", "2"],
]
_PRICES = [
    ["Vare", "Pris (kr.)"],
    ["Grøn Tuborg", "7"],
    ["Carlsberg", "7"],
    ["Fadøl", "10"],
    ["Sodavand", "5"],
]
_ACCOUNTS: list[list[object]] = [
    ["Konto", "Navn", "Type"],
    [1000, "Huslejeindtægter", "Indtægt"],
    [1100, "Ølkælderen", "Indtægt"],
    [2000, "Vedligehold", "Udgift"],
    [2100, "Fester", "Udgift"],
    [2200, "Rengøringsartikler", "Udgift"],
]
_BUDGET: list[list[object]] = [
    ["Post", "Budget (kr.)", "Forbrugt (kr.)"],
    ["Vedligehold", 40000, 12450],
    ["Fester", 15000, 6200],
    ["Rengøringsartikler", 8000, 3100],
    ["I alt", "=SUM(B2:B4)", "=SUM(C2:C4)"],
]
_BUDGET_REVISED: list[list[object]] = [
    ["Post", "Budget (kr.)", "Forbrugt (kr.)"],
    ["Vedligehold", 45000, 18900],
    ["Fester", 15000, 9400],
    ["Rengøringsartikler", 8000, 3650],
    ["I alt", "=SUM(B2:B4)", "=SUM(C2:C4)"],
]
_BALANCE: list[list[object]] = [
    ["Post", "Primo (kr.)", "Ultimo (kr.)"],
    ["Bank", 182000, 176450],
    ["Tilgodehavender", 4200, 3100],
    ["Skyldige omkostninger", -12000, -9800],
]
_HARVEST: list[list[object]] = [["Vare", "Antal", "Pris (kr.)"], ["Æbler", 40, 120], ["Cider", 24, 480]]
_TRIP_BUDGET: list[list[object]] = [
    ["Post", "Kr. pr. person"],
    ["Tog", 900],
    ["Vandrehjem", 750],
    ["Mad", 600],
]
_SUPPLIERS: list[list[object]] = [
    ["Leverandør", "Kontakt", "Leveringsdag"],
    ["Bryggeriet", "ordre@bryggeriet.example", "Tirsdag"],
    ["Grossisten", "salg@grossisten.example", "Fredag"],
]
_BUDGET_TEMPLATE: list[list[object]] = [["Post", "Budget (kr.)", "Forbrugt (kr.)"], ["", 0, 0]]
_TOOLS: list[list[object]] = [
    ["Værktøj", "Antal", "Placering"],
    ["Boremaskine", 2, "Værkstedet"],
    ["Stige", 3, "Kælderen"],
    ["Malerruller", 12, "Værkstedet"],
]
_PURCHASES: list[list[object]] = [
    ["Dato", "Leverandør", "Beløb (kr.)"],
    ["2026-10-04", "Bryggeriet", 4800],
    ["2026-10-25", "Grossisten", 2150],
    ["2026-11-08", "Bryggeriet", 5100],
]
_LAUNDRY = [
    "Vaskeriet er åbent alle dage kl. 7-22.",
    "Book en tid på tavlen ved døren. En tid er to timer.",
    "Tøm filteret i tørretumbleren efter brug.",
]
_EXPENSE_FORM = ["Navn:", "Værelse:", "Beløb:", "Formål:", "Vedlæg kvitteringen."]
_AUDIT = ["Regnskabet for perioden er revideret uden bemærkninger.", "Revisor"]
_THEME_PLAN = ["Tema: 80'erne.", "Musik: Ole og Astrid.", "Pynt: Astrid.", "Mad: alle tager en ret med."]
_TRIP = ["Dag 1: Afgang kl. 8 fra Hovedbanegården.", "Dag 2: Byvandring og museum.", "Dag 3: Hjemrejse."]
_PLAYLIST = """Playliste til temafesten

1. Take On Me
2. Girls Just Want to Have Fun
3. Don't You (Forget About Me)
"""
_MEETING_AGENDA = ["1. Godkendelse af dagsorden", "2. Nyt fra Inspektionen", "3. Fester", "4. Eventuelt"]
_MEETING_MINUTES = [
    "Til stede: 20 beboere.",
    "Dagsordenen blev godkendt.",
    "Næste fællesmøde holdes om en måned.",
]
_TILL = [
    ["Dato", "Vare", "Antal", "Beløb"],
    ["03-11-2026", "Grøn Tuborg", "48", "336,00"],
    ["03-11-2026", "Fadøl", "22", "220,00"],
    ["10-11-2026", "Sodavand", "15", "75,00"],
    ["", "I alt", "", "631,00"],
]
_SCANS = ["Bilag 1.jpg", "Bilag 2.jpg", "Bilag 3.jpg"]
_HEATING_LOG = """2026-11-02 06:00:01 INFO  kedel startet, fremløb 55 °C
2026-11-02 06:15:12 WARN  tryk lavt: 0,9 bar
2026-11-02 07:30:44 INFO  tryk efterfyldt: 1,5 bar
2026-11-03 06:00:02 INFO  kedel startet, fremløb 55 °C
"""
_THERMOSTATS = """{
  "fællesrum": {"dag": 21, "nat": 17},
  "køkken": {"dag": 20, "nat": 17},
  "gange": {"dag": 18, "nat": 16}
}
"""
_PARTY_PROGRAMME = ["Kl. 14: Gårdspil", "Kl. 18: Grill i gården", "Kl. 22: Band i fællesrummet"]
_GENERAL_MEETING = [
    "Dagsorden: valg af dirigent, beretning, regnskab, indkomne forslag, eventuelt.",
    "Beretningen og regnskabet blev godkendt.",
    "Forslaget om nye cykelstativer blev vedtaget.",
]
_ANNUAL_REPORT = [
    "Året bød på en ny tagterrasse og et renoveret fælleskøkken.",
    "Kollegiet havde 61 beboere og fuld belægning hele året.",
]
_ACCOUNTS_REPORT = [
    "Indtægter og udgifter for perioden, afstemt med e-conomic.",
    "Resultat: et lille overskud, som overføres til næste periode.",
]
_CELLAR_STATUS = ["Omsætning i perioden og lagerstatus ved periodens slutning.", "Alle fadøl er talt op."]
_MINUTES_TEMPLATE = ["Dato:", "Til stede:", "Dagsorden:", "1. ", "2. ", "Næste møde:"]
_HOUSE_MEETING = [
    "Til stede: 24 beboere.",
    "Julefrokosten holdes i fællesrummet. Festudvalget står for maden.",
    "Cykelkælderen ryddes; cykler uden navn fjernes efter en måned.",
]
_AK_MEETING = [
    "Gennemgang af arbejdsplanen for resten af perioden.",
    "Tagrenderne er renset. Cykelkælderen males i uge 46.",
]
