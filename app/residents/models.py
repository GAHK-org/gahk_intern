"""Residents, residency (per-month membership) and monthly role assignments.

Consolidates the legacy `intern_alumne` (directory + login principal) and `gahk_admin_user`
(role flags) into one user model with **time-bound** roles (decided 2026-06): a resident's active
privileges come from the *current month's* assignments, not a static table — see
02-schema-etl.md §5 / 99-index.md F-010. Network-closed / MAC fields are dropped (feature retired).
"""

import logging
from typing import Any

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.cache import cache
from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from core.files import delete_attached_files, delete_files_after_commit


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
    FOTO = "foto", "Fotogruppen"  # curates the photo album: approves, bins, locks (photo_album.access)
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
    "Fotogruppen": Role.FOTO,  # curates the photo album (photo_album.access)
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


# Attribute `_has_admin_role` memoises its grant lookup under, on the request's user instance.
_ADMIN_GRANT_MEMO = "_gahk_admin_grant"

# Edge of the square profile thumbnail, in px. The largest avatar is the profile page's 64 CSS px,
# and 3x covers the densest phone screen.
PROFILE_THUMBNAIL_SIZE = 192
# How long one process waits before asking the worker for the same thumbnail again. Long enough that
# a page full of avatars does not queue a job per render, short enough that a job lost with a worker
# restart, or a picture that could not be read, is tried again the same day.
PROFILE_THUMBNAIL_REQUEUE_SECONDS = 3600

