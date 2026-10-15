# Feature: Ølkælder — the beer cellar's till, balances, interest and debt warnings

**Feature ID:** F-003

## What it is

The kollegium's beer cellar sells on credit. A till (an old iPad in the cellar) lists the products
and the residents with an account; a buyer taps what they took, picks themselves — or several people
to split the basket — and the sale is booked against their account. Residents top up by bank
transfer, and the ølkælder officers (role `oelkaelder`, "ØK") register those deposits, manage the
assortment, read reports, correct mistakes and decide on interest and a credit limit. Every resident
can see their own balance and statement at `/intern/oelkaelder/min-saldo`.

All of it lives in `app/oelkaelder/`, mounted under `/intern/oelkaelder/` (`app/residents/urls.py`),
namespace `oelkaelder:`.

**What it replaces.** The PHP version was an Angular till talking to JSON endpoints, with a mutable
`saldo` column updated by read-modify-write, prices taken from the client, and several money-moving
actions on GET. The till's look and DOM were reproduced faithfully (`app/templates/oelkaelder/shop.html`
says so); the money model underneath was not.

## Access

| Who | What |
| --- | --- |
| Anyone whose request comes from a till IP (no login) | The till: list products and shoppers, record a purchase |
| Every logged-in resident | Their own balance and statement (`min-saldo`) |
| Role `oelkaelder` | Everything under `admin…`, plus deposits |

