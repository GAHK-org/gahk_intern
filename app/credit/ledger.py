"""Writing the ledger. Every row of credit's tables is written here and nowhere else.

The money rules live here; who may ask for what is checked here too, so a view, a job or a later
feature (Ølkælder's till) gets the same answer. `actor=None` is the system itself (a job) and skips
the who-may check, never the money rules.

  send     a holder moves money from their own account. Inside the book it may use the account's
           credit, down to its limit; to another book only money the account has (down to 0).
  charge   a book's managers take money from an account in their book, into an account their book's
           holder holds (its bank, its own takings). Not stopped by the limit: the keeper decides.
  reverse  a book's managers undo a transfer into an account of their book. Not stopped by limits.
  record_history  a feature's history from before the ledger, dated when it happened (an import).

Each runs in one database transaction: the accounts it touches are locked in id order (two writes
can never wait on each other), their balances read under the lock, the transfer and its two postings
written with their hash-chain links, the companion between two books' mellemregning accounts added
when the money crosses books, the database's own checks forced on the spot, and the audit trail
written beside it. All of it commits, or none of it.
"""

import datetime
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from django.db import IntegrityError, connection, transaction
from django.db.models import OuterRef, QuerySet, Subquery
from django.utils import timezone

from audit import trail
from residents.models import Account

from . import chain, holders
from .models import Book, Closing, CreditAccount, Holder, Posting, Transfer

TEXT_LENGTH = 140
MAIN_ACCOUNT_NAME = "Generel"


class LedgerError(Exception):
    """A transfer or a change the rules refuse. The message is for the person who asked (Danish)."""


# ---- reading --------------------------------------------------------------------------------------


def balance_of(account: CreditAccount) -> int:
    latest = (
        Posting.objects.filter(account=account)
        .order_by("-id")
        .values_list("balance_after", flat=True)
        .first()
    )
    return latest or 0


def with_balances(accounts: QuerySet[CreditAccount]) -> QuerySet[CreditAccount]:
    """The accounts with `balance` annotated: their latest posting's balance_after (None = 0)."""
    latest = Posting.objects.filter(account=OuterRef("pk")).order_by("-id").values("balance_after")[:1]
    return accounts.annotate(balance=Subquery(latest))


def annotated_balance(account: CreditAccount) -> int:
    """The balance `with_balances` put on an account (0 for one with no postings)."""
    return int(getattr(account, "balance", None) or 0)


def book_total(book: Book) -> int:
    """What the book's balances add up to. Always 0; anything else is a fault (credit.checks)."""
    return sum(annotated_balance(a) for a in with_balances(CreditAccount.objects.filter(book=book)))


@dataclass(frozen=True)
class Line:
    """One line of an account's statement."""

    posting: Posting
    counterparty: CreditAccount

    @property
    def transfer(self) -> Transfer:
        return self.posting.transfer


def statement(account: CreditAccount, *, limit: int | None = None) -> list[Line]:
    """The account's postings, newest first, each with the account on the other side."""
    postings = list(
        Posting.objects.filter(account=account)
        .select_related("transfer", "transfer__reversed_by")
        .order_by("-id")[: limit or None]
    )
    others = {
        p.transfer_id: p
        for p in Posting.objects.filter(transfer_id__in=[p.transfer_id for p in postings])
        .exclude(account=account)
        .select_related("account__holder__person", "account__holder__workgroup")
    }
    return [Line(p, others[p.transfer_id].account) for p in postings if p.transfer_id in others]


# ---- the rule on credit ---------------------------------------------------------------------------


def lowest_allowed(sender: CreditAccount, receiver: CreditAccount) -> int | None:
    """How low the sender may go for this transfer; None = no floor. Inside its book an account may
    use the credit the book gives it; out of the book, only money it actually has."""
    if sender.credit_limit_ore is None:
        return None
    if sender.book_id == receiver.book_id:
        return sender.credit_limit_ore
    return 0


# ---- opening and changing accounts ----------------------------------------------------------------


