"""Drive's folders, files and file versions (spec/features/Drive.md).

Every folder carries the three things access is decided by, so a folder's rights are read off its
own row rather than by walking up the tree: its SPACE (Fælles or an embedsgruppe's), its WORKGROUP
(for a group's folders), and PERIOD_START, the first day of the period or year it is filed under,
or null for the timeless folders (`Uden periode`, `Uden årstal`) and what is below them. They are
set when a folder is created, from its parent, and again when it is moved.

`path` is the materialised path of primary keys (`/3/17/42/`), so a folder's whole subtree - for
its file count and size - is one indexed prefix match. A move rewrites the three columns and the
path for the whole moved subtree at once (drive/operations.py).

Deleting a file or folder puts it in the bin: `deleted_at` is set and the row stays for BIN_DAYS,
so it can be restored with its versions and history. Names are unique only among what is not in
the bin. A deleted folder is kept for the binned files in it, so restoring one brings its folder
back.

Folders and files are named in URLs and forms by a random `uuid`, never by their primary key, so
nobody can count them or watch the numbers climb. The keys stay integers inside: `path` stays short
and the joins small.
"""

import unicodedata
from datetime import date, datetime
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

NAME_MAX_LENGTH = 200
# How long a deleted file stays in the bin before it is removed with all its versions.
BIN_DAYS = 30
# A file locks this long after its last new version; a folder after this long with nothing new in it.
LOCK_AFTER_DAYS = 365
# The longest an unlock lasts.
MAX_UNLOCK_DAYS = 30
# A Delt mappe: the fewest members it may have, how long it lasts unless set otherwise, how far ahead
# its expiry may be set (in years from its creation), and how long before it moderators are told.
MIN_SHARED_MEMBERS = 3
SHARED_DEFAULT_MONTHS = 6
SHARED_MAX_YEARS = 2
SHARED_WARN_DAYS = 14


def normalize_name(value: str) -> str:
    """NFC, so the "å" a Mac sends (a + combining ring) and the one Windows sends are one name."""
    return unicodedata.normalize("NFC", value)


def name_key(value: str) -> str:
    """What two names are compared by: the same name ignoring case is the same name."""
    return normalize_name(value).casefold()


def validate_name(value: str) -> None:
    """A file or folder name has to survive a zip and every operating system it is downloaded to.

    No separators, no control characters, and no leading or trailing space or trailing dot -
    Windows silently strips the last two, so the name would not come back the same.
    """
    if not value or not value.strip():
        raise ValidationError("Navnet mangler.")
    if len(value) > NAME_MAX_LENGTH:
        raise ValidationError(f"Navnet må højst være {NAME_MAX_LENGTH} tegn.")
    if "/" in value or "\\" in value:
        raise ValidationError("Et navn må ikke indeholde / eller \\.")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValidationError("Et navn må ikke indeholde kontroltegn.")
    if value != value.strip() or value.endswith("."):
        raise ValidationError("Et navn må ikke starte eller slutte med mellemrum eller slutte med punktum.")


class Lockable(models.Model):
    """A lock, set by hand or by the daily task (`locked_by` null), and lifted for a while by an
    approved unlock request (`unlocked_until`). The lock holds again the moment that time is past:
    nothing has to run for it."""

    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    unlocked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    def lock_active(self, now: datetime | None = None) -> bool:
        if self.locked_at is None:
            return False
        return self.unlocked_until is None or self.unlocked_until <= (now or timezone.now())


class Space(models.TextChoices):
    COMMON = "common", "Fælles"
    GROUP = "group", "Embedsgruppe"
    SHARED = "shared", "Delt mappe"


