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
"""

import unicodedata

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

NAME_MAX_LENGTH = 200
# How long a deleted file stays in the bin before it is removed with all its versions.
BIN_DAYS = 30


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


class Space(models.TextChoices):
    COMMON = "common", "Fælles"
    GROUP = "group", "Embedsgruppe"


class Folder(models.Model):
    class Kind(models.TextChoices):
        # Made by the system, and never renamed, moved or deleted.
        COMMON_ROOT = "common_root", "Fælles"
        GROUPS_ROOT = "groups_root", "Embedsgrupper"
        GROUP = "group", "Embedsgruppe"
        TIMELESS = "timeless", "Uden periode eller årstal"
        PERIOD = "period", "Periode"
        YEAR = "year", "År"
        # Made by residents.
        FOLDER = "folder", "Mappe"

    SYSTEM_KINDS = frozenset(
        {Kind.COMMON_ROOT, Kind.GROUPS_ROOT, Kind.GROUP, Kind.TIMELESS, Kind.PERIOD, Kind.YEAR}
    )

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
            models.UniqueConstraint(
                fields=["parent", "name_key"],
                condition=Q(deleted_at__isnull=True),
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


class File(models.Model):
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

    class Meta:
        ordering = ["-at", "-pk"]

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
        return str(self)
