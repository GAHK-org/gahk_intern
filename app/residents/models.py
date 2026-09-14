"""Residents, residency (per-month membership) and monthly role assignments.

Consolidates the legacy `intern_alumne` (directory + login principal) and `gahk_admin_user`
(role flags) into one user model with **time-bound** roles (decided 2026-06): a resident's active
privileges come from the *current month's* assignments, not a static table — see
02-schema-etl.md §5 / 99-index.md F-010. Network-closed / MAC fields are dropped (feature retired).
"""

from typing import Any

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from core.files import delete_attached_files


class Role(models.TextChoices):
    INDSTILLING = "indstilling", "Indstillingen"
    INSPEKTION = "inspektion", "Inspektionen"
    KOKKENGRUPPE = "kokkengruppe", "Køkkengruppen"
    AK = "ak", "AK"
    OELKAELDER = "oelkaelder", "Ølkælderen"
    REGNSKAB = "regnskab", "Regnskab"
    PR = "pr", "PR"  # frontpage/CMS content editors (F-006)
    REPPER = "repper", "Reppergruppen"  # repair-crew: manages the Reparationer board
    VICEVAERT = "vicevaert", "Viceværterne"  # triages Reparationer before handing it to Repper
    ADMINISTRATOR = "administrator", "Administrator"
    # NOTE: legacy `editpage` is intentionally omitted — there is no runtime CMS editing (F-006/F-007).


# Embedsgrupper (workgroups) are the monthly office groups every resident belongs to; the ones below
# are the *privileged* subset that grants a site-access role. Keyed by Workgroup.name (core.Workgroup).
# `administrator` is deliberately absent — it is not a workgroup and is managed separately.
WORKGROUP_ROLE = {
    "Indstillingen": Role.INDSTILLING,
    "Inspektionen": Role.INSPEKTION,
    "Køkkengruppen": Role.KOKKENGRUPPE,
    "AK-gruppen": Role.AK,
    "Ølkælderen": Role.OELKAELDER,
    "Regnskabsgruppen": Role.REGNSKAB,  # legacy intern_alumne_workgroup name (id 23)
    "PR-gruppen": Role.PR,  # grants CMS/frontpage editing
    "Repperne": Role.REPPER,  # manages the Reparationer board (reparationer.views.MANAGE_ROLES)
    "Vicevært": Role.VICEVAERT,  # triages Reparationer before handing it to Repperne
}
WORKGROUP_ROLE_VALUES = frozenset(WORKGROUP_ROLE.values())


class ResidentManager(BaseUserManager["Resident"]):
    use_in_migrations = True

    def get_by_natural_key(self, username: str | None) -> "Resident":
        """Look up the login principal (email) case-insensitively.

        Email is the USERNAME_FIELD, and an email login id should not be case-sensitive - residents
        typing a capital first letter could not log in (auth/login ticket). ModelBackend.authenticate
        resolves the user through here, so making it case-insensitive fixes login for every path.

        Exact match first: unambiguous, preserves behaviour, and avoids MultipleObjectsReturned if two
        rows ever differ only by case. Fall back to iexact only when the exact form isn't found.
        """
        field = self.model.USERNAME_FIELD
        try:
            return self.get(**{field: username})
        except self.model.DoesNotExist:
            return self.get(**{f"{field}__iexact": username})

    def create_user(self, email: str, password: str | None = None, **extra: object) -> "Resident":
        if not email:
            raise ValueError("Residents must have an email address")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra: object) -> "Resident":
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        return self.create_user(email, password, **extra)


class Resident(AbstractBaseUser, PermissionsMixin):
    # identity / login (legacy intern_alumne.email becomes the unique login id)
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=40, blank=True)
    # residency facts
    birthday = models.DateField(null=True, blank=True)
    move_in_date = models.DateField(null=True, blank=True)
    move_out_date = models.DateField(null=True, blank=True)
    study = models.CharField(max_length=255, blank=True)
    # lineage: fylgje = fadder/sponsor (an older resident who introduces a newcomer) — F-011
    sponsor = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="proteges"
    )
    fylgje_raw = models.CharField(max_length=255, blank=True)  # original free-text, kept if unresolved
    # profile
    profile_picture = models.FileField(upload_to="profile_pictures/", blank=True)
    bio = models.TextField(max_length=500, blank=True)
    facebook_link = models.URLField(blank=True)
    instagram_handle = models.CharField(max_length=50, blank=True)
    # django auth
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(
        default=False
    )  # = holds any role in the active period (set by ETL/signals)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = ResidentManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    class Meta:
        ordering = ["first_name", "last_name"]

    def __str__(self) -> str:
        return f"{self.full_name} <{self.email}>"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def has_role(self, role: str, period: tuple[int, int] | None = None) -> bool:
        year, month = period or active_period()
        return self.role_assignments.filter(role=role, year=year, month=month).exists()


