"""Ølkælder — beer-cellar POS (F-003).

Ølkælder keeps its own records (products, baskets and their items, shoppers, deposits, adjustments,
warnings, policies, its log); every kroner they stand for moved as a transfer in GAHK-konto (credit),
and every balance is read from there. A record is voided when its transfer is reversed. Money is
integer øre; line prices come from the product, never the client.
"""

from datetime import date

from django.db import models
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
        "residents.Account", on_delete=models.CASCADE, related_name="shopper_accounts"
    )
    active = models.BooleanField(default=True)
    # The shopper's money: their ølkælder account in GAHK-konto's Ølkælder book, held by their person.
    # Every balance is read from there (oelkaelder.ledger); nothing here sums money.
    credit_account = models.OneToOneField("credit.CreditAccount", on_delete=models.PROTECT, related_name="+")

    def __str__(self) -> str:
        return self.resident.full_name


class Deposit(models.Model):  # intern_oelkaelder_deposit
    """The record of money paid in: the ledger has the transfer (Ølkælder's bank → the shopper), this
    keeps it findable for the deposit report. Voided = its transfer reversed."""

    shopper = models.ForeignKey(Shopper, on_delete=models.CASCADE, related_name="deposits")
    amount_ore = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)
    # None only for a legacy deposit of 0 kr, which moved nothing.
    transfer = models.OneToOneField(
        "credit.Transfer", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )


class Transaction(models.Model):  # intern_oelkaelder_transaction
    """One basket from the till. What it cost each sharer is a transfer each (PurchaseShare); it is
    voided when every one of them is reversed."""

    created_at = models.DateTimeField(default=timezone.now)
    # The till's send: the same basket posted twice (a double tap) is one sale.
    dedupe_token = models.UUIDField(null=True, blank=True, unique=True)

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
    """One sharer's part of a basket and the transfer that paid it (their account → Salg)."""

    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="shares")
    shopper = models.ForeignKey(Shopper, on_delete=models.PROTECT, related_name="purchase_shares")
    share_ore = models.PositiveIntegerField()  # what this sharer paid; the shares sum to the total
    # None only for a share of 0 kr (a basket of fewer øre than sharers), which moved nothing.
    transfer = models.OneToOneField(
        "credit.Transfer", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Set when the sharer was not part of it: their transfer reversed, and this charge took the same
    # amount from the basket's executor instead.
    moved_transfer = models.OneToOneField(
        "credit.Transfer", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

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
    """The record of a correction to a shopper's balance: monthly interest (a charge into Renter) or a
    manual one (a charge into, or money from, Justeringer). Signed: negative = more debt. Voided =
    its transfer reversed."""

    class Kind(models.TextChoices):
        INTEREST = "interest", "Rente"
        MANUAL = "manual", "Manuel justering"

    shopper = models.ForeignKey(Shopper, on_delete=models.CASCADE, related_name="adjustments")
    amount_ore = models.IntegerField()  # signed; negative = charge (more debt)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.MANUAL)
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    # None only for a legacy adjustment of 0 kr, which moved nothing.
    transfer = models.OneToOneField(
        "credit.Transfer", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )


class InterestPolicy(models.Model):
    """Single-row config (pk=1) for the monthly debt interest ØK applies via the admin button."""

    active = models.BooleanField(default=False)
    rate_percent = models.DecimalField(max_digits=5, decimal_places=2, default=5)  # % of the debt
    threshold_ore = models.IntegerField(default=-10000)  # only debt below this is charged (-100 kr)

    @classmethod
    def get(cls) -> "InterestPolicy":
        return cls.objects.get_or_create(pk=1)[0]


class InterestExemption(models.Model):
    """An ølkælder account the monthly interest skips, from one day until another (or for good).
    Ølkælder's own rule, kept here: a GAHK-konto account knows nothing of interest."""

    credit_account = models.ForeignKey("credit.CreditAccount", on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=255)
    starts_on = models.DateField()
    ends_on = models.DateField(null=True, blank=True)  # the last day it applies; None = until ended
    created_by = models.ForeignKey(
        "residents.Account", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-starts_on", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_on__isnull=True) | models.Q(ends_on__gte=models.F("starts_on")),
                name="oelk_exemption_ends_after_it_starts",
            )
        ]

    def __str__(self) -> str:
        return f"Rentefritagelse: {self.credit_account}"

    def applies_on(self, day: date) -> bool:
        return self.starts_on <= day and (self.ends_on is None or day <= self.ends_on)