def open_account(
    *,
    actor: Account | None,
    book: Book,
    holder: Holder,
    name: str,
    credit_limit_ore: int | None = None,
    unlimited: bool = False,
) -> CreditAccount:
    """A new account in `book`. Its managers open them; `actor=None` is the system. `unlimited`: one
    of the book's own accounts that stands for the keeper's money (its takings, the interest it
    charges), so like the bank it has no limit; only the book's holder may hold one."""
    if actor is not None and not holders.manages(actor, book):
        raise LedgerError("Kun kontoførerens ansvarlige kan oprette konti her.")
    limit: int | None
    if unlimited:
        if holder.pk != book.holder_id:
            raise LedgerError("Kun kontoførerens egne konti kan være uden kreditgrænse.")
        limit = None
    else:
        limit = book.default_credit_limit_ore if credit_limit_ore is None else credit_limit_ore
        if limit > 0:
            raise LedgerError("En kreditgrænse er 0 eller negativ.")
    with trail.event("credit.account.opened", actor=actor):
        return CreditAccount.objects.create(
            book=book, holder=holder, name=name.strip(), credit_limit_ore=limit
        )


def main_account(person: object) -> CreditAccount | None:
    """The person's general account in the main book, opened the first time it is asked for. None
    while there is no main book."""
    book = Book.objects.filter(is_main=True).first()
    if book is None:
        return None
    holder = holders.holder_for_person(person)
    existing = CreditAccount.objects.filter(book=book, holder=holder, counterpart_book__isnull=True).first()
    if existing is not None:
        return existing
    return open_account(actor=None, book=book, holder=holder, name=MAIN_ACCOUNT_NAME)


def set_credit_limit(*, actor: Account, account: CreditAccount, credit_limit_ore: int) -> None:
    if not holders.manages(actor, account.book):
        raise LedgerError("Kun kontoførerens ansvarlige kan ændre kreditgrænsen.")
    if account.credit_limit_ore is None or account.is_mellemregning:
        raise LedgerError("Denne konto har ingen kreditgrænse.")
    if credit_limit_ore > 0:
        raise LedgerError("En kreditgrænse er 0 eller negativ.")
    with trail.event("credit.account.limit_changed", subject=account):
        account.credit_limit_ore = credit_limit_ore
        account.save(update_fields=["credit_limit_ore"])


def close_account(*, actor: Account, account: CreditAccount) -> None:
    """Only at balance 0, by its holder or its book's managers. Nothing is ever deleted."""
    if not (holders.acts_for(actor, account.holder) or holders.manages(actor, account.book)):
        raise LedgerError("Du kan ikke lukke denne konto.")
    with transaction.atomic():
        account = CreditAccount.objects.select_for_update().get(pk=account.pk)
        if account.closed_at is not None:
            return
        if balance_of(account) != 0:
            raise LedgerError("En konto kan kun lukkes, når saldoen er 0,00 kr.")
        with trail.event("credit.account.closed", subject=account):
            account.closed_at = timezone.now()
            account.save(update_fields=["closed_at"])


def create_book(
    *,
    actor: Account | None,
    name: str,
    holder: Holder,
    bank_name: str,
    bank_reference: str,
    default_credit_limit_ore: int = 0,
    is_main: bool = False,
) -> Book:
    """A new book with the account standing for its bank account. Administrators only (the view
    checks the permission); a book is never made on its own."""
    if default_credit_limit_ore > 0:
        raise LedgerError("En kreditgrænse er 0 eller negativ.")
    reference = " ".join(bank_reference.split())
    if not reference:
        raise LedgerError("Skriv bankkontoens reg.nr. og kontonr.")
    with trail.event("credit.book.created", actor=actor):
        try:
            with transaction.atomic():
                book = Book.objects.create(
                    name=name.strip(),
                    holder=holder,
                    default_credit_limit_ore=default_credit_limit_ore,
                    is_main=is_main,
                )
                CreditAccount.objects.create(
                    book=book,
                    holder=holder,
                    name=bank_name.strip() or "Bank",
                    credit_limit_ore=None,
                    bank_reference=reference,
                )
        except IntegrityError as exc:
            raise LedgerError("Navnet, hovedbogen eller bankkontoen er allerede i brug.") from exc
    return book


def add_bank_account(*, actor: Account | None, book: Book, name: str, bank_reference: str) -> CreditAccount:
    reference = " ".join(bank_reference.split())
    if not reference:
        raise LedgerError("Skriv bankkontoens reg.nr. og kontonr.")
    with trail.event("credit.account.opened", actor=actor):
        try:
            with transaction.atomic():
                return CreditAccount.objects.create(
                    book=book,
                    holder=book.holder,
                    name=name.strip() or "Bank",
                    credit_limit_ore=None,
                    bank_reference=reference,
                )
        except IntegrityError as exc:
            raise LedgerError("Bankkontoen findes allerede.") from exc


