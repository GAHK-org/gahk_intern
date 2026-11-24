"""Who may act for a holder: the one place the ledger asks.

  * a person: their login;
  * an embedsgruppe: its current members, from the alumneliste in effect, and anyone holding the
    group's role by a grant (access.roles), so a role given by hand counts like a month on the list.

A book's managers are its holder's: the ølkælder embedsgruppe manages Ølkælder's book.
"""

from django.db import IntegrityError, transaction
from django.db.models import QuerySet

from access.api import holding
from audit import trail
from residents.models import WORKGROUP_ROLE, Account, Residency, active_period

from .models import Book, CreditAccount, Holder


def holder_for_person(person: object) -> Holder:
    return _holder(person=person)


def holder_for_workgroup(workgroup: object) -> Holder:
    return _holder(workgroup=workgroup)


def _holder(**who: object) -> Holder:
    """The holder for a person or an embedsgruppe, made the first time it is needed."""
    found = Holder.objects.filter(**who).first()
    if found is not None:
        return found
    try:
        with trail.event("credit.holder.created", actor=None), transaction.atomic():
            return Holder.objects.create(**who)
    except IntegrityError:  # made meanwhile
        return Holder.objects.get(**who)


def members(holder: Holder) -> QuerySet[Account]:
    """The active logins that may act for `holder`."""
    if holder.person_id is not None:
        return Account.objects.filter(person_id=holder.person_id, is_active=True)
    if holder.workgroup is None:
        return Account.objects.none()
    year, month = active_period()
    listed = Residency.objects.filter(year=year, month=month, workgroup=holder.workgroup).values(
        "resident__person__account"
    )
    ids = set(Account.objects.filter(pk__in=listed, is_active=True).values_list("pk", flat=True))
    role = WORKGROUP_ROLE.get(holder.workgroup.name)  # the embedsgrupper that also give a site role
    if role is not None:
        ids |= set(holding([role]).values_list("pk", flat=True))
    return Account.objects.filter(pk__in=ids)


def acts_for(account: Account, holder: Holder) -> bool:
    if not account.is_authenticated:
        return False
    if holder.person_id is not None:
        return account.person_id == holder.person_id
    return members(holder).filter(pk=account.pk).exists()


def holders_of(account: Account) -> list[Holder]:
    """Every holder `account` acts for: its person, and the embedsgrupper it is a member of."""
    if not account.is_authenticated:
        return []
    out: list[Holder] = []
    if account.person_id is not None:
        out.extend(Holder.objects.filter(person_id=account.person_id))
    for holder in Holder.objects.filter(workgroup__isnull=False).select_related("workgroup"):
        if acts_for(account, holder):
            out.append(holder)
    return out


def manages(account: Account, book: Book) -> bool:
    return acts_for(account, book.holder)


def managed_books(account: Account) -> list[Book]:
    holders = {h.pk for h in holders_of(account)}
    return [b for b in Book.objects.select_related("holder") if b.holder_id in holders]


def may_see(account: Account, credit_account: CreditAccount) -> bool:
    """A holder sees their own accounts; a book's managers see every account in their book."""
    return acts_for(account, credit_account.holder) or manages(account, credit_account.book)
