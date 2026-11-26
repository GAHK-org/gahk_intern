"""Ølkælder's place in GAHK-konto: its book, the book's own accounts, and each shopper's account.

  Ølkælder (book)  held by the embedsgruppe Ølkælderen, whose members manage it; new accounts may
                   go down to -200 kr.
    bank           the real bank account behind it (made with the book): deposits come from here.
    Salg           takes what baskets cost.
    Renter         takes the monthly interest.
    Justeringer    takes, or gives, manual corrections.
    one account per shopper, held by the shopper's person.

Everything is found by name and made the first time it is asked for, so the till, the admin pages,
the history import and the demo seed all end up with the same book and accounts.
"""

from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, QuerySet, Subquery

from core.models import Workgroup
from credit import ledger
from credit.holders import holder_for_person, holder_for_workgroup
from credit.models import Book, CreditAccount, Posting
from residents.models import Account

from .models import Shopper

BOOK_NAME = "Ølkælder"
WORKGROUP_NAME = "Ølkælderen"
DEFAULT_CREDIT_LIMIT_ORE = -20000  # -200 kr
SALES = "Salg"
INTEREST = "Renter"
ADJUSTMENTS = "Justeringer"
SHOPPER_ACCOUNT_NAME = "Ølkælder"


@dataclass(frozen=True)
class Accounts:
    book: Book
    bank: CreditAccount
    sales: CreditAccount
    interest: CreditAccount
    adjustments: CreditAccount


def book() -> Book:
    """The Ølkælder book, made (by nobody: the system) the first time anything needs it."""
    found = Book.objects.filter(name=BOOK_NAME).select_related("holder").first()
    if found is not None:
        return found
    group, _ = Workgroup.objects.get_or_create(name=WORKGROUP_NAME)
    try:
        with transaction.atomic():
            return ledger.create_book(
                actor=None,
                name=BOOK_NAME,
                holder=holder_for_workgroup(group),
                bank_name="Bank",
                bank_reference=f"{settings.OELKAELDER_BANK_REG} {settings.OELKAELDER_BANK_ACCOUNT}",
                default_credit_limit_ore=DEFAULT_CREDIT_LIMIT_ORE,
            )
    except ledger.LedgerError:  # made meanwhile
        return Book.objects.select_related("holder").get(name=BOOK_NAME)


def accounts() -> Accounts:
    cellar = book()
    own = list(
        CreditAccount.objects.filter(
            book=cellar, holder_id=cellar.holder_id, counterpart_book__isnull=True
        ).order_by("pk")
    )
    bank = next(a for a in own if a.bank_reference)  # the book is made with one; deposits use the first
    named = {a.name: a for a in reversed(own) if not a.bank_reference}  # the oldest of a name wins
    return Accounts(
        book=cellar,
        bank=bank,
        sales=named.get(SALES) or _own_account(cellar, SALES),
        interest=named.get(INTEREST) or _own_account(cellar, INTEREST),
        adjustments=named.get(ADJUSTMENTS) or _own_account(cellar, ADJUSTMENTS),
    )


def _own_account(cellar: Book, name: str) -> CreditAccount:
    """One of the book's own accounts, made once: the book row is locked while it is looked for, so
    two requests at the same moment cannot both make it."""
    with transaction.atomic():
        Book.objects.select_for_update().get(pk=cellar.pk)
        found = CreditAccount.objects.filter(
            book=cellar,
            holder_id=cellar.holder_id,
            name=name,
            bank_reference="",
            counterpart_book__isnull=True,
        ).first()
        if found is not None:
            return found
        return ledger.open_account(actor=None, book=cellar, holder=cellar.holder, name=name, unlimited=True)


class NoPerson(ledger.LedgerError):
    """A login with no person behind it (a device, a service) cannot hold money."""


def open_shopper_account(resident: Account) -> CreditAccount:
    """A new ølkælder account for the person behind `resident`, with the book's default limit."""
    if resident.person_id is None:
        raise NoPerson(f"{resident.full_name} er ikke en person og kan ikke have en ølkælder-konto.")
    return ledger.open_account(
        actor=None, book=book(), holder=holder_for_person(resident.person), name=SHOPPER_ACCOUNT_NAME
    )


def open_shopper(resident: Account, *, active: bool = True) -> Shopper:
    """Make `resident` a shopper, with their ølkælder account. One shopper per login."""
    with transaction.atomic():
        existing = Shopper.objects.filter(resident=resident).first()
        if existing is not None:
            return existing
        try:
            with transaction.atomic():
                return Shopper.objects.create(
                    resident=resident, active=active, credit_account=open_shopper_account(resident)
                )
        except IntegrityError:  # pragma: no cover - a second request made it meanwhile
            return Shopper.objects.get(resident=resident)


def balances(account_ids: list[int]) -> dict[int, int]:
    """The balance of each account, in one query; 0 for one that never moved."""
    latest = Posting.objects.filter(account=OuterRef("pk")).order_by("-id").values("balance_after")[:1]
    rows = CreditAccount.objects.filter(pk__in=account_ids).annotate(balance=Subquery(latest))
    return {a.pk: int(a.balance or 0) for a in rows}


def with_balance(shoppers: QuerySet[Shopper]) -> QuerySet[Shopper]:
    """The shoppers with `balance` annotated from their account's latest posting (None = 0)."""
    latest = (
        Posting.objects.filter(account=OuterRef("credit_account")).order_by("-id").values("balance_after")[:1]
    )
    return shoppers.annotate(balance=Subquery(latest))


def annotated(shopper: Shopper) -> int:
    """The balance `with_balance` put on a shopper (0 for an account that never moved)."""
    return int(getattr(shopper, "balance", None) or 0)


def balance(shopper: Shopper) -> int:
    return ledger.balance_of(shopper.credit_account)