class Residency(models.Model):
    """One row per resident per month (legacy intern_alumne_liste): which room + chore groups."""

    resident = models.ForeignKey(Resident, on_delete=models.CASCADE, related_name="residencies")
    room = models.ForeignKey("core.Room", on_delete=models.PROTECT, related_name="residencies")
    workgroup = models.ForeignKey("core.Workgroup", null=True, blank=True, on_delete=models.SET_NULL)
    cleaning = models.ForeignKey("core.Cleaning", null=True, blank=True, on_delete=models.SET_NULL)
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()  # 1..12 (decoded from legacy monthNumber = 12*y+m)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["resident", "year", "month"], name="uniq_resident_month")
        ]
        indexes = [models.Index(fields=["year", "month"])]
        verbose_name_plural = "residencies"

    def __str__(self) -> str:
        return f"{self.resident.full_name} — {self.year}-{self.month:02d}"


class RoleAssignment(models.Model):
    """A privilege/embedsgruppe role held by a resident **for a specific month** (decided 2026-06)."""

    resident = models.ForeignKey(Resident, on_delete=models.CASCADE, related_name="role_assignments")
    role = models.CharField(max_length=20, choices=Role.choices)
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["resident", "role", "year", "month"], name="uniq_role_assignment")
        ]
        indexes = [models.Index(fields=["role", "year", "month"])]

    def __str__(self) -> str:
        return f"{self.resident.full_name} = {self.get_role_display()} ({self.year}-{self.month:02d})"


def active_period() -> tuple[int, int]:
    """The (year, month) currently *in effect*: the most recent published residency list that has
    already started. A list indstilling is preparing for a future month does NOT become active until
    that month arrives. Falls back to the calendar month when no (past-or-current) list exists.

    Per F-010: the newest monthly list governs — but future-dated lists are held back so next month's
    roster can be edited ahead of time without changing who has access now.

    Reads the date via core.clock.current_date so a developer can fast-forward the month locally
    (DEBUG only); in prod this is exactly timezone.localdate().
    """
    from core.clock import current_date

    today = current_date()
    latest = (
        Residency.objects.filter(
            models.Q(year__lt=today.year) | models.Q(year=today.year, month__lte=today.month)
        )
        .order_by("-year", "-month")
        .values("year", "month")
        .first()
    )
    if latest:
        return latest["year"], latest["month"]
    return today.year, today.month


def embedsgruppe_of(resident: "Resident", period: tuple[int, int] | None = None) -> str:
    """The Workgroup name `resident` holds in `period` (the active month by default), or "".

    Read at the moment something is written, never when it is displayed: opslagstavle.models stamps
    it onto a post and onto a comment as they are created. Embedsgrupper rotate monthly (F-010), so
    resolving a byline against today's list would relabel every post on the board each time the
    månedsliste is saved, and a two-year archive would spend most of its life wrong.

    "" for someone with no residency that month, or one with no group — an alumnus whose posts stay
    on the board is the ordinary case, and the template renders no pill rather than an empty one.
    """
    year, month = period or active_period()
    name = (
        Residency.objects.filter(resident=resident, year=year, month=month)
        .values_list("workgroup__name", flat=True)
        .first()
    )
    return name or ""


def next_period(period: tuple[int, int] | None = None) -> tuple[int, int]:
    """The month after `period` (defaults to the active period), as (year, month)."""
    year, month = period or active_period()
    return (year + 1, 1) if month == 12 else (year, month + 1)


def prev_period(period: tuple[int, int] | None = None) -> tuple[int, int]:
    """The month before `period` (defaults to the active period), as (year, month)."""
    year, month = period or active_period()
    return (year - 1, 12) if month == 1 else (year, month - 1)


@receiver(post_delete, sender=Resident)
def _delete_resident_files(sender: type[models.Model], instance: models.Model, **kwargs: Any) -> None:  # noqa: ANN401
    """Remove the profile picture from storage when a resident row is deleted."""
    delete_attached_files(instance)
