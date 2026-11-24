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

## Closing the books

- Each book is closed every month, half year or year (its setting), one period after the other.
- A period can be closed once it has ended, by the book's managers, when every bank account matches
  the bank statement they type in and every mellemregning is settled, or the closing says why not.
- A closing keeps every account's balance and chain head at the end, and the book's signed anchor.
  Afterwards nothing can be recorded into the period: a posting belongs to the period it was
  recorded in, and a later correction goes into the open one.

## Statements

- On the 1st, every holder gets last month's statement by e-mail: one signed PDF of the accounts
  that moved or end the month with a balance. An embedsgruppe's goes to its current members; a
  person without a login gets none by mail, and their book's managers fetch it on the account page.
- Each account shows its opening and closing balance, the month's lines and two control numbers:
  this month's chain head and last month's, which must match last month's statement. The shared
  control number (the month anchor over every account) is the same on every statement that month,
  so any two statements can expose a rewritten history.
- Each statement is sent once; the account page offers the last twelve months as PDFs.

## Integrity

- The database refuses changing or deleting ledger rows, changing an account's book or holder,
  a posting that does not continue its account's balance and chain, and a transfer that is not
  exactly two postings summing to 0.
- Each account's postings form a SHA-256 hash chain; no secret key is involved, so nothing can be
  lost. A nightly check recomputes every chain, balance and book total and reports a fault in the
  log and the audit trail; it never corrects anything.
- Every transfer is also in the general audit trail.
- Anchors fix the ledger outside the database: every night, at every month's start and at every
  closing, one hash over the chain heads, signed and sent to a bucket with S3 Object Lock, or by
  e-mail to the treasurers where there is none. `manage.py credit_verify` checks everything against
  them, including anchor files kept elsewhere.
- Signing keys may be lost or replaced: every signature names its key, and the public half of every
  key is kept in the repository, so old signatures keep verifying.

## Decisions

- Two postings per transfer, not more: a balance is one lookup, every line has one counterparty
  like a bank statement, and a single interest charge or share can be reversed alone. Several
  payers and several receivers at once (pant, VAT) is the bookkeeping's job, not this ledger's.
- Books instead of a keeper field: settlement between keepers is a plain account balance.
- Credit is only spent inside its own book, so Ølkælder's credit never leaves Ølkælder.
- The hash chain is keyless and anchored outside the database instead of relying on a secret that
  could be lost.
- Anchors are signed with Ed25519; PDFs with an ECDSA certificate, since PDF readers understand
  that. The certificate is self-made for now (readers show an unknown signer); a bought one can
  replace it without other changes.
