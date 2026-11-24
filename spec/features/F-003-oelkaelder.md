# Ølkælder

The dorm's beer cellar, run on a tab: a self-service till where residents buy, alone or sharing a
basket, and pay in money by bank transfer. Ølkælderen (the embedsgruppe) runs it. Its money lives in
GAHK-konto (see `credit.md`), in the book "Ølkælder"; Ølkælder keeps its own records beside it.

## What there is

- **Products**: name, price (or betalingshop steps, or a price per 100 g), on or off the till,
  highlighted, an image.
- **Shoppers**: residents who may buy, active or not. Each has one ølkælder account in the Ølkælder
  book, held by their person, opened with the book's credit limit (-200 kr).
- **The Ølkælder book**: managed by Ølkælderen's members, with its own accounts beside the shoppers':
  the bank account behind it, **Salg** (sales), **Renter** (interest) and **Justeringer** (corrections).
- **Records**: each basket with its items and each sharer's share, each deposit, each correction and
  interest charge. Each record is linked to the transfer that moved its money; a record is voided
  when that transfer is reversed. Every balance is read from GAHK-konto, never summed from records.
- Debt-warning mails (two, each with a threshold), the interest policy (rate, threshold, on/off),
  interest exemptions, and a log.

## The till

- Runs on the house's network only, without a login, on an old iPad (no modern scripts).
- Tap products into a basket, then pick a buyer, or several to share ("fordel på"); a 5-second
  countdown, then the sale is posted. Prices are always taken from the product, never the till.
- A basket is one record and one transfer per sharer (their account → Salg), all or nothing. Each
  pays total ÷ n rounded down; the **executor**, the one picked first, also pays the remainder.
- The account's credit limit decides, checked on the balance **after** the purchase. If any sharer
  would go below their limit the whole basket is refused, and the till says who and why.
- A tile shows each shopper's balance, and "Spærret" when they are at their limit.
- The same basket posted twice (a double tap) is one sale.

## Money in and corrections

- **Deposit**: a manager records money paid into Ølkælder's bank: a transfer from the bank account to
  the shopper ("Indbetaling"). Voiding it reverses it.
- **Correction**: a manager adds or subtracts an amount with a written reason, which the resident
  sees. Subtracting is a charge into Justeringer; adding is money from Justeringer. Voiding reverses.
- **Interest**: on the button, once a month, each active account whose debt is past the threshold is
  charged the rate of its debt, one charge per account into Renter. An account is charged at most
  once a month; a wrong charge is reversed alone and not charged again that month.
- **Exemptions**: managers exempt an account from interest from one day, until a day or until ended
  (with a reason). The exemption is Ølkælder's own record, never part of the account.
- Charges and reversals are never stopped by the limit: the keeper decides.

## Sales

- **Void a sale**: every transfer of the basket is reversed at once.
- **"X var ikke med"**: a sharer who was not part of it gets their share reversed, and the executor
  (who picked them) is charged the same amount instead ("Køb #N: Xs andel flyttet til dig").
- A debt-warning mail goes out when a purchase takes a balance down through a warning's threshold.

## Who sees and does what

- Every resident with a shopper sees "Min ølkælder-saldo": the balance and the account's lines from
  GAHK-konto, with a link to the account there and how to pay in.
- Ølkælderen manages: shoppers (add, deactivate, reactivate), deposits, corrections, interest and its
  exemptions, warning mails, products and prices, the sales overview, the per-person history and
  three reports (deposits with balances, sales, quantities), each on screen or as CSV/Excel.
- Credit limits are set per account on GAHK-konto's page for the Ølkælder book, by its managers.
- Moving money takes being one of the book's managers; an administrator may look but not move.
- Monthly statements are GAHK-konto's, which cover the ølkælder accounts.

## History

- Everything from before GAHK-konto was imported once as ledger history, oldest first, each dated
  when it happened: deposits, one transfer per stored share, interest and corrections; a voided record
  followed by its reversal. Every balance afterwards is what the old records added up to.
- The import never writes into a closed period or under an anchor already made, and may be run again
  without writing anything twice.
- Old baskets whose buyers were never migrated keep their items but moved no money.

## Decisions

- Money lives only in GAHK-konto: one ledger, one balance, append-only, so no derived sums can drift
  and every change is a reversible transfer.
- One transfer per sharer, not one per basket: each sharer's part can be reversed or moved alone.
- The executor pays the remainder: whole øre, and the one who rang it up carries the odd øre.
- The account's limit replaced the separate purchase-limit setting: one rule, enforced for the till
  and every other way in. It is checked after the purchase, so a sale may land exactly on it.
- Interest and its exemptions are Ølkælder's: GAHK-konto accounts know nothing of beer or interest.
- Ølkælder's own monthly mail is retired in favour of GAHK-konto's signed monthly statement.
