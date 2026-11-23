"""Who may see, open and write what in Drive - the access table in spec/features/Drive.md.

STAGED ROLLOUT: limited to administrators and Inspektionen while it is tried out - except with DEBUG
on, where every resident is let in, so a resident's Drive can be tried locally.

    TO OPEN IT TO EVERY RESIDENT: set ACCESS_ROLES to None.

Every rule is decided per folder from its own row (space, workgroup, period_start; see models.py)
and a Viewer, which holds what the rules need to know about the resident, read once per request:
which embedsgrupper they are in this month and next month, every month they have been in each, and
whether they oversee (administrators and Inspektionen - an administrator holds every role, so
asking for Inspektionen's covers both).

A resident who has moved out has no access to anything. Nothing is deleted when they move: their
past months stay in the residency lists, so they get exactly the same access back if they return.

A DELT MAPPE is its members' only: moderators and editors write, readers read. Admins and
Inspektionen see that it exists - its tag, members, files and size - and nothing else. Once
archived it is read by every resident.

LOCKS narrow writing further (models.Lockable). A folder's lock covers everything below it; a
file's covers only the file. `may_write` is the table alone, whatever is locked: it says who may
lock by hand and ask for an unlock, so an unlock never gives anyone more than the table does.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from enum import IntEnum

from django.conf import settings
from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone

from core.clock import current_date
from core.rollout import Gate
from residents.models import Residency, Resident, Role, RoleAssignment, active_period
from residents.permissions import current_resident, has_active_role, request_has_role

from .models import File, Folder, Share, SharedFolder, SharedMember, Space
from .periods import Period, period_containing

# Where nothing is put directly: residents' things go in the timeless, period and year folders.
_ROOTS = (
    Folder.Kind.COMMON_ROOT,
    Folder.Kind.GROUPS_ROOT,
    Folder.Kind.GROUP,
    Folder.Kind.SHARED_ROOT,
    Folder.Kind.ARCHIVE_YEAR,
)

# None = every logged-in resident.
ACCESS_ROLES: tuple[str, ...] | None = (Role.ADMINISTRATOR, Role.INSPEKTION)


def _access_roles() -> tuple[str, ...] | None:
    """ACCESS_ROLES, or nobody kept out at all in local development. Read on every call (core.rollout)."""
    return None if settings.DEBUG else ACCESS_ROLES


gate = Gate(_access_roles)
access_required = gate.required
roles_allowed = gate.roles_allowed


class Level(IntEnum):
    NONE = 0
    # Name, file count and size, never the contents: admins and Inspektionen on a group's current
    # and next period.
    META = 1
    READ = 2
    WRITE = 3


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


@dataclass(frozen=True)
class Grant:
    """One share, as access needs it: the item (a folder's path, or a file), the item's period, and
    to whom - the resident (workgroup_id None) or a group."""

    path: str
    file_id: int | None
    period_start: date | None
    workgroup_id: int | None
    can_edit: bool

    @classmethod
    def load(cls, resident_id: int, groups: set[int]) -> list["Grant"]:
        rows = Share.objects.filter(Q(resident_id=resident_id) | Q(workgroup_id__in=groups)).select_related(
            "folder", "file__folder"
        )
        grants = []
        for share in rows:
            if share.folder is not None and share.folder.deleted_at is None:
                grants.append(
                    cls(
                        share.folder.path, None, share.folder.period_start, share.workgroup_id, share.can_edit
                    )
                )
            elif share.file is not None and share.file.deleted_at is None:
                period = share.file.folder.period_start
                grants.append(cls("", share.file_id, period, share.workgroup_id, share.can_edit))
        return grants


@dataclass
class Viewer:
    resident_id: int
    today: date
    living: bool
    oversees: bool
    groups_now: set[int] = field(default_factory=set)
    groups_next: set[int] = field(default_factory=set)
    months: dict[int, set[tuple[int, int]]] = field(default_factory=dict)
    # Delt mappe id -> this resident's role in it.
    shared_roles: dict[int, str] = field(default_factory=dict)
    # The archived Delte mapper, read by everyone.
    archived: set[int] = field(default_factory=set)
    # What has been shared with the viewer, or with a group they have been in.
    grants: list["Grant"] = field(default_factory=list)
    # folder id -> the folder whose lock covers it, per request.
    _locks: dict[int, Folder | None] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def for_request(cls, request: HttpRequest) -> "Viewer":
        cached = getattr(request, "_drive_viewer", None)
        if cached is None:
            cached = cls.build(
                current_resident(request).pk, oversees=request_has_role(request, Role.INSPEKTION)
            )
            cached.living = cached.living or request.user.is_superuser
            request._drive_viewer = cached  # type: ignore[attr-defined]
        return cached

    @classmethod
    def for_resident(cls, resident: Resident) -> "Viewer":
        """Somebody other than the one asking: whether a file's creator can still see its folder."""
        viewer = cls.build(resident.pk, oversees=has_active_role(resident, Role.INSPEKTION))
        viewer.living = viewer.living or resident.is_superuser
        return viewer

    @classmethod
    def build(cls, resident_id: int, *, oversees: bool, today: date | None = None) -> "Viewer":
        now = active_period()
        following = _next_month(*now)
        months: dict[int, set[tuple[int, int]]] = defaultdict(set)
        living = False
        groups_now: set[int] = set()
        groups_next: set[int] = set()
        for year, month, workgroup in Residency.objects.filter(resident_id=resident_id).values_list(
            "year", "month", "workgroup_id"
        ):
            if (year, month) == now:
                living = True
            if workgroup is None:
                continue
            months[workgroup].add((year, month))
            if (year, month) == now:
                groups_now.add(workgroup)
            elif (year, month) == following:
                groups_next.add(workgroup)
        # Administrators need not live here (notifications.services.everyone).
        living = (
            living
            or RoleAssignment.objects.filter(resident_id=resident_id, year=now[0], month=now[1]).exists()
        )
        return cls(
            resident_id=resident_id,
            today=today or current_date(),
            living=living,
            oversees=oversees,
            groups_now=groups_now,
            groups_next=groups_next,
            months=dict(months),
            shared_roles=dict(
                SharedMember.objects.filter(resident_id=resident_id).values_list("shared_id", "role")
            ),
            archived=set(SharedFolder.objects.filter(archived_at__isnull=False).values_list("pk", flat=True)),
            grants=Grant.load(resident_id, set(months)),
        )

    def was_member(self, workgroup: int, period: Period) -> bool:
        """In the group for at least one month of the period."""
        mine = self.months.get(workgroup, set())
        return any(month in mine for month in period.months())

    def level(self, folder: Folder, *, shares: bool = True) -> Level:
        """`shares=False`: the access table alone, without what has been shared with the viewer."""
        if not self.living:
            return Level.NONE
        if folder.space == Space.COMMON:
            return self._common(folder)
        if folder.space == Space.SHARED:
            return self._shared(folder)
        own = self._group(folder)
        if not shares or own == Level.WRITE:
            return own
        via = [self._via(grant) for grant in self.grants if grant.path and folder.path.startswith(grant.path)]
        return max([own, *via])

    def file_level(self, file: File) -> Level:
        """A file's folder's level, or more where the file itself is shared with the viewer."""
        own = self.level(file.folder)
        via = [self._via(grant) for grant in self.grants if grant.file_id == file.pk]
        return max([own, *via]) if self.living else Level.NONE

    def _via(self, grant: "Grant") -> Level:
        """What a share gives: read, or write when it is an edit share and the item's period can
        still be written. A group's share reaches only those its own matching folder lets in -
        as members, never as overseers."""
        current = period_containing(self.today)
        open_period = grant.period_start is None or Period(grant.period_start) >= current.previous()
        writes = grant.can_edit and open_period
        if grant.workgroup_id is None:
            return Level.WRITE if writes else Level.READ
        theirs = Folder(
            kind=Folder.Kind.FOLDER,
            space=Space.GROUP,
            workgroup_id=grant.workgroup_id,
            period_start=grant.period_start,
        )
        recipient = self._group(theirs, oversight_counts=False)
        if recipient < Level.READ:
            return Level.NONE
        return Level.WRITE if writes and recipient == Level.WRITE else Level.READ

    def can_share(self, folder: Folder) -> bool:
        """Share a file in `folder`, or `folder` itself: things in a group's space, by who can write
        there by the access table - not by a share."""
        return (
            folder.space == Space.GROUP
            and folder.kind not in _ROOTS
            and self.level(folder, shares=False) >= Level.WRITE
        )

    def can_version(self, file: File) -> bool:
        """Add a version: whoever may change the file, or anyone it is shared with to edit."""
        if file.lock_active() or self.lock_holder(file.folder) is not None:
            return False
        return self.can_change_file_in(file.folder) or self.file_level(file) >= Level.WRITE

    def _shared(self, folder: Folder) -> Level:
        if folder.kind in (Folder.Kind.SHARED_ROOT, Folder.Kind.ARCHIVE_YEAR):
            return Level.READ
        shared = folder.shared_id
        if shared is None:
            return Level.NONE
        if shared in self.archived:
            return Level.READ
        role = self.shared_roles.get(shared)
        if role is None:
            # That it exists, never what is in it: only the Delt mappe itself, under its tag.
            return Level.META if self.oversees and folder.kind == Folder.Kind.SHARED else Level.NONE
        return Level.READ if role == SharedMember.Role.READ else Level.WRITE

    def moderates(self, shared_id: int | None) -> bool:
        return (
            self.living
            and shared_id is not None
            and shared_id not in self.archived
            and self.shared_roles.get(shared_id) == SharedMember.Role.MODERATOR
        )

    def _common(self, folder: Folder) -> Level:
        if folder.kind == Folder.Kind.COMMON_ROOT:
            return Level.READ
        if folder.period_start is None:  # Uden årstal
            return Level.WRITE
        # The current year, the next once it has a folder, and the most recent previous one.
        return Level.WRITE if folder.period_start.year >= self.today.year - 1 else Level.READ

    def _group(self, folder: Folder, *, oversight_counts: bool = True) -> Level:
        oversees = self.oversees and oversight_counts
        if folder.kind == Folder.Kind.GROUPS_ROOT:
            return Level.READ
        workgroup = folder.workgroup_id
        if workgroup is None:  # every folder below Embedsgrupper belongs to a group
            return Level.NONE
        oversight = Level.NONE
        if folder.kind == Folder.Kind.GROUP:
            # The way in to the periods: anyone who has ever been in the group, and the overseers.
            ever = workgroup in self.months
            return Level.READ if ever or oversees else Level.NONE
        if folder.period_start is None:  # Uden periode
            if workgroup in self.groups_now:
                return Level.WRITE
            return Level.READ if oversees else Level.NONE

        period = Period(folder.period_start)
        current = period_containing(self.today)
        now = workgroup in self.groups_now
        was = self.was_member(workgroup, period)
        if period > current:
            # The next period, from LEAD before it begins: the group now and the group to come.
            mine = Level.WRITE if now or workgroup in self.groups_next else Level.NONE
            oversight = Level.META if oversees else Level.NONE
        elif period == current:
            mine = Level.WRITE if now else Level.READ if was else Level.NONE
            oversight = Level.META if oversees else Level.NONE
        elif period == current.previous():
            # Corrections after the period, by those who were in it and still are.
            mine = Level.WRITE if now and was else Level.READ if now or was else Level.NONE
            oversight = Level.READ if oversees else Level.NONE
        else:
            mine = Level.READ if now or was else Level.NONE
            oversight = Level.READ if oversees else Level.NONE
        return max(mine, oversight)

    def may_write(self, folder: Folder) -> bool:
        """Write access by the access table alone, whatever is locked."""
        return folder.kind not in _ROOTS and self.level(folder) >= Level.WRITE

    def lock_holder(self, folder: Folder) -> Folder | None:
        """The folder whose lock covers `folder` - itself or the topmost locked one above it."""
        if folder.pk not in self._locks:
            ids = [int(part) for part in folder.path.strip("/").split("/") if part]
            now = timezone.now()
            held = [
                f for f in Folder.objects.filter(pk__in=ids, locked_at__isnull=False) if f.lock_active(now)
            ]
            self._locks[folder.pk] = min(held, key=lambda f: len(f.path)) if held else None
        return self._locks[folder.pk]

    def can_create_in(self, folder: Folder) -> bool:
        """Folders and files go in the timeless, period and year folders and below, never directly in
        a root or a group folder, and never in a locked folder. The first level of Fælles' `Uden
        årstal` is admins' and Inspektionen's to arrange."""
        if not self.can_upload_to(folder):
            return False
        return not (
            folder.space == Space.COMMON and folder.kind == Folder.Kind.TIMELESS and not self.oversees
        )

    def can_upload_to(self, folder: Folder) -> bool:
        return self.may_write(folder) and self.lock_holder(folder) is None

    def can_change_folder(self, folder: Folder) -> bool:
        """Rename, move or delete a folder: never one the system made, and only where a folder like it
        could be created - so the first level of Fælles' `Uden årstal` stays admins' and
        Inspektionen's to arrange."""
        if folder.is_system or folder.parent is None or folder.kind == Folder.Kind.SHARED:
            return False  # a Delt mappe itself: drive/shared.py, by its moderators
        return self.lock_holder(folder) is None and self.can_create_in(folder.parent)

    def can_change_file_in(self, folder: Folder) -> bool:
        """Change the files in this folder, as far as the folder goes (see can_change_file)."""
        return self.can_upload_to(folder)

    def can_change_file(self, file: File) -> bool:
        """Rename, move, delete or add a version to this file: its folder allows it, and the file
        itself is not locked."""
        return not file.lock_active() and self.can_change_file_in(file.folder)
