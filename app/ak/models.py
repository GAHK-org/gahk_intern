"""AK — Aktivitetskrydser (F-009).

Each month every resident is assessed -2 krydser (the cost of the 2 hours of dorm labour they owe);
doing labour adds krydser back. The running balance may go negative (= behind/in debt); the goal is to
stay ≥ 0. The AK embedsgruppe (role `ak`) tracks and adjusts crosses, for themselves and others.

Modelled as an **append-only ledger** (one row per change) instead of the legacy running-total column +
buggy bulk ops (`monthNumber='24178'`, `-1*` insert). The balance is the SUM of entries - always
consistent, and the monthly assessment / labour / adjustments are just entries.
"""

from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone

from residents.models import Resident


class AkEntry(models.Model):
    class Kind(models.TextChoices):
        MONTHLY = "monthly", "Månedlig afregning"
        LABOUR = "labour", "Udført arbejde"
        ADJUSTMENT = "adjustment", "Manuel justering"
        OPENING = "opening", "Startsaldo (migreret)"

    resident = models.ForeignKey("residents.Resident", on_delete=models.CASCADE, related_name="ak_entries")
    delta = models.IntegerField()  # krydser; negative allowed (debt), positive = credit
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.ADJUSTMENT)
    reason = models.CharField(max_length=255, blank=True)  # legacy `comment`
    # The assessment (year, month) a MONTHLY charge belongs to; null for labour/adjustment/opening.
    # This is the authoritative "which month" key (so re-running a month never double-charges), whereas
    # created_at is only the wall-clock time the row was written.
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    month = models.PositiveSmallIntegerField(null=True, blank=True)  # 1..12
    created_at = models.DateTimeField(default=timezone.now)
    # the officer who made the change; null for system/monthly/migrated entries
    created_by = models.ForeignKey(
        "residents.Resident",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ak_entries_created",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["resident", "created_at"])]
        constraints = [
            # At most one monthly charge per resident per period.
            models.UniqueConstraint(
                fields=["resident", "year", "month"],
                condition=Q(kind="monthly"),
                name="uniq_ak_monthly_per_period",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.resident.full_name}: {self.delta:+d} ({self.get_kind_display()})"

    @staticmethod
    def balance_for(resident: Resident) -> int:
        return AkEntry.objects.filter(resident=resident).aggregate(b=Sum("delta"))["b"] or 0


class AkMonthlyCharge(models.Model):
    """Per-calendar-month schedule for the AK monthly kryds deduction (F-009).

    AK officers set, for each month of the year (januar…december), how many krydser are subtracted and
    whether that month is charged at all. The amount is the same every year (keyed on `month`, not a
    specific period). When a month is over, its schedule is applied to that month's final alumneliste
    on the 1st of the next (see `ak.services.apply_monthly_charge`). There are always 12 rows (seeded
    1..12).
    """

    month = models.PositiveSmallIntegerField(unique=True)  # 1..12 (calendar month, any year)
    krydser = models.PositiveSmallIntegerField(default=2)  # amount subtracted (stored positive)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "residents.Resident",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ak_charges_updated",
    )

    class Meta:
        ordering = ["month"]

    def __str__(self) -> str:
        state = f"{self.krydser} krydser" if self.active else "slået fra"
        return f"AK måned {self.month:02d}: {state}"


class AkAutoApply(models.Model):
    """Singleton (pk=1) marker of the last (year, month) the monthly deduction was charged for.

    A scheduled task charges the month that just ended on the night of the 1st (DEPLOY.md §4b), and
    the same deduction is applied lazily on the first internal page load of a new month (see
    `ak.services.ensure_previous_month_charged`) as a backstop — cron fails silently, and a skipped
    month leaves every balance wrong. This row records which period has been handled, so whichever
    runs first wins, the hot path is a single cheap lookup, and the reconciliation itself happens
    exactly once.
    """

    year = models.PositiveSmallIntegerField(null=True, blank=True)
    month = models.PositiveSmallIntegerField(null=True, blank=True)

    def __str__(self) -> str:
        return f"AK auto-apply: {self.year}-{self.month}"

    @classmethod
    def get(cls) -> "AkAutoApply":
        return cls.objects.get_or_create(pk=1)[0]