def _mellemregning(book: Book, counterpart: Book) -> CreditAccount:
    """`book`'s account with `counterpart`, made the first time the two exchange money."""
    found = CreditAccount.objects.filter(book=book, counterpart_book=counterpart).first()
    if found is not None:
        return found
    try:
        with trail.event("credit.account.opened", actor=None), transaction.atomic():
            return CreditAccount.objects.create(
                book=book,
                holder=book.holder,
                name=f"Mellemregning med {counterpart.name}",
                credit_limit_ore=None,
                counterpart_book=counterpart,
            )
    except IntegrityError:  # made meanwhile by another transfer between the two
        return CreditAccount.objects.get(book=book, counterpart_book=counterpart)


# ---- moving money ---------------------------------------------------------------------------------


def send(
    *,
    actor: Account | None,
    sender: CreditAccount,
    receiver: CreditAccount,
    amount_ore: int,
    sender_text: str = "",
    receiver_text: str = "",
    dedupe_token: uuid.UUID,
    source: str = "",
) -> Transfer:
    """Money from an account the actor acts for, to any open account."""
    if actor is not None and not holders.acts_for(actor, sender.holder):
        raise LedgerError("Du kan kun overføre fra dine egne konti.")
    if (sender.is_mellemregning or receiver.is_mellemregning) and sender.book_id != receiver.book_id:
        raise LedgerError("En mellemregning bevæger sig kun inden for sin egen kontofører.")
    return _move(
        kind=Transfer.Kind.TRANSFER,
        sender=sender,
        receiver=receiver,
        amount_ore=amount_ore,
        sender_text=sender_text or f"Til {receiver.holder.name}",
        receiver_text=receiver_text or f"Fra {sender.holder.name}",
        actor=actor,
        dedupe_token=dedupe_token,
        source=source,
        limited=True,
    )


def charge(
    *,
    actor: Account | None,
    account: CreditAccount,
    into: CreditAccount,
    amount_ore: int,
    text: str,
    into_text: str = "",
    dedupe_token: uuid.UUID,
    source: str = "",
) -> Transfer:
    """A book's managers take money from an account in their book, into one of the book's own."""
    if into.book_id != account.book_id or into.holder_id != account.book.holder_id:
        raise LedgerError("Der kan kun opkræves til kontoførerens egne konti i samme bog.")
    if actor is not None and not holders.manages(actor, account.book):
        raise LedgerError("Kun kontoførerens ansvarlige kan opkræve.")
    return _move(
        kind=Transfer.Kind.CHARGE,
        sender=account,
        receiver=into,
        amount_ore=amount_ore,
        sender_text=text,
        receiver_text=into_text or f"Fra {account.holder.name} · {text}",
        actor=actor,
        dedupe_token=dedupe_token,
        source=source,
        limited=False,
    )


def reverse(
    *, actor: Account | None, transfer: Transfer, dedupe_token: uuid.UUID, source: str = ""
) -> Transfer:
    """Undo a transfer or a charge: the same amount back. The managers of the book the money went
    into decide. A reversal, a companion and an already reversed transfer cannot be undone."""
    if transfer.kind not in (Transfer.Kind.TRANSFER, Transfer.Kind.CHARGE):
        raise LedgerError("Kun overførsler og opkrævninger kan tilbageføres.")
    if Transfer.objects.filter(reverses=transfer).exists():
        raise LedgerError("Overførslen er allerede tilbageført.")
    out, back = _sides(transfer)
    if actor is not None and not holders.manages(actor, back.account.book):
        raise LedgerError("Kun de ansvarlige for kontoen, pengene gik til, kan tilbageføre.")
    return _move(
        kind=Transfer.Kind.REVERSAL,
        sender=back.account,
        receiver=out.account,
        amount_ore=back.amount_ore,
        sender_text=f"Tilbageført: {back.text}",
        receiver_text=f"Tilbageført: {out.text}",
        actor=actor,
        dedupe_token=dedupe_token,
        source=source,
        limited=False,
        reverses=transfer,
    )


