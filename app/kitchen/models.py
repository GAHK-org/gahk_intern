"""Køkkenvagter — kitchen duty shifts and the køkkenkryds ledger (spec/features/kitchen-duty.md).

The point account is an append-only ledger like a bank statement: every row records the balance
before, the amount and the balance after. Rows are written only through `services.post_entry`, which
locks the resident's row so two concurrent postings cannot both read the same "before".
"""

from django.db import models
from django.db.models import Q, Sum

from core.clock import current_datetime


class KitchenShift(models.Model):
    class Kind(models.TextChoices):
        MORNING = "morning", "Morgenvagt"
        MIDDAY = "midday", "Middagsvagt"
        EVENING = "evening", "Aftensvagt"
        SPECIAL = "special", "Særlig vagt"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    date = models.DateField()
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    spots = models.PositiveSmallIntegerField(default=1)
    points_per_spot = models.PositiveSmallIntegerField()
    # Awarded per spot, on top of points_per_spot.
    bonus_points = models.PositiveSmallIntegerField(default=0)
    description = models.CharField(max_length=255, blank=True)
    is_disabled = models.BooleanField(default=False)
    disabled_reason = models.CharField(max_length=255, blank=True)
    # Set once the points for the shift have been booked; never booked twice.
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["starts_at"]
        indexes = [models.Index(fields=["starts_at"]), models.Index(fields=["ends_at", "completed_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["date", "kind"], condition=~Q(kind="special"), name="uniq_kitchen_standard_shift"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} {self.starts_at:%Y-%m-%d %H:%M}"

    @property
    def title(self) -> str:
        if self.kind == self.Kind.SPECIAL and self.description:
            return self.description
        return self.get_kind_display()

    @property
    def points_per_spot_total(self) -> int:
        return self.points_per_spot + self.bonus_points

    @property
    def has_started(self) -> bool:
        return self.starts_at <= current_datetime()

    @property
    def has_ended(self) -> bool:
        return self.ends_at <= current_datetime()

    def taken_spots(self) -> int:
        return sum(a.spots for a in self.assignments.all())

    def free_spots(self) -> int:
        return max(0, self.spots - self.taken_spots())


class KitchenAssignment(models.Model):
    """One resident on one shift, holding `spots` of its spots."""

    shift = models.ForeignKey(KitchenShift, on_delete=models.CASCADE, related_name="assignments")
    resident = models.ForeignKey(
        "residents.Resident", on_delete=models.CASCADE, related_name="kitchen_assignments"
    )
    spots = models.PositiveSmallIntegerField(default=1)
    for_sale = models.BooleanField(default=False)
    # Extra points (0..3) the seller pays the buyer on top of the shift itself.
    sale_bonus = models.PositiveSmallIntegerField(default=0)
    put_for_sale_at = models.DateTimeField(null=True, blank=True)
    # Set only when the resident signed up themselves; drives the short regret window.
    self_enrolled_at = models.DateTimeField(null=True, blank=True)
    awarded_points = models.IntegerField(default=0)
    is_absent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["shift__starts_at", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["shift", "resident"], name="uniq_kitchen_assignment"),
        ]

    def __str__(self) -> str:
        return f"{self.resident.full_name} × {self.spots} på {self.shift}"


class KitchenPointEntry(models.Model):
    class Kind(models.TextChoices):
        SHIFT = "shift", "Vagt"
        MONTHLY = "monthly", "Månedlig afregning"
        ABSENCE = "absence", "Udeblivelse"
        TRADE = "trade", "Vagtbytte"
        MANUAL = "manual", "Manuel postering"

    resident = models.ForeignKey(
        "residents.Resident", on_delete=models.CASCADE, related_name="kitchen_entries"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    balance_before = models.IntegerField()
    amount = models.IntegerField()
    balance_after = models.IntegerField()
    message = models.CharField(max_length=255)
    shift = models.ForeignKey(
        KitchenShift, null=True, blank=True, on_delete=models.SET_NULL, related_name="entries"
    )
    # The period a MONTHLY charge belongs to, so re-running a month never double-charges.
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    month = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["resident", "-id"])]
        constraints = [
            models.UniqueConstraint(
                fields=["resident", "year", "month"],
                condition=Q(kind="monthly"),
                name="uniq_kitchen_monthly_per_period",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.resident.full_name}: {self.amount:+d} ({self.message})"

    @staticmethod
    def balance_for(resident_id: int) -> int:
        last = KitchenPointEntry.objects.filter(resident_id=resident_id).order_by("-id").first()
        return last.balance_after if last else 0

    @staticmethod
    def balances() -> dict[int, int]:
        """Every resident's balance. The ledger's SUM equals the newest balance_after by construction."""
        return dict(
            KitchenPointEntry.objects.values("resident")
            .annotate(b=Sum("amount"))
            .values_list("resident", "b")
        )


class KitchenMonthlyState(models.Model):
    """Singleton (pk=1) marker of the last (year, month) the monthly deduction was applied."""

    year = models.PositiveSmallIntegerField(null=True, blank=True)
    month = models.PositiveSmallIntegerField(null=True, blank=True)

    @classmethod
    def get(cls) -> "KitchenMonthlyState":
        return cls.objects.get_or_create(pk=1)[0]