**The till is gated by source IP, not by login** (`_is_kiosk` in `app/oelkaelder/views.py`). The
allowed IPs are `OELKAELDER_KIOSK_IPS` (comma-separated env var, `app/config/settings.py`); with
`DEBUG` on, the gate is open. Off the list, both `shop` and `purchase` raise 403 ("Tillen er kun
tilgængelig fra kollegiets netværk."). `_client_ip` takes the **last** `X-Forwarded-For` hop, the
one Traefik appended, on the stated premise that the app server is reachable only through the proxy.

Officer views use `residents.permissions.role_required("oelkaelder")`, which reads *effective* roles
(so the admin preview tool applies) and gives `administrator`/superusers every role. Every
state-changing officer endpoint is `@require_POST`.

## Routes

All under `/intern/oelkaelder/`.

| URL | Method | Who | Purpose |
| --- | --- | --- | --- |
| `` (root) | GET | till IP | The till (`shop`) |
| `purchase` | POST | till IP | Record a sale; redirects back to the till |
| `min-saldo` | GET | logged in | Own balance(s), last 100 statement lines, bank details |
| `admin` | GET | ØK | Balances (debtors first), inactive accounts, add account, last 15 deposits, warning mails, interest, credit limit, report links |
| `deposit/<pk>` | POST | ØK | Register a deposit (kroner) for shopper `pk` |
| `deposit/<pk>/annuller` | POST | ØK | Void a deposit |
| `admin/deaktiver/<pk>` | POST | ØK | Deactivate a shopper (hides them from the till) |
| `admin/genaktiver` | POST | ØK | Reactivate (`shopper` in the body) |
| `admin/tilfoej` | POST | ØK | Create an account for a resident (`resident` in the body) |
| `admin/advarsel/<pk>` | POST | ØK | Edit warning mail `pk` |
| `admin/rente` | POST | ØK | Edit the interest policy |
| `admin/koebsgraense` | POST | ØK | Edit the credit limit |
| `admin/rente/anvend` | POST | ØK | Charge this month's interest now |
| `admin/rapport/indbetalinger` | GET | ØK | Deposits per active shopper in a date range, plus current balance |
| `admin/rapport/salg` | GET | ØK | Quantity and revenue per product in a date range |
| `admin/rapport/antal` | GET | ØK | Quantity per product in a date range |
| `admin/salgsoverblik` | GET | ØK | Salgsoverblik: every sale, one row per transaction, paginated |
| `admin/salgsoverblik/<pk>/annuller` | POST | ØK | Void a sale |
| `admin/person` | GET | ØK | One resident's full history (`?resident=<id>`) |
| `admin/person/<pk>/justering` | POST | ØK | Manual balance correction for shopper `pk` |
| `admin/justering/<pk>/annuller` | POST | ØK | Void a manual correction |
| `admin/produkter` | GET | ØK | The assortment |
| `admin/produkter/opret` | POST | ØK | Create a product |
| `admin/produkter/<pk>` | POST | ØK | Update a product |

The three reports and Salgsoverblik take `?start=&end=` (ISO dates) and `?format=csv|xlsx` for a
download (`core.exports.csv_or_xlsx_response`); without `format` they render a table.

## Data model

`app/oelkaelder/models.py`. **All money is integer øre**; forms take kroner and convert.

- `Product` — name, `price_ore`, optional `weight_price_ore` (per 100 g) and `price_steps` (a list,
  or the legacy `"50;100;500;1000"` string, normalised by `price_steps_ore()`), image, `active` (on
  the till), `highlighted` (sorted first). `pricing_mode` is `step` > `weight` > `fixed`.
- `Shopper` — an account, FK to `Resident` (`related_name="shopper_accounts"`), `active`. A
  resident may in principle have several; `min-saldo` and the person history list them all.
- `Deposit` — positive `amount_ore`, soft-deletable (`is_valid`).
- `Transaction` — one sale; `created_at`, `is_valid`. `TransactionItem` — product, quantity (grams
  for weight lines), server-computed line `price_ore`. `PurchaseShare` — one per buyer, `share_ore`;
  unique per (transaction, shopper).
- `Adjustment` — signed correction, `kind` = `interest` or `manual`, `reason`, soft-deletable.
- `Warning` — a debt-warning mail: `message`, `threshold_ore`, `active`.
- `InterestPolicy` and `PurchasePolicy` — single-row config (`pk=1`, `get()` creates it).
- `LogEntry` — free-text audit trail written by every service function.

**The balance is derived, never stored**: valid deposits − shares of valid transactions + valid
adjustments (`Shopper.balance_ore`). Negative means the shopper owes money. `services.bulk_balances`
computes the same figure for many shoppers in three queries; a test pins the two to each other.

## The till

`shop` renders products (active, highlighted first, then by name) and active shoppers (by first
name) with their balance. The page extends `kiosk_base.html` and uses **inline ES5 and no
Alpine/Tailwind bundle**, because the till's iPad runs iOS 10.3; a test asserts `dist/app.js` and
`x-data` are absent. Flow: tap products into a basket (step products ask which step; weight products
ask for grams) → choose a buyer from the list or by typing their alumne-ID (the resident id), or
"fordel på" several → a 5-second countdown with *fortryd* → POST of repeated `shopper` fields plus
a `basket` JSON list of `{product, mode, qty|grams|step_ore}`.

`services.record_purchase` does the rest in one `transaction.atomic`:

- only **active** shoppers and **active** products count; an empty buyer set or a zero total
  ("Tom kurv.") raises and rolls back;
- **each line is priced from the database** (`_line_ore`): fixed = price × qty, step = the tapped
  step × qty but only if that step is one of the product's own steps ("Ugyldig pris…"), weight =
  per-100 g price × grams / 100. Non-positive lines are skipped;
- the total is split with **largest remainder** (`largest_remainder`), so shares sum to the total
  exactly; the extra øre go to the lowest shopper ids;
- a `LogEntry` is written, and debt-warning mails are queued with `transaction.on_commit`.

Errors come back to the till as a flash message.

## Credit limit

`PurchasePolicy`: `active` (default **off**) and `block_below_ore` (default −100 kr). When on, a
shopper whose balance is **strictly below** the limit cannot buy. It is enforced in `record_purchase`,
not just on the till, and a split sale containing one blocked buyer is refused **whole**, naming
them. The till shows a blocked shopper as "Spærret – indbetal først" without buy buttons, and the
alumne-ID path opens a dedicated "Kontoen er spærret" box instead of "Forkert nummer".

## Debt warnings

`services.send_debt_warnings` runs after a purchase commits. For each active `Warning`, if the
buyer's balance went from **above** its threshold to **below** it, the shopper gets the message by
mail from `OELKAELDER_FROM_EMAIL` (subject "Ølkælder saldo"), with `SALDOSALDOSALDO` or `{saldo}`
replaced by the new balance. It is edge-triggered and stateless: no repeat mail while the balance
stays below, and a new mail on the next downward crossing. Migration `0004` seeds two warnings
(pk 1 at 0 kr, pk 2 at −100 kr); the admin page edits exactly pk 1 and 2. Mail failures are
swallowed and logged.

## Interest

`InterestPolicy`: `active` (default **off**), `rate_percent` (default 5), `threshold_ore` (default
−100 kr). Interest is **not scheduled**: an officer presses "Anvend rente nu", which calls
`services.apply_interest`. For every active shopper with a balance strictly below the threshold
and no interest adjustment yet this calendar month, it writes a negative `Adjustment` of
rate × debt, rounded half-up to whole øre. Running it twice in a month charges nobody twice.

## Corrections

- **Void a sale** (`services.void_purchase`) clears `Transaction.is_valid`. Because balances are
  derived, every buyer is refunded at once with no compensating rows. It is idempotent (returns
  False, writes no second log line) and logs total, buyers and actor. Voided sales stay visible in
  Salgsoverblik, marked and without a void button.
- **Void a deposit** sets `Deposit.is_valid = False`.
- **Manual adjustment** (`services.record_adjustment`): the form asks for a positive kroner amount
  plus a direction (`subtract`/`add`) instead of a signed number; a reason is required and the
  amount may not be zero. Shown in the resident's own statement under its reason.
- **Void a manual adjustment** (`services.void_adjustment`) — manual only, idempotent, logged.

## Officer views

**Salgsoverblik** (`all_sales`) defaults to the last 90 days, pages at 50, can search buyers by full
name or email (`q`) and filter `buyers=none|any`. The total is the sum of the **items**, and
"pr. person" is read off the actual shares (a range if they differ by an øre). Sales whose buyers
were never migrated show as "(ukendt — fraflytter)". Export covers the whole filtered set up to
5000 rows; above that it refuses and asks for a shorter period. Pagers and the void redirect carry
only a whitelisted set of filter params.

**Person history** (`person_history`) and `min-saldo` share `_account_entries`: purchase shares as
debits (text = the basket's items), deposits and adjustments as credits, newest first.

## Scheduled jobs

| Job | When | What |
| --- | --- | --- |
| `oelkaelder.tasks.send_monthly_statements` (Celery beat, `email-oelkaelder-monthly-statements`) | 1st of the month, 06:10 | Mails every active shopper whose resident is active: the previous month's deposits, purchases, adjustments, and the balance **at the end of that month** |

## Decisions

- **Integer øre and a derived balance** (`models.py` docstring) remove the legacy non-atomic
  `saldo` read-modify-write and its fractional-øre drift; voiding becomes a flag flip.
- **The credit limit is a separate policy from the interest threshold** (`PurchasePolicy`
  docstring). Both default to −100 kr, and the till used to hardcode that number, so "owes enough to
  pay interest" had silently become "may not buy". It is off by default because the PHP till never
  refused a sale.
- **Voiding a sale deliberately does not undo** a warning mail already sent, nor interest charged
  while the debt included it — that interest stays (`void_purchase` docstring).
- **Interest adjustments cannot be voided by hand**, because that would desynchronise
  `apply_interest`'s once-per-month guard (`void_adjustment` docstring).
- **Manual adjustments need a written reason**: an unexplained change looks like a bug to the
  resident. They exist to repair sales the 2026-08 migration could not reconstruct, which went wrong
  in both directions — hence the sign (`record_adjustment` docstring).
- **The statement quotes the balance at the end of its month**, not today's; and the month name is
  formatted with Django's `date_format` because `strftime` printed English (`tasks.py`).
- **Salgsoverblik defaults to 90 days**, so the default view and its export button do not cover
  every sale ever recorded. Buyer search uses `Exists()` so a sale is not repeated per matching
  buyer. `Transaction` orders by `(-created_at, -id)` because legacy timestamps are minute-resolution
  and ties would otherwise move rows between pages (`views.py`, `models.py`).

## Open questions

- **A non-numeric `shopper` value 500s the till.** `purchase` parses `int(x)` for each `shopper`
  before its `try` block.
- **`inf` as an amount 500s officer forms.** `add_deposit`, `_kr_to_ore` and `_signed_ore` use
  `float()`; `round(inf)` raises `OverflowError`, which nothing catches.
- **Voiding a deposit is not logged**, unlike voiding a sale or an adjustment (`void_deposit` writes
  no `LogEntry`), and it records no actor.
- **The till's red balance styling ignores the policy.** It marks `balance <= block_below_ore` even
  while the limit is off, and uses `<=` where the rule is strictly `<`.
- **The seeded warning texts promise 5 % monthly interest**, but `InterestPolicy` defaults to off and
  is only charged when an officer presses the button. Whether anyone does so monthly is not visible
  in code.
- **Interest month boundary.** `apply_interest` compares `timezone.now()`'s UTC year/month with
  `created_at__month` lookups evaluated in Europe/Copenhagen; pressed in the first hour or two of a
  month, the once-per-month guard can look at the wrong month.
- **One failed address stops the statement run.** `send_monthly_statements` calls `send_mail`
  without `fail_silently`, so the remaining shoppers get nothing.
- **Accounts outlive residents.** Nothing deactivates a `Shopper` when the resident moves out; the
  till keeps listing them until an officer does. Statements skip inactive residents.
- **One account per resident is not enforced.** `Shopper.resident` is a plain FK; `add_shopper`'s
  `get_or_create` would raise `MultipleObjectsReturned` if two ever existed.
- **The admin page and the deposits report compute balances one shopper at a time** (three queries
  each) rather than with `bulk_balances`, which the till was given for exactly this reason.