def record_history(
    *,
    kind: str,
    sender: CreditAccount,
    receiver: CreditAccount,
    amount_ore: int,
    sender_text: str,
    receiver_text: str,
    at: datetime.datetime,
    dedupe_token: uuid.UUID,
    source: str,
    reverses: Transfer | None = None,
) -> Transfer:
    """A feature's own history from before it kept its money here, written as it happened: dated
    `at`, by nobody, never stopped by a limit (history is history). Only for importing that history
    (oelkaelder.history), in order, oldest first, so ids still follow the dates. Never into a book
    that has closed a period: a closing has fixed what stood before it."""
    if kind not in (Transfer.Kind.TRANSFER, Transfer.Kind.CHARGE, Transfer.Kind.REVERSAL):
        raise LedgerError("Kun overførsler, opkrævninger og tilbageførsler kan indlæses.")
    if (kind == Transfer.Kind.REVERSAL) != (reverses is not None):
        raise LedgerError("En tilbageførsel skal nævne, hvad den tilbagefører.")
    if sender.book_id != receiver.book_id:
        raise LedgerError("Historik indlæses kun inden for én kontofører.")
    if Closing.objects.filter(book_id=sender.book_id).exists():
        raise LedgerError(
            f"Kontoføreren {sender.book} har lukket en periode; historik kan ikke længere indlæses i den."
        )
    return _move(
        kind=kind,
        sender=sender,
        receiver=receiver,
        amount_ore=amount_ore,
        sender_text=sender_text,
        receiver_text=receiver_text,
        actor=None,
        dedupe_token=dedupe_token,
        source=source,
        limited=False,
        reverses=reverses,
        at=at,
    )


def _sides(transfer: Transfer) -> tuple[Posting, Posting]:
    """(the side money left, the side it went to)."""
    postings = sorted(transfer.postings.select_related("account__book"), key=lambda p: p.amount_ore)
    return postings[0], postings[1]


_DEFERRED_CHECKS = "credit_transfer_balanced, credit_posting_balanced"


def _check_now() -> None:
    """Run the database's balance checks on this transaction's postings now, rather than at commit,
    so a fault surfaces here (and inside the test suite, whose transactions never commit)."""
    with connection.cursor() as cursor:
        cursor.execute(f"SET CONSTRAINTS {_DEFERRED_CHECKS} IMMEDIATE")
        cursor.execute(f"SET CONSTRAINTS {_DEFERRED_CHECKS} DEFERRED")


def _heads(account_ids: Iterable[int]) -> dict[int, tuple[int, bytes]]:
    """Each account's balance and chain head, read under its lock."""
    out = {}
    for pk in account_ids:
        latest = (
            Posting.objects.filter(account_id=pk)
            .order_by("-id")
            .values_list("balance_after", "row_hash")
            .first()
        )
        out[pk] = (latest[0], bytes(latest[1])) if latest else (0, chain.GENESIS)
    return out


