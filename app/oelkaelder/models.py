"""Ølkælder — beer-cellar POS (F-003).

Fixes baked in: all money is **integer øre** (never float); a shopper's **balance is derived from the
ledger** (deposits - purchase shares of valid transactions), eliminating the non-atomic read-modify-write
`saldo` bug; line prices come from the product (server-side pricing, never the client); a purchase's
split is stored per shopper with **largest-remainder** rounding so shares sum exactly to the price; the
purchase write (transaction + items + shares) is wrapped in `transaction.atomic` at the view layer.
"""

from django.db import models
from django.db.models import Sum
from django.utils import timezone


class Product(models.Model):  # intern_oelkaelder_product
    name = models.CharField(max_length=255)
    price_ore = models.PositiveIntegerField()  # legacy current_price (normalized to øre)
    weight_price_ore = models.PositiveIntegerField(null=True, blank=True)
    price_steps = models.JSONField(
        null=True, blank=True
    )  # legacy price_steps text (no weight-only product today)
    image = models.FileField(upload_to="oel/", max_length=500, blank=True)  # legacy imageurl (can be long)
    active = models.BooleanField(default=True)
    highlighted = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.name

    def price_steps_ore(self) -> list[int]:
        """Normalize `price_steps` to a list of øre ints. Legacy stored it as a `"50;100;500;1000"`
        string; the JSONField may hold that string or an actual list."""
        raw = self.price_steps
        if not raw:
            return []
        parts = raw.replace(",", ";").split(";") if isinstance(raw, str) else list(raw)
        out: list[int] = []
        for p in parts:
            try:
                out.append(int(p))
            except (TypeError, ValueError):
                continue
        return out

    @property
    def pricing_mode(self) -> str:
        """`step` (betalingshop, 4 price quadrants) > `weight` (per-gram) > `fixed` (single price)."""
        if self.price_steps_ore():
            return "step"
        if self.weight_price_ore:
            return "weight"
        return "fixed"


class Shopper(models.Model):  # intern_shopper (+ intern_oelkaelder_saldo.active)
    resident = models.ForeignKey(
        "residents.Resident", on_delete=models.CASCADE, related_name="shopper_accounts"
    )
    active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.resident.full_name

    @property
    def balance_ore(self) -> int:
        """Derived balance = valid deposits - purchase shares + valid adjustments (e.g. interest).
        Negative = the shopper owes money."""
        deposits = self.deposits.filter(is_valid=True).aggregate(s=Sum("amount_ore"))["s"] or 0
        spent = (
            self.purchase_shares.filter(transaction__is_valid=True).aggregate(s=Sum("share_ore"))["s"] or 0
        )
        adjustments = self.adjustments.filter(is_valid=True).aggregate(s=Sum("amount_ore"))["s"] or 0
        return deposits - spent + adjustments


class Deposit(models.Model):  # intern_oelkaelder_deposit
    shopper = models.ForeignKey(Shopper, on_delete=models.CASCADE, related_name="deposits")
    amount_ore = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)
    is_valid = models.BooleanField(default=True)  # soft-delete (legacy `valid`)


class Transaction(models.Model):  # intern_oelkaelder_transaction
    created_at = models.DateTimeField(default=timezone.now)
    is_valid = models.BooleanField(default=True)

    class Meta:
        # The -id tiebreak is load-bearing, not cosmetic: legacy created_at is minute-resolution and the
        # till fires bursts, so timestamp ties are common. Postgres gives no stable order for tied sort
        # keys across separate LIMIT/OFFSET queries, which silently duplicates rows onto one paginated
        # page and drops them from another. The index matches the ordering so the sales list's date
        # filter and "ORDER BY … LIMIT" are an index range scan instead of a seq scan plus sort.
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["-created_at", "-id"], name="oelk_txn_created_id_idx")]


class TransactionItem(models.Model):  # intern_oelkaelder_transaction_item
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField(default=1)
    price_ore = models.PositiveIntegerField()  # server-computed line total (quantity × product price)


class PurchaseShare(models.Model):  # intern_oelkaelder_purchase (+ the per-shopper split amount, NEW)
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="shares")
    shopper = models.ForeignKey(Shopper, on_delete=models.PROTECT, related_name="purchase_shares")
    share_ore = models.PositiveIntegerField()  # largest-remainder split; shares sum to the transaction total

    class Meta:
        constraints = [models.UniqueConstraint(fields=["transaction", "shopper"], name="uniq_txn_shopper")]


class Warning(models.Model):  # intern_oelkaelder_warnings (debt-threshold warning emails)
    message = models.TextField(blank=True)
    threshold_ore = models.IntegerField()  # legacy amount; e.g. 10000 (=100 kr), 20000 (=200 kr)
    active = models.BooleanField(default=True)


class LogEntry(models.Model):  # intern_oelkaelder_log (audit trail)
    created_at = models.DateTimeField(default=timezone.now)
    message = models.TextField()

    class Meta:
        ordering = ["-created_at"]


class Adjustment(models.Model):
    """A signed correction to a shopper's balance (part of the derived ledger). Negative = a charge
    that increases their debt (e.g. monthly interest); positive = a credit. Soft-deletable."""

    class Kind(models.TextChoices):
        INTEREST = "interest", "Rente"
        MANUAL = "manual", "Manuel justering"

    shopper = models.ForeignKey(Shopper, on_delete=models.CASCADE, related_name="adjustments")
    amount_ore = models.IntegerField()  # signed; negative = charge (more debt)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.MANUAL)
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    is_valid = models.BooleanField(default=True)


class InterestPolicy(models.Model):
    """Single-row config (pk=1) for the monthly debt interest ØK applies via the admin button."""

    active = models.BooleanField(default=False)
    rate_percent = models.DecimalField(max_digits=5, decimal_places=2, default=5)  # % of the debt
    threshold_ore = models.IntegerField(default=-10000)  # only debt below this is charged (-100 kr)

    @classmethod
    def get(cls) -> "InterestPolicy":
        return cls.objects.get_or_create(pk=1)[0]


class PurchasePolicy(models.Model):
    """Single-row config (pk=1) for the ølkælder credit limit — how far into debt a shopper may go
    before the till refuses to sell to them.

    Deliberately separate from InterestPolicy. Both happen to default to -100 kr, and the till used
    to hardcode that same number, which is how "you owe enough to be charged interest" quietly
    became "you may not buy anything" — two different policies that ØK should be able to set apart.

    Off by default: the legacy till never blocked a purchase (Oelkaelder_model::addItem has no
    balance check), so switching it on is a decision for the ølkælder officers, not a silent
    inheritance from a hardcoded template condition.
    """

    active = models.BooleanField(default=False)
    # Balance strictly below this refuses the sale. Negative = the shopper owes money.
    block_below_ore = models.IntegerField(default=-10000)  # -100 kr

    class Meta:
        verbose_name = "Købsgrænse"
        verbose_name_plural = "Købsgrænser"

    def __str__(self) -> str:
        state = f"spærret under {self.block_below_ore / 100:.2f} kr" if self.active else "slået fra"
        return f"Købsgrænse: {state}"

    @classmethod
    def get(cls) -> "PurchasePolicy":
        return cls.objects.get_or_create(pk=1)[0]

    def blocks(self, balance_ore: int) -> bool:
        """Whether a shopper on this balance may not buy. Always False while the policy is off."""
        return self.active and balance_ore < self.block_below_ore
