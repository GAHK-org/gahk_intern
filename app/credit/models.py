"""GAHK-konto: an append-only ledger of accounts that stand for real kroner.

  Holder         who an account belongs to: a person or an embedsgruppe, never a login.
  Book           ("kontofører") one keeper's pool of real money: the accounts whose money sits in its
                 bank account(s), managed by the book's holder. Every book always sums to 0.
  CreditAccount  an account: in one book for good, held by one holder, with an optional credit limit.
  Transfer       one movement of money, written once, never changed; exactly two Postings.
  Posting        one account's side of a transfer: a signed amount, the balance after it, the text
                 shown on that account's statement, and a link in the account's hash chain.

The rules (credit.ledger writes every row; nothing else may):

  * Amounts are whole øre. + is money into an account, − out of it.
  * A transfer has exactly two postings, one − and one +, summing to 0 (a trigger checks it).
  * Nothing is changed or deleted (core.append_only, and the triggers in migration 0001). A mistake
    is undone by a reversing transfer.
  * A transfer between two books gets a companion between the books' mellemregning accounts, so each
    book still sums to 0 and the mellemregning shows what one book owes the other.
  * Each account's postings form a SHA-256 hash chain (credit.chain), so a changed or removed row
    shows when the chain is recomputed and compared with anchors kept outside the database.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.append_only import AppendOnly


class Holder(AppendOnly):
    """Who accounts belong to: exactly one person or one embedsgruppe. Made the first time one of
    theirs is opened, never changed or removed: a ledger row must always find its holder."""

    person = models.OneToOneField(
        "residents.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    workgroup = models.OneToOneField(
        "core.Workgroup", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(person__isnull=False) & Q(workgroup__isnull=True))
                | (Q(person__isnull=True) & Q(workgroup__isnull=False)),
                name="credit_holder_exactly_one",
            )
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def name(self) -> str:
        if self.person is not None:
            return self.person.full_name
        return self.workgroup.name if self.workgroup is not None else f"#{self.pk}"


class Book(models.Model):
    """A keeper's pool of real money ("kontofører"). Its holder's members manage it: they open its
    accounts, set their limits, charge them, record money in and out of its bank account(s), and
    settle with other books. Made deliberately by an administrator, never on its own."""

    name = models.CharField(max_length=100, unique=True)
    holder = models.ForeignKey(Holder, on_delete=models.PROTECT, related_name="books")
    # The limit an account opened in this book starts with; 0 = no credit. Its managers may change any
    # account's own limit afterwards.
    default_credit_limit_ore = models.BigIntegerField(default=0)
    # The book where every person with a login gets a general account. One at most.
    is_main = models.BooleanField(default=False)
    # How often its managers close it (credit.closing): every month, half year or year.
    closing_months = models.PositiveSmallIntegerField(
        default=1, choices=[(1, "Hver måned"), (6, "Hvert halve år"), (12, "Hvert år")]
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_main"], condition=Q(is_main=True), name="credit_one_main_book"
            ),
            models.CheckConstraint(
                condition=Q(default_credit_limit_ore__lte=0), name="credit_book_default_limit_not_positive"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class CreditAccount(models.Model):
    """A GAHK-konto. Generic on purpose: nothing about beer, interest or parties lives here, only
    what any account has. Its book and holder never change (a trigger refuses it); its name, limit
    and closing may."""

    book = models.ForeignKey(Book, on_delete=models.PROTECT, related_name="accounts")
    holder = models.ForeignKey(Holder, on_delete=models.PROTECT, related_name="accounts")
    name = models.CharField(max_length=100)
    # The lowest balance the account may reach paying inside its book; None = no limit (an account
    # standing for real money, a mellemregning). To another book it may only go down to 0.
    credit_limit_ore = models.BigIntegerField(null=True, blank=True, default=0)
    # Set on the account that stands for a real bank account: "reg.nr. kontonr.", unique.
    bank_reference = models.CharField(max_length=40, blank=True)
    # Set on a mellemregning: this book's account with that book.
    counterpart_book = models.ForeignKey(
        Book, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["book__name", "name", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(credit_limit_ore__isnull=True) | Q(credit_limit_ore__lte=0),
                name="credit_account_limit_not_positive",
            ),
            models.UniqueConstraint(
                fields=["bank_reference"], condition=~Q(bank_reference=""), name="credit_one_account_per_bank"
            ),
            models.UniqueConstraint(
                fields=["book", "counterpart_book"],
                condition=Q(counterpart_book__isnull=False),
                name="credit_one_mellemregning_per_pair",
            ),
            models.CheckConstraint(
                condition=~Q(counterpart_book=F("book")), name="credit_mellemregning_with_another_book"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.holder.name} · {self.name}"

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def is_mellemregning(self) -> bool:
        return self.counterpart_book_id is not None

    @property
    def stands_for_bank(self) -> bool:
        return bool(self.bank_reference)


class Transfer(AppendOnly):
    """One movement of money between two accounts, with exactly two postings."""

    class Kind(models.TextChoices):
        TRANSFER = "transfer", "Overførsel"  # a holder sends from their own account
        CHARGE = "charge", "Opkrævning"  # a book's managers take from an account in their book
        REVERSAL = "reversal", "Tilbageførsel"  # undoes `reverses`
        COMPANION = "companion", "Mellemregning"  # mirrors a transfer between two books

    kind = models.CharField(max_length=20, choices=Kind.choices)
    created_at = models.DateTimeField(default=timezone.now)
    # The login that made it; None for a job the system runs.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # What wrote it, for tracing ("credit.transfer_form", later "oelkaelder.basket:4711"). Never shown.
    source = models.CharField(max_length=80, blank=True)
    # The send's identity: the same token twice is the same transfer, made once.
    dedupe_token = models.UUIDField(unique=True)
    reverses = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )
    companion_of = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="companion"
    )

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind="reversal", reverses__isnull=False)
                | (~Q(kind="reversal") & Q(reverses__isnull=True)),
                name="credit_reversal_names_what_it_reverses",
            ),
            models.CheckConstraint(
                condition=Q(kind="companion", companion_of__isnull=False)
                | (~Q(kind="companion") & Q(companion_of__isnull=True)),
                name="credit_companion_names_its_transfer",
            ),
        ]

    def __str__(self) -> str:
        return f"Overførsel #{self.pk}"


class Posting(AppendOnly):
    """One account's side of a transfer."""

    transfer = models.ForeignKey(Transfer, on_delete=models.PROTECT, related_name="postings")
    account = models.ForeignKey(CreditAccount, on_delete=models.PROTECT, related_name="postings")
    amount_ore = models.BigIntegerField()
    balance_after = models.BigIntegerField()
    text = models.CharField(max_length=140)
    # The account's hash chain (credit.chain): the previous posting's row_hash (zeros for the first)
    # and this posting's own.
    prev_hash = models.BinaryField(max_length=32)
    row_hash = models.BinaryField(max_length=32)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(condition=~Q(amount_ore=0), name="credit_posting_not_zero"),
            models.UniqueConstraint(fields=["transfer", "account"], name="credit_one_side_per_account"),
        ]
        indexes = [models.Index(fields=["account", "-id"], name="credit_posting_account_id")]

    def __str__(self) -> str:
        return f"{self.account}: {self.amount_ore} øre"