class Folder(Lockable):
    class Kind(models.TextChoices):
        # Made by the system, and never renamed, moved or deleted.
        COMMON_ROOT = "common_root", "Fælles"
        GROUPS_ROOT = "groups_root", "Embedsgrupper"
        GROUP = "group", "Embedsgruppe"
        TIMELESS = "timeless", "Uden periode eller årstal"
        PERIOD = "period", "Periode"
        YEAR = "year", "År"
        SHARED_ROOT = "shared_root", "Delte mapper"
        # Delte mapper/<year>/, where archived Delte mapper go.
        ARCHIVE_YEAR = "archive_year", "Arkiverede delte mapper"
        # A standing folder's folder in one period or year (StandingFolder).
        STANDING = "standing", "Stående mappe"
        # Made by residents.
        FOLDER = "folder", "Mappe"
        # The top of a Delt mappe: renamed and deleted only by its moderators, never moved by hand.
        SHARED = "shared", "Delt mappe"

    SYSTEM_KINDS = frozenset(
        {
            Kind.COMMON_ROOT,
            Kind.GROUPS_ROOT,
            Kind.GROUP,
            Kind.TIMELESS,
            Kind.PERIOD,
            Kind.YEAR,
            Kind.SHARED_ROOT,
            Kind.ARCHIVE_YEAR,
            Kind.STANDING,
        }
    )

    uuid = models.UUIDField(default=uuid4, unique=True, editable=False)  # in URLs, not the key
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    name = models.CharField(max_length=NAME_MAX_LENGTH)
    name_key = models.CharField(max_length=NAME_MAX_LENGTH, editable=False)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.FOLDER)
    space = models.CharField(max_length=10, choices=Space.choices)
    # PROTECT: a deleted workgroup must not leave its folders without the column that keeps them
    # private.
    workgroup = models.ForeignKey(
        "core.Workgroup", null=True, blank=True, on_delete=models.PROTECT, related_name="drive_folders"
    )
    period_start = models.DateField(null=True, blank=True)
    # The Delt mappe a folder is in (itself included), for the folders of that space.
    shared = models.ForeignKey(
        "SharedFolder", null=True, blank=True, on_delete=models.PROTECT, related_name="folders"
    )
    # The standing folder a folder is in (itself included), for a write limit on it.
    standing = models.ForeignKey(
        "StandingFolder", null=True, blank=True, on_delete=models.PROTECT, related_name="folders"
    )
    path = models.CharField(max_length=500, editable=False, db_index=True)
    created_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        constraints = [
            # Not among Delte mapper: a name refused because it is taken would tell that a Delt mappe
            # of that name exists.
            models.UniqueConstraint(
                fields=["parent", "name_key"],
                condition=Q(deleted_at__isnull=True) & ~Q(kind="shared"),
                name="drive_folder_unique_name",
            ),
            models.UniqueConstraint(
                fields=["kind"], condition=Q(parent__isnull=True), name="drive_one_root_of_each_kind"
            ),
            models.UniqueConstraint(
                fields=["workgroup"], condition=Q(kind="group"), name="drive_one_folder_per_group"
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args: object, **kwargs: object) -> None:
        self.name = normalize_name(self.name)
        self.name_key = name_key(self.name)
        super().save(*args, **kwargs)  # type: ignore[arg-type]
        path = f"{self.parent.path if self.parent else '/'}{self.pk}/"
        if self.path != path:
            self.path = path
            Folder.objects.filter(pk=self.pk).update(path=path)

    @property
    def is_system(self) -> bool:
        return self.kind in self.SYSTEM_KINDS

    @property
    def is_timeless(self) -> bool:
        """In `Uden periode` or `Uden årstal`, or one of them."""
        return self.period_start is None and self.kind in (self.Kind.TIMELESS, self.Kind.FOLDER)

    def ancestors(self) -> list["Folder"]:
        """From the top down, not including this folder."""
        ids = [int(part) for part in self.path.strip("/").split("/")[:-1] if part]
        by_id = Folder.objects.in_bulk(ids)
        return [by_id[pk] for pk in ids if pk in by_id]

    def display_path(self) -> str:
        """`Embedsgrupper / Regnskabsgruppen / 2026-27 Vinter / Budget`, for history lines and the bin."""
        return " / ".join([*(folder.name for folder in self.ancestors()), self.name])


class File(Lockable):
    uuid = models.UUIDField(default=uuid4, unique=True, editable=False)  # in URLs, not the key
    folder = models.ForeignKey(Folder, on_delete=models.PROTECT, related_name="files")
    name = models.CharField(max_length=NAME_MAX_LENGTH)
    name_key = models.CharField(max_length=NAME_MAX_LENGTH, editable=False)
    # The latest version's, kept here so a listing and a folder's total size need no join.
    size = models.BigIntegerField(default=0)
    content_type = models.CharField(max_length=200, blank=True)
    # Whoever uploaded the first version: notified when somebody else deletes, renames or moves it.
    created_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    changed_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    changed_at = models.DateTimeField()
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["folder", "name_key"],
                condition=Q(deleted_at__isnull=True),
                name="drive_file_unique_name",
            )
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args: object, **kwargs: object) -> None:
        self.name = normalize_name(self.name)
        self.name_key = name_key(self.name)
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def latest_version(self) -> "FileVersion":
        return self.versions.order_by("-number").first()  # type: ignore[return-value]