def _move(
    *,
    kind: str,
    sender: CreditAccount,
    receiver: CreditAccount,
    amount_ore: int,
    sender_text: str,
    receiver_text: str,
    actor: Account | None,
    dedupe_token: uuid.UUID,
    source: str,
    limited: bool,
    reverses: Transfer | None = None,
    at: datetime.datetime | None = None,
) -> Transfer:
    """`at`: when the transfer happened, for record_history alone; everything else is dated now."""
    if amount_ore <= 0:
        raise LedgerError("Beløbet skal være større end 0.")
    if sender.pk == receiver.pk:
        raise LedgerError("Afsender og modtager er den samme konto.")
    dedupe_token = uuid.UUID(str(dedupe_token))
    with transaction.atomic():
        done = Transfer.objects.filter(dedupe_token=dedupe_token).first()
        if done is not None:  # this send was made already: the same transfer, once
            return done
        crossing = sender.book_id != receiver.book_id
        ids = {sender.pk, receiver.pk}
        if crossing:
            out_book, in_book = Book.objects.get(pk=sender.book_id), Book.objects.get(pk=receiver.book_id)
            owes, owed = _mellemregning(out_book, in_book), _mellemregning(in_book, out_book)
            ids |= {owes.pk, owed.pk}
        locked = {
            a.pk: a for a in CreditAccount.objects.select_for_update().filter(pk__in=ids).order_by("pk")
        }
        sender, receiver = locked[sender.pk], locked[receiver.pk]
        for account in (sender, receiver):
            if account.closed_at is not None:
                raise LedgerError(f"Kontoen {account} er lukket.")
        heads = _heads(sorted(ids))
        if limited:
            floor = lowest_allowed(sender, receiver)
            if floor is not None and heads[sender.pk][0] - amount_ore < floor:
                raise LedgerError(_refusal(floor, heads[sender.pk][0]))
        action = {
            Transfer.Kind.TRANSFER.value: "credit.transfer.sent",
            Transfer.Kind.CHARGE.value: "credit.transfer.charged",
            Transfer.Kind.REVERSAL.value: "credit.transfer.reversed",
        }[str(kind)]
        with trail.event(action, actor=actor, involve=[(sender, "sender"), (receiver, "receiver")]) as event:
            try:
                with transaction.atomic():
                    # Dated by the field's default unless history says when (record_history).
                    dated = {"created_at": at} if at is not None else {}
                    made = Transfer(
                        **dated,
                        kind=kind,
                        created_by=actor,
                        source=source[:80],
                        dedupe_token=dedupe_token,
                        reverses=reverses,
                    )
                    made.save()
            except IntegrityError:  # the same token, sent at the same moment by a second press
                event.cancel()
                return Transfer.objects.get(dedupe_token=dedupe_token)
            event.involve(made, "subject")
            _post(made, sender, receiver, amount_ore, sender_text, receiver_text, heads)
            if crossing:
                companion = Transfer(
                    **dated,
                    kind=Transfer.Kind.COMPANION,
                    created_by=actor,
                    source=source[:80],
                    dedupe_token=uuid.uuid5(dedupe_token, "companion"),
                    companion_of=made,
                )
                companion.save()
                text = f"{sender} → {receiver} (#{made.pk})"
                _post(companion, locked[owed.pk], locked[owes.pk], amount_ore, text, text, heads)
            _check_now()
    return made


def _refusal(floor: int, balance: int) -> str:
    from .templatetags.credit_extras import kr

    if floor == 0:
        return f"Der er kun {kr(max(balance, 0))} på kontoen, og der kan ikke bruges kredit til en anden kontofører."
    return f"Overførslen ville bringe saldoen under kreditgrænsen på {kr(floor)} (saldo nu {kr(balance)})."


def _post(
    made: Transfer,
    sender: CreditAccount,
    receiver: CreditAccount,
    amount_ore: int,
    sender_text: str,
    receiver_text: str,
    heads: dict[int, tuple[int, bytes]],
) -> None:
    for account, other, signed, text in (
        (sender, receiver, -amount_ore, sender_text),
        (receiver, sender, amount_ore, receiver_text),
    ):
        balance, prev = heads[account.pk]
        after = balance + signed
        clean = " ".join(text.split())[:TEXT_LENGTH] or "-"
        encoded = chain.encode(
            transfer_id=made.pk,
            kind=made.kind,
            created_at=made.created_at,
            created_by_id=made.created_by_id,
            source=made.source,
            dedupe_token=str(made.dedupe_token),
            reverses_id=made.reverses_id,
            companion_of_id=made.companion_of_id,
            account_id=account.pk,
            counterparty_id=other.pk,
            amount_ore=signed,
            balance_after=after,
            text=clean,
        )
        row_hash = chain.link(prev, encoded)
        Posting(
            transfer=made,
            account=account,
            amount_ore=signed,
            balance_after=after,
            text=clean,
            prev_hash=prev,
            row_hash=row_hash,
        ).save()
        heads[account.pk] = (after, row_hash)


def posting_hash(posting: Posting, counterparty_id: int) -> bytes:
    """What a stored posting's row_hash should be, recomputed from its row and its transfer."""
    made = posting.transfer
    encoded = chain.encode(
        transfer_id=made.pk,
        kind=made.kind,
        created_at=made.created_at,
        created_by_id=made.created_by_id,
        source=made.source,
        dedupe_token=str(made.dedupe_token),
        reverses_id=made.reverses_id,
        companion_of_id=made.companion_of_id,
        account_id=posting.account_id,
        counterparty_id=counterparty_id,
        amount_ore=posting.amount_ore,
        balance_after=posting.balance_after,
        text=posting.text,
    )
    return chain.link(bytes(posting.prev_hash), encoded)