class Anchor(AppendOnly):
    """A fixed point of the ledger kept outside the database: one hash over every account's chain head
    (or one book's, for a closing) as of a moment, signed, and delivered somewhere a rewrite of the
    database cannot reach (credit.anchors). Anchors also chain to each other."""

    class Kind(models.TextChoices):
        DAILY = "daily", "Dagligt"
        MONTH = "month", "Månedsskifte"  # what every monthly statement prints
        CLOSING = "closing", "Lukning"  # one book's, made when its period is closed

    kind = models.CharField(max_length=10, choices=Kind.choices)
    book = models.ForeignKey(Book, null=True, blank=True, on_delete=models.PROTECT, related_name="anchors")
    # The heads are each account's last posting from before this moment.
    as_of = models.DateTimeField()
    made_at = models.DateTimeField(default=timezone.now)
    root = models.BinaryField(max_length=32)
    accounts = models.PositiveIntegerField()
    postings = models.PositiveBigIntegerField()
    previous = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="next_anchor"
    )
    # SHA-256 of the previous anchor's anchor_hash and this anchor's fields (credit.anchors.message).
    anchor_hash = models.BinaryField(max_length=32)
    signature = models.BinaryField(max_length=128, blank=True)
    key_id = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "as_of"],
                condition=Q(book__isnull=True),
                name="credit_one_global_anchor_per_moment",
            ),
            models.UniqueConstraint(
                fields=["kind", "book", "as_of"],
                condition=Q(book__isnull=False),
                name="credit_one_book_anchor_per_moment",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.as_of:%Y-%m-%d %H:%M}"