class FileVersion(models.Model):
    """One saved state of a file. Kept for as long as the file exists."""

    file = models.ForeignKey(File, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    # Where the bytes are: `drive/<uuid>`, never the name, which lives on the row and is put back on
    # every download. Not unique: a copy, and a restored old version, point at the bytes that are
    # already there instead of copying up to 5 GB. The bytes go only when no version points at them.
    key = models.CharField(max_length=100, db_index=True)
    # The version this one brings back, when it was made by restoring an old one.
    restored_from = models.PositiveIntegerField(null=True, blank=True)
    size = models.BigIntegerField()
    content_type = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["file", "number"], name="drive_version_unique_number")]

    def __str__(self) -> str:
        return f"{self.file.name} v{self.number}"


class LastDownload(models.Model):
    """The version a resident last downloaded of a file, so replacing it can say when somebody else
    has changed it since."""

    file = models.ForeignKey(File, on_delete=models.CASCADE, related_name="+")
    resident = models.ForeignKey("residents.Resident", on_delete=models.CASCADE, related_name="+")
    number = models.PositiveIntegerField()
    at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["file", "resident"], name="drive_one_last_download_each")
        ]


class Event(models.Model):
    """One line of an item's history. It outlives what it describes: the file and folder are set to
    null when they are removed for good, and `name` and `details` still say what happened."""

    class Action(models.TextChoices):
        CREATED = "created", "Oprettet"
        VERSION = "version", "Ny version"
        VERSION_RESTORED = "version_restored", "Version gendannet"
        RENAMED = "renamed", "Omdøbt"
        MOVED = "moved", "Flyttet"
        COPIED = "copied", "Kopieret"
        DELETED = "deleted", "Slettet"
        RESTORED = "restored", "Gendannet"
        LOCKED = "locked", "Låst"
        UNLOCK_REQUESTED = "unlock_requested", "Oplåsning ønsket"
        UNLOCK_DECIDED = "unlock_decided", "Oplåsning besluttet"
        PURGE_REQUESTED = "purge_requested", "Permanent sletning ønsket"
        PURGE_DECIDED = "purge_decided", "Permanent sletning besluttet"
        PURGED = "purged", "Slettet permanent"
        MEMBER_ADDED = "member_added", "Medlem tilføjet"
        MEMBER_REMOVED = "member_removed", "Medlem fjernet"
        MEMBER_LEFT = "member_left", "Medlem gået"
        ROLE_CHANGED = "role_changed", "Rolle ændret"
        EXPIRY_CHANGED = "expiry_changed", "Udløb ændret"
        ARCHIVED = "archived", "Arkiveret"
        SHARED = "shared", "Delt"
        UNSHARED = "unshared", "Deling stoppet"

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    action = models.CharField(max_length=30, choices=Action.choices)
    # One of the two: what the line is about.
    file = models.ForeignKey(File, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    folder = models.ForeignKey(
        Folder, null=True, blank=True, on_delete=models.SET_NULL, related_name="events"
    )
    # The item's name at the time, and what else the line needs: old and new names, paths, a
    # version number.
    name = models.CharField(max_length=NAME_MAX_LENGTH)
    details = models.JSONField(default=dict, blank=True)
    # Where the item was, so a space's activity page shows a line only to those who could read the
    # item there - also once the item itself is gone.
    space = models.CharField(max_length=10, choices=Space.choices, blank=True)
    workgroup = models.ForeignKey(
        "core.Workgroup", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    period_start = models.DateField(null=True, blank=True)
    shared = models.ForeignKey(
        "SharedFolder", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-at", "-pk"]
        indexes = [models.Index(fields=["space", "workgroup", "-at"], name="drive_event_activity")]

    def __str__(self) -> str:
        return f"{self.get_action_display()}: {self.name}"

    @property
    def summary(self) -> str:
        """The line as the history shows it, without who and when."""
        d = self.details
        match self.action:
            case self.Action.CREATED:
                return f"Oprettede {self.name}"
            case self.Action.VERSION:
                return f"Lagde version {d.get('number')} af {self.name} op"
            case self.Action.VERSION_RESTORED:
                return f"Gendannede version {d.get('restored_from')} af {self.name} som version {d.get('number')}"
            case self.Action.RENAMED:
                return f"Omdøbte {d.get('old')} til {self.name}"
            case self.Action.MOVED:
                return f"Flyttede {self.name} fra {d.get('origin')} til {d.get('to')}"
            case self.Action.COPIED:
                return f"Kopierede {self.name} fra {d.get('origin')}"
            case self.Action.DELETED:
                return f"Lagde {self.name} i papirkurven"
            case self.Action.RESTORED:
                return f"Gendannede {self.name} fra papirkurven"
            case self.Action.LOCKED:
                if d.get("automatic"):
                    return f"{self.name} blev låst efter et år uden ændringer"
                return f"Låste {self.name}"
            case self.Action.UNLOCK_REQUESTED:
                return f"Bad om at låse {self.name} op i {d.get('days')} dage: {d.get('reason')}"
            case self.Action.UNLOCK_DECIDED:
                if d.get("approved"):
                    return f"Godkendte at låse {self.name} op til {d.get('until')}"
                return f"Afviste at låse {self.name} op: {d.get('reason')}"
            case self.Action.PURGE_REQUESTED:
                return f"Bad om at slette {_what(self.name, d)} permanent: {d.get('reason')}"
            case self.Action.PURGE_DECIDED:
                return f"Afviste at slette {_what(self.name, d)} permanent: {d.get('reason')}"
            case self.Action.MEMBER_ADDED:
                return f"Gav {d.get('member')} adgang som {d.get('role')}"
            case self.Action.MEMBER_REMOVED:
                return f"Fjernede {d.get('member')}"
            case self.Action.MEMBER_LEFT:
                return f"Forlod {self.name}"
            case self.Action.ROLE_CHANGED:
                return f"Gjorde {d.get('member')} til {d.get('role')}"
            case self.Action.EXPIRY_CHANGED:
                return f"Satte udløbet til {d.get('expires_on')}, hvor mappen {d.get('then')}"
            case self.Action.SHARED:
                return f"Delte {self.name} med {d.get('recipient')}, der kan {d.get('access')}"
            case self.Action.UNSHARED:
                return f"Stoppede delingen af {self.name} med {d.get('recipient')}"
            case self.Action.ARCHIVED:
                return f"{self.name} blev arkiveret ved udløbet"
            case self.Action.PURGED:
                return f"{_what(self.name, d, capital=True)} slettet permanent af {d.get('requested_by')}, godkendt af {d.get('approved_by')}"
        return str(self)


def _what(name: str, details: dict, *, capital: bool = False) -> str:
    """`version 3 af Budget.xlsx`, or the file's name when the whole file is meant."""
    if details.get("number"):
        return f"{'V' if capital else 'v'}ersion {details['number']} af {name}"
    return name


class ApprovalRequest(models.Model):
    """A request that somebody else decides: lifting a lock for a while, or deleting a version or a
    binned file for good before its time. Admins and Inspektionen decide, never the one who asked;
    they see the name, the path and the version, never the contents."""

    class Kind(models.TextChoices):
        UNLOCK = "unlock", "Oplåsning"
        PURGE = "purge", "Permanent sletning"

    class State(models.TextChoices):
        PENDING = "pending", "Venter"
        APPROVED = "approved", "Godkendt"
        REJECTED = "rejected", "Afvist"

    uuid = models.UUIDField(default=uuid4, unique=True, editable=False)  # in URLs, not the key
    kind = models.CharField(max_length=10, choices=Kind.choices)
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING, db_index=True)
    # One of the two. Null once what it was about is gone; `name` and `where` stay.
    file = models.ForeignKey(File, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    folder = models.ForeignKey(Folder, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # The Delt mappe it is in, whose moderators decide it instead of admins and Inspektionen.
    shared = models.ForeignKey(
        "SharedFolder", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # A purge of one version, rather than of a binned file.
    version_number = models.PositiveIntegerField(null=True, blank=True)
    name = models.CharField(max_length=NAME_MAX_LENGTH)
    where = models.CharField(max_length=1000)
    days = models.PositiveSmallIntegerField(null=True, blank=True)
    reason = models.TextField()
    requested_by = models.ForeignKey(
        "residents.Resident", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.name}"


class SharedFolder(models.Model):
    """A Delt mappe: a folder for the residents invited to it, until it expires.

    Its top folder (kind SHARED) and every folder below point here. At expiry it is deleted for good,
    or archived: moved to `Delte mapper/<year it was created>/`, readable by every resident under its
    real name, and kept for good. Admins and Inspektionen see only its number, member count, file
    count and size."""

    class OnExpiry(models.TextChoices):
        DELETE = "delete", "slettes"
        ARCHIVE = "archive", "arkiveres"

    uuid = models.UUIDField(default=uuid4, unique=True, editable=False)  # in URLs, not the key
    expires_on = models.DateField()
    on_expiry = models.CharField(max_length=10, choices=OnExpiry.choices)
    created_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # The expiry the moderators were warned about; moving the date warns again.
    warned_for = models.DateField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Delt mappe {self.number}"

    @property
    def number(self) -> str:
        return f"#{self.pk:04d}"

    def top(self) -> "Folder":
        return Folder.objects.get(shared=self, kind=Folder.Kind.SHARED)

    def latest_expiry(self) -> date:
        created = timezone.localtime(self.created_at).date() if self.created_at else timezone.localdate()
        return add_years(created, SHARED_MAX_YEARS)


def add_years(day: date, years: int) -> date:
    """The same day `years` later; 29 February becomes 28 February."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


class SharedMember(models.Model):
    class Role(models.TextChoices):
        MODERATOR = "moderator", "moderator"
        EDIT = "edit", "redaktør"
        READ = "read", "læser"

    shared = models.ForeignKey(SharedFolder, on_delete=models.CASCADE, related_name="members")
    resident = models.ForeignKey("residents.Resident", on_delete=models.CASCADE, related_name="+")
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.EDIT)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["shared", "resident"], name="drive_shared_member_once")
        ]

    def __str__(self) -> str:
        return f"{self.resident_id} in {self.shared_id} ({self.role})"


class Share(models.Model):
    """A file or a resident's folder in a group's space, shared with one resident or one embedsgruppe,
    to read or to edit. It covers a folder's whole subtree, and never makes the item more open than
    the item is: editing needs an edit share while the item's period can still be written, and for a
    group the recipient's own matching folder decides who in the group sees it (drive/access.py).
    Shares do not expire; they end when the item leaves the group's space."""

    uuid = models.UUIDField(default=uuid4, unique=True, editable=False)  # in URLs, not the key
    file = models.ForeignKey(File, null=True, blank=True, on_delete=models.CASCADE, related_name="shares")
    folder = models.ForeignKey(Folder, null=True, blank=True, on_delete=models.CASCADE, related_name="shares")
    resident = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    workgroup = models.ForeignKey(
        "core.Workgroup", null=True, blank=True, on_delete=models.CASCADE, related_name="drive_shares"
    )
    can_edit = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # One item and one recipient each; drive/sharing.py keeps a pair to one share.
        constraints = [
            models.CheckConstraint(
                condition=(Q(file__isnull=True) & Q(folder__isnull=False))
                | (Q(file__isnull=False) & Q(folder__isnull=True)),
                name="drive_share_one_item",
            ),
            models.CheckConstraint(
                condition=(Q(resident__isnull=True) & Q(workgroup__isnull=False))
                | (Q(resident__isnull=False) & Q(workgroup__isnull=True)),
                name="drive_share_one_recipient",
            ),
        ]

    def __str__(self) -> str:
        return f"Share {self.pk}"

    @property
    def recipient(self) -> str:
        if self.resident_id:
            return self.resident.full_name if self.resident else ""
        return self.workgroup.name if self.workgroup else ""


class StandingFolder(models.Model):
    """A folder made again in every period or year of a space, under the same name: `Fællesmøder`
    in each year of Fælles, `Erfaringsrapporter` in each of a group's periods. It applies from the
    next period or year after it is defined (`starts_on`), and can be stopped; folders made stay.

    In Fælles it can limit writing (`limited`) to admins, Inspektionen and the members of
    `writer_groups`; everyone still reads it. A naming rule (`item_label`) gives it a button that
    asks for a date and makes `2026-11-03 Fællesmøde`."""

    uuid = models.UUIDField(default=uuid4, unique=True, editable=False)  # in URLs, not the key
    space = models.CharField(max_length=10, choices=Space.choices)
    workgroup = models.ForeignKey(
        "core.Workgroup",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="drive_standing_folders",
    )
    name = models.CharField(max_length=NAME_MAX_LENGTH)
    item_label = models.CharField(max_length=60, blank=True)
    limited = models.BooleanField(default=False)
    writer_groups = models.ManyToManyField("core.Workgroup", blank=True, related_name="+")
    starts_on = models.DateField()
    stopped_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.name

    @property
    def all_name(self) -> str:
        """`Fællesmøder (alle år)`, `Erfaringsrapporter (alle perioder)`."""
        return f"{self.name} (alle {'år' if self.space == Space.COMMON else 'perioder'})"
