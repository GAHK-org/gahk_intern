# Ølkælder

The beer cellar's till and every resident's tab, sold on credit and settled by bank transfer.

## Access
- The till (`/intern/oelkaelder/`) needs no login but only works from the beer cellar's own network.
- Every logged-in resident can see their own balance and statement (`/intern/oelkaelder/min-saldo`), with the bank details for topping up.
- The Ølkælder role manages accounts, deposits, products, corrections, warnings, interest, the credit limit and reports.

## Accounts and balance
- A resident can only buy once the Ølkælder role has created an account for them; the admin page lists residents without one.
- Accounts can be deactivated (hidden from the till) and reactivated; nothing deactivates them automatically on move-out.
- The balance is deposits minus purchases plus adjustments; negative means the resident owes money.
- The admin page lists all active accounts, biggest debtors first, plus the inactive ones.

## Buying at the till
- The till shows active products (featured ones first) and every active account with its balance.
- Tap products into a basket; some products offer a choice of fixed price steps, and per-weight products ask for grams.
- Pick the buyer from the list or by typing their alumne-ID, or pick several with "fordel på" to split the basket evenly; any odd øre goes to one of them.
- A 5-second countdown with *Fortryd* and *Godkend nu* precedes recording the sale.
- Prices are always taken from the current product list, never from the till page.
- Errors (empty basket, inactive buyer, invalid price) are shown on the till and nothing is charged.

## Credit limit
- Off by default. When on, an account whose balance is below the configured limit (e.g. −100 kr) cannot buy; exactly at the limit is still allowed.
- The till shows a blocked account as "Spærret – indbetal først" and explains the block when the alumne-ID is typed.
- A split sale that includes a blocked buyer is refused as a whole, naming who is blocked.
- A deposit that lifts the balance above the limit reopens the account immediately.

## Deposits and corrections
- Residents pay by bank transfer; the Ølkælder role registers each deposit against the account (positive amounts only).
- A mistaken deposit can be voided and drops out of the balance.
- A sale can be voided from the sales overview; every buyer of that basket is refunded at once. Voiding twice does nothing.
- A manual adjustment adds or subtracts an amount from one account and requires a written reason; it shows in the resident's own statement under that reason.
- Manual adjustments can be voided; interest charges cannot.
- Sales, deposits, adjustments, voids and interest runs are written to an audit log.

## Debt warnings
- Two warning emails are configurable (text, threshold in kr, on/off); the text can include the new balance.
- A resident is emailed when a purchase takes their balance from above a threshold to below it — once per crossing, not repeatedly while below.
- A failed warning email never affects the purchase.

## Interest
- Off by default; the Ølkælder role sets a monthly percentage and a debt threshold.
- Interest is charged only when an officer presses "Anvend rente nu": every active account below the threshold is charged the percentage of its debt as an adjustment.
- Each account is charged at most once per calendar month, however often the button is pressed.

## Reports and history
- Deposit, sales and quantity reports per date range, on screen or downloaded as CSV or Excel.
- Salgsoverblik lists every sale (buyers, total, per-person share, items, voided status), 50 per page, defaulting to the last 90 days; it can search buyers by name or email and filter sales with or without known buyers.
- Salgsoverblik exports the whole filtered set, up to 5000 sales; larger exports are refused with a request for a shorter period.
- The person history shows one resident's full statement: purchases, deposits and adjustments, newest first.
- The resident's own page shows the latest 100 statement lines; the officer view shows 200.

## Products
- The Ølkælder role creates and edits products: name, price, image, shown on the till or not, featured or not.
- Prices must be positive.

## Monthly statement
- On the 1st of each month at 06:10, every active account holder who is still a resident is emailed last month's deposits, purchases, adjustments and the balance at the end of that month.

## Decisions
- The credit limit is separate from the interest threshold: both default to −100 kr, and sharing one number had turned "pays interest" into "may not buy". It is off by default so that blocking sales is an explicit choice by the Ølkælder officers.
- The credit limit is enforced when the sale is recorded, not just on the till screen, so a split sale cannot smuggle a blocked buyer in.
- Voiding a sale does not undo a warning email already sent or interest already charged on that debt.
- Interest charges cannot be voided by hand, because that would break the once-per-month guard.
- Adjustments require a reason because an unexplained balance change looks like a bug to the resident; they go both ways because imported sales left some residents under-charged and others over-charged.
- The statement quotes the balance at the end of its own month, so its numbers add up even after new purchases.