class AnchorDelivery(AppendOnly):
    """Where an anchor was sent, and whether it got there."""

    class Way(models.TextChoices):
        S3 = "s3", "S3 med objektlås"
        MAIL = "mail", "E-mail"
        NONE = "none", "Ingen modtager sat op"

    anchor = models.ForeignKey(Anchor, on_delete=models.PROTECT, related_name="deliveries")
    way = models.CharField(max_length=10, choices=Way.choices)
    target = models.CharField(max_length=255, blank=True)
    ok = models.BooleanField()
    detail = models.TextField(blank=True)
    at = models.DateTimeField(default=timezone.now)


class Closing(AppendOnly):
    """A book's period, closed and signed off by its managers: the balance of every account at its
    end, the bank statements they match, and the book's anchor. Nothing can be recorded into a
    closed period afterwards (a trigger refuses it); a correction goes into the open one."""

    book = models.ForeignKey(Book, on_delete=models.PROTECT, related_name="closings")
    period_start = models.DateField()
    period_end = models.DateField()  # the period's last day
    ends_at = models.DateTimeField()  # midnight after period_end, local time: the first moment after it
    previous = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="next_closing"
    )
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    closed_at = models.DateTimeField(default=timezone.now)
    # Why something was not settled (a mellemregning in transit), written by whoever closed.
    note = models.TextField(blank=True)
    anchor = models.OneToOneField(Anchor, on_delete=models.PROTECT, related_name="closing")

    class Meta:
        ordering = ["-period_end"]
        constraints = [
            models.UniqueConstraint(fields=["book", "period_end"], name="credit_one_closing_per_period")
        ]

    def __str__(self) -> str:
        return f"{self.book.name}: {self.period_start} – {self.period_end}"


class ClosingBalance(AppendOnly):
    closing = models.ForeignKey(Closing, on_delete=models.PROTECT, related_name="balances")
    account = models.ForeignKey(CreditAccount, on_delete=models.PROTECT, related_name="+")
    balance = models.BigIntegerField()
    last_posting = models.ForeignKey(
        Posting, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    chain_head = models.BinaryField(max_length=32)
    # For an account standing for a bank: the bank statement's balance at the period's end, as typed.
    bank_statement_ore = models.BigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["closing", "account"], name="credit_one_balance_per_closing")
        ]


class Statement(AppendOnly):
    """A holder's monthly statement, sent once (the unique month is the send's identity)."""

    holder = models.ForeignKey(Holder, on_delete=models.PROTECT, related_name="statements")
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    anchor = models.ForeignKey(Anchor, on_delete=models.PROTECT, related_name="statements")
    recipients = models.TextField(blank=True)  # the addresses it went to, one per line
    pdf_sha256 = models.CharField(max_length=64)
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-year", "-month"]
        constraints = [
            models.UniqueConstraint(fields=["holder", "year", "month"], name="credit_one_statement_per_month")
        ]