logger = logging.getLogger(__name__)


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
    # e-conomic's chart-of-accounts accountNumber for this alumne (F-010 regnskab role; alumneregning
    # automation). GAHK does not track alumni as e-conomic Customers/debtors — each alumne has their
    # own GL account instead, and their balance/statement is the sum of that account's entries. Set
    # by regnskab once; unset for most current residents — only alumni actually billed through
    # e-conomic have one. (Named economic_customer_number in an earlier version of this feature,
    # before it was confirmed GAHK uses per-alumne accounts, not e-conomic Customer records.)
    economic_account_number = models.PositiveIntegerField(null=True, blank=True, unique=True)
    # profile
    profile_picture = models.FileField(upload_to="profile_pictures/", blank=True)
    # A small square copy of profile_picture, made by the worker (residents.tasks). Every avatar on
    # the site draws this; only the profile page's "open in viewer" link reaches the original. See
    # avatar_url.
    profile_thumbnail = models.FileField(upload_to="profile_pictures/thumbnails/", blank=True, editable=False)
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

    # profile_picture's name as last loaded or saved; see from_db/save below.
    _stored_picture: str | None

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    class Meta:
        ordering = ["first_name", "last_name"]

    def __str__(self) -> str:
        return f"{self.full_name} <{self.email}>"

    # --- profile thumbnail --------------------------------------------------------------------------
    #
    # The largest avatar on the site is 64 CSS px, and phones uploaded originals of several MB. Den
    # Hurtige draws one per message and the alumneliste one per row, so every page view was pulling
    # whole camera photos to paint circles a few dozen pixels across (#177).
    #
    # The thumbnail is made by the Celery worker (residents.tasks.make_profile_thumbnail), so an upload
    # does not wait for the original to be read back from the bucket and resized. It is asked for from
    # two places:
    #
    #   save()      whenever the picture changes, however it was changed: the edit form, the admin
    #               and seed_demo all assign profile_picture in different ways, and all of them end in
    #               save(). It compares against the name loaded from the database rather than checking
    #               for an uncommitted upload, because FieldFile.save() (seed_demo) commits the file
    #               before save() is called.
    #   avatar_url  whenever an avatar is drawn without one. That is the backfill for pictures that
    #               predate the thumbnail, and the retry for a job that was lost, with no command to
    #               remember after a deploy.
    #
    # Until the worker has made it, avatars draw the original, as they did before #177.

    @classmethod
    def from_db(cls, db: str | None, field_names: Any, values: Any) -> "Resident":  # noqa: ANN401
        instance = super().from_db(db, field_names, values)
        # Raw from __dict__ so a deferred field is not fetched just to remember it; absent means
        # "unknown", and an unknown picture is never treated as changed.
        instance._stored_picture = instance.__dict__.get("profile_picture")
        return instance

    def save(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        update_fields = kwargs.get("update_fields")
        stored = getattr(self, "_stored_picture", "" if self._state.adding else None)
        changed = (
            (update_fields is None or "profile_picture" in update_fields)
            and stored is not None
            and (self.profile_picture.name or "") != (stored or "")
        )
        super().save(*args, **kwargs)
        self._stored_picture = self.profile_picture.name or ""
        if changed:
            self._drop_profile_thumbnail()
            self.queue_profile_thumbnail()

    def _drop_profile_thumbnail(self) -> None:
        """Forget the thumbnail of the previous picture, and delete its file once this commits.

        At once rather than when the new one is ready: until then the avatar should show the new
        picture at full size, not the old one small.
        """
        old = self.profile_thumbnail.name
        if not old:
            return
        self.profile_thumbnail = ""
        # update(), not save(): this runs inside save(), and only this one column has changed.
        Resident.objects.filter(pk=self.pk).update(profile_thumbnail="")
        delete_files_after_commit([(self.profile_thumbnail.storage, old)], f"residents.Resident:{self.pk}")

    def queue_profile_thumbnail(self) -> None:
        """Ask the worker for the current picture's thumbnail, at most once an hour per process.

        The cache key is what keeps avatar_url cheap: an alumneliste page draws dozens of avatars,
        and without it every render of every one still waiting would add a job to the queue. It names
        the picture as well as the resident, so a new picture is asked for straight away. The job
        itself is idempotent, so the per-process cache letting a few duplicates through is harmless.
        """
        name = self.profile_picture.name
        if not name or not cache.add(
            f"profile-thumbnail:{self.pk}:{name}", True, PROFILE_THUMBNAIL_REQUEUE_SECONDS
        ):
            return
        from .tasks import make_profile_thumbnail

        pk = self.pk

        def enqueue() -> None:
            # Broad on purpose: this can run while a page is rendering, and a queue that is down must
            # cost the avatar its thumbnail, not the page.
            try:
                make_profile_thumbnail.delay(pk, name)
            except Exception:
                logger.exception("could not queue the profile thumbnail for resident %s", pk)

        transaction.on_commit(enqueue)

    @property
    def avatar_url(self) -> str:
        """The URL every avatar should draw: the thumbnail, or the original until the worker has made one."""
        if self.profile_thumbnail:
            return self.profile_thumbnail.url
        if not self.profile_picture:
            return ""
        self.queue_profile_thumbnail()
        return self.profile_picture.url

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def has_role(self, role: str, period: tuple[int, int] | None = None) -> bool:
        year, month = period or active_period()
        return self.role_assignments.filter(role=role, year=year, month=month).exists()

    def has_perm(self, perm: str, obj: models.Model | None = None) -> bool:
        """Netvaerksgruppen (spelled `administrator`) gets every Django admin permission, for exactly
        as long as they hold the role AND a second administrator has accepted the grant. Derived per
        request rather than stored in `is_superuser`: role holding expires with the monthly period,
        and nothing fires on that rollover."""
        return super().has_perm(perm, obj) or self._has_admin_role()

    def has_module_perms(self, app_label: str) -> bool:
        return super().has_module_perms(app_label) or self._has_admin_role()

    def _has_admin_role(self) -> bool:
        from .permissions import has_active_role  # imported here: permissions imports this module

        if not (self.is_active and has_active_role(self, Role.ADMINISTRATOR)):
            return False
        # Memoised per instance, like real_roles: the admin index asks once per registered model.
        approved = getattr(self, _ADMIN_GRANT_MEMO, None)
        if approved is None:
            approved = AdminAccessGrant.objects.filter(
                resident_id=self.pk, status=AdminAccessGrant.Status.APPROVED
            ).exists()
            setattr(self, _ADMIN_GRANT_MEMO, approved)
        return approved


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


class AdminAccessGrant(models.Model):
    """One resident's accepted (or pending) Django-admin access — the two-person rule on `administrator`.

    Holding the role is necessary but not sufficient: a *second* administrator must accept before
    `Resident._has_admin_role` returns True. Both doors that mint the role (the role editor and the
    monthly list roll-forward) are reachable by the group itself, so without this Netvaerksgruppen
    decides its own membership. Opened and closed by residents.signals, decided in residents.admin_access.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Afventer godkendelse"
        APPROVED = "approved", "Godkendt"
        DENIED = "denied", "Afvist"

    resident = models.OneToOneField(Resident, on_delete=models.CASCADE, related_name="admin_access")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    # Who granted the role, when known. The role editor stamps it; the monthly roll-forward and the
    # ETL leave it NULL, and a NULL only means the "granter may not approve" rule has nobody to bar.
    requested_by = models.ForeignKey(
        Resident, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_by = models.ForeignKey(
        Resident, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.resident.full_name} — {self.get_status_display()}"


class EconomicLedgerEntry(models.Model):
    """One posting from e-conomic's own ledger, mirrored locally (F-010 regnskab, alumneregning
    automation). e-conomic remains the real books; this is GAHK's own permanent, append-only copy of
    what e-conomic said at sync time — so a balance or a statement can be read from gahk-intern's own
    data instead of a live API call every time, and the record survives even if e-conomic access ever
    lapses. Matches how AkEntry/oelkaelder.Transaction already work: entries summed, never edited or
    deleted — see residents.ledger.sync_entries, which only ever INSERTs.

    is_draft distinguishes e-conomic's two entry id sequences, which are independent and can collide
    numerically: posted entries have their own entryNumber, draft/journal entries (not yet formally
    booked — GAHK's regnskab wants these included, not just posted ones, see F-010) have their own
    journalEntryNumber from a completely separate counter. (entry_number, is_draft) together are the
    sync key, not entry_number alone.
    """

    resident = models.ForeignKey(Resident, on_delete=models.PROTECT, related_name="economic_entries")
    entry_number = models.PositiveIntegerField()  # e-conomic's entryNumber, or journalEntryNumber if draft
    is_draft = models.BooleanField(default=False)
    date = models.DateField()
    text = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)  # + = resident owes GAHK (e-conomic's sign)
    entry_type = models.CharField(max_length=40, blank=True)
    voucher_number = models.PositiveIntegerField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    synced_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["date", "entry_number"]
        indexes = [models.Index(fields=["resident", "date"])]
        constraints = [
            models.UniqueConstraint(fields=["entry_number", "is_draft"], name="uniq_entry_number_per_kind")
        ]

    def __str__(self) -> str:
        return f"{self.resident.full_name} — {self.date} {self.amount} kr (e-conomic #{self.entry_number})"


# string.Template source for the default alumneregning mail body (residents.alumneregning._body
# substitutes $FORNAVN/$SALDO_LINE/$REG_NR/$KONTO_NR). Matches the wording the legacy script's
# message.txt used, kept as the starting point regnskab edits from in AlumneregningSettings.
DEFAULT_ALUMNEREGNING_MESSAGE = (
    "Hej $FORNAVN,\n"
    "\n"
    "Os i Regnskabsgruppen sender dig hermed (vedhæftet) din alumneregning.\n"
    "\n"
    "$SALDO_LINE\n"
    "\n"
    "Vi beder om at du generelt forsøger at holde din gæld så lav som muligt.\n"
    "\n"
    "Hvis du skylder mere end 600 kr. beder vi om at du indbetaler inden for udgangen af "
    "indeværende uge. Skulle du have brug for en speciel aftale om afbetaling, fordi din gæld er "
    "for stor til, at du kan betale den af på én gang, så bedes du kontakte det siddende "
    "inspektionsmedlem i Regnskabsgruppen.\n"
    "\n"
    "UDFLYTTER-INFO: Denne regning skal ikke betales.\n"
    "I tilfælde af at du skylder alumnekassen penge bliver dette fratrukket dit depositum som PKS "
    "tilbagebetaler snarest.\n"
    "\n"
    "------------------------------------------------------------------------\n"
    "\n"
    "På vegne af Regnskabsgruppen på G. A. Hagemanns Kollegium\n"
    "\n"
    "Reg. nr. $REG_NR Konto nr. $KONTO_NR\n"
    "\n"
    "------------------------------------------------------------------------"
)


class AlumneregningSettings(models.Model):
    """The editable-in-the-UI content every alumneregning mail is built from (F-010 regnskab):
    the message body template and an optional følgebrev (cover letter) PDF, merged in as the
    statement PDF's first page (residents.statement_pdf.prepend_followup) rather than sent as a
    separate attachment. Regnskab expects to swap the følgebrev out roughly every 6 months, hence it
    being editable here rather than baked into the generated statement. Singleton — always pk=1 —
    because there is exactly one of these at a time, same shape as the legacy system's ID=1
    `intern_alumne_emailtonew` row it replaces.
    """

    message_template = models.TextField(default=DEFAULT_ALUMNEREGNING_MESSAGE)
    followup_pdf = models.FileField(upload_to="alumneregning/", blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Alumneregning-indstillinger"
        verbose_name_plural = "Alumneregning-indstillinger"

    def __str__(self) -> str:
        return "Alumneregning-indstillinger"

    @classmethod
    def get_solo(cls) -> "AlumneregningSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


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
