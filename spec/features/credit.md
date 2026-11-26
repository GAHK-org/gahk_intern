# GAHK-konto

Accounts that stand for real kroner: what people and embedsgrupper owe the house and its keepers, or
have to their credit. Not a bank account, and not the books (e-conomic stays the bookkeeping); a
ledger of claims that can never be changed after the fact.

## What there is

- **Kontohaver** (holder): a person or an embedsgruppe. A login is never a holder; it acts for one.
- **Kontofører** (book): one keeper's pool of real money, e.g. GAHK (Regnskabsgruppen) and Ølkælder
  (Ølkælderen), each with its own real bank account. Every account is in exactly one book, for good.
- **GAHK-konto** (account): in one book, held by one holder, with a credit limit (0 or negative).
  A person may have several (a general one, an ølkælder one). Accounts are generic: interest,
  exemptions or party rules belong to the feature that charges, never to the account.
- **Bank accounts** stand for the real bank account behind a book: no limit, and a negative balance
  is money in the bank.
- **Overførsel** (transfer): one movement between two accounts, with a text for each side.

## Rules

- Amounts are whole øre; + is money into an account, − out of it. Every book always sums to 0.
- Nothing is ever changed or deleted: a mistake is undone by a reversing transfer. Accounts are
  closed (only at balance 0), never deleted.
- A transfer is what stands or falls together. Several accounts at once (a shared basket, a monthly
  interest run) are several transfers, written all or nothing.
- **Send**: a holder sends from their own account to any open account. Inside the account's book it
  may use the account's credit down to its limit; to another book only money it actually has.
- **Charge**: a book's managers take money from any account in their book, into an account the book
  itself holds. Not stopped by the limit: the keeper carries the risk and decides.
- **Reverse**: the managers of the book the money went into undo a transfer or a charge, once.
- Money in and out of a book is a transfer from or to its bank account, recorded by its managers.
- A transfer between two books gets a companion between the books' mellemregning accounts, so each
  book still sums to 0 and the mellemregning shows what one book owes the other. Settling is each
  book recording its own side (mellemregning ↔ bank) once the real money has moved; a pair that
  does not mirror is a settlement half recorded.
- The same send twice (a double press, a retry) is one transfer: each form carries its own token.

## Who sees and does what

- Everyone with a login gets a general account in the main book the first time they open the page.
- A holder sees their own accounts and statements; an embedsgruppe's current members see and send
  from the group's accounts.
- A book's managers (its holder's members) see every account in their book, but not the same
  person's accounts in other books; they open accounts, set limits, charge, reverse and settle.
- Administrators make books (name, managing embedsgruppe, bank account, default limit).
- The page stays open after moving out: someone who left may still owe or be owed.

## Integrity

- The database refuses changing or deleting ledger rows, changing an account's book or holder,
  a posting that does not continue its account's balance and chain, and a transfer that is not
  exactly two postings summing to 0.
- Each account's postings form a SHA-256 hash chain; no secret key is involved, so nothing can be
  lost. A nightly check recomputes every chain, balance and book total and reports a fault in the
  log and the audit trail; it never corrects anything.
- Every transfer is also in the general audit trail.

## Decisions

- Two postings per transfer, not more: a balance is one lookup, every line has one counterparty
  like a bank statement, and a single interest charge or share can be reversed alone. Several
  payers and several receivers at once (pant, VAT) is the bookkeeping's job, not this ledger's.
- Books instead of a keeper field: settlement between keepers is a plain account balance.
- Credit is only spent inside its own book, so Ølkælder's credit never leaves Ølkælder.
- The hash chain is keyless and anchored outside the database instead of relying on a secret that
  could be lost.
