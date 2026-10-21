"""e-conomic REST API client for alumneregninger (F-010 regnskab role).

GAHK does not track alumni as e-conomic Customers/debtors (there is exactly one real Customer record
in GAHK's e-conomic company) — each alumne instead has their own account in the chart of accounts,
and their balance/statement is the sum of that account's entries. This client is scoped to exactly
that: looking up an account, and reading its entries. Not a general e-conomic client — e-conomic
exposes far more (customers, orders, products, suppliers, invoicing, ...) this app has no use for.

Auth is two tokens as HTTP headers (X-AppSecretToken + X-AgreementGrantToken), both "demo" in dev by
default (settings.ECONOMIC_APP_SECRET_TOKEN/_AGREEMENT_GRANT_TOKEN) — restapi.e-conomic.com
authenticates "demo"/"demo" against its own public demo company, so this client is exercisable (and
was developed and tested against the real API) before GAHK's e-conomic account was verified for real
tokens. See app/.env.example for how real tokens are obtained.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests
from django.conf import settings

BASE_URL = "https://restapi.e-conomic.com"
_JSON_TIMEOUT = 15

# e-conomic's rate-limit status. One initial attempt plus this many retries, each backing off
# (Retry-After if e-conomic sends one, else exponential: 1s, 2s, 4s) before giving up.
_RATE_LIMIT_STATUS = 429
MAX_RATE_LIMIT_RETRIES = 3
_BASE_BACKOFF_SECONDS = 1.0


class EconomicApiError(Exception):
    """A non-2xx response from e-conomic, or the request itself failing (network, timeout, ...)."""


class EconomicAuthError(EconomicApiError):
    """401/403 — the AppSecretToken/AgreementGrantToken pair is invalid, expired, or revoked.

    e-conomic's tokens are long-lived, not short-lived OAuth access tokens, so there is no silent
    refresh to attempt here — the only fix is a human re-running the install/grant flow in
    e-conomic's developer portal to mint a new AgreementGrantToken. Callers (residents.alumneregning)
    turn this into an actionable "reconnect e-conomic" message rather than a generic failure.
    """


class EconomicValidationError(EconomicApiError):
    """400/422 — the request itself was malformed (bad account number, unknown year, ...). Retrying
    the identical request fails the same way every time; this signals a bug in the caller, not a
    transient condition, so _get does not retry it the way it retries a 429."""


class EconomicRateLimitError(EconomicApiError):
    """429 — e-conomic's call-cost rate limit was hit and MAX_RATE_LIMIT_RETRIES backoff retries
    were all still rate-limited. Raised only after _get has already tried to recover; a caller
    seeing this should wait longer than _get's own backoff before trying again."""


@dataclass(frozen=True)
class EconomicAccount:
    account_number: int
    name: str
    balance: float  # e-conomic's own running balance for this account, at the time of the call


@dataclass(frozen=True)
class LedgerEntry:
    """One posting from e-conomic's own ledger touching this account — the raw material
    residents.ledger mirrors into an immutable local row.

    entry_number is entryNumber for a posted entry or journalEntryNumber for a draft one — two
    independent e-conomic counters that can collide numerically, which is why is_draft is part of
    the identity (see EconomicLedgerEntry's uniqueness constraint)."""

    entry_number: int
    is_draft: bool
    date: str
    text: str
    amount: float  # e-conomic's own sign convention: positive = the alumne owes GAHK
    entry_type: str
    voucher_number: int
    due_date: str | None


def _headers() -> dict[str, str]:
    return {
        "X-AppSecretToken": settings.ECONOMIC_APP_SECRET_TOKEN,
        "X-AgreementGrantToken": settings.ECONOMIC_AGREEMENT_GRANT_TOKEN,
    }


def _error_for_status(response: requests.Response, url: str) -> EconomicApiError:
    detail = f"e-conomic {response.status_code} on {url}: {response.text[:200]}"
    if response.status_code in (401, 403):
        return EconomicAuthError(detail)
    if response.status_code in (400, 422):
        return EconomicValidationError(detail)
    return EconomicApiError(detail)


def _retry_delay_seconds(response: requests.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass  # not a valid seconds value (e.g. an HTTP-date) — fall through to our own backoff
    return _BASE_BACKOFF_SECONDS * (2**attempt)


def _get(
    url: str, *, params: dict[str, Any] | None = None, timeout: int = _JSON_TIMEOUT
) -> requests.Response:
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            response = requests.get(url, headers=_headers(), params=params, timeout=timeout)
        except requests.RequestException as exc:
            raise EconomicApiError(f"e-conomic request failed: {url}") from exc

        if response.status_code != _RATE_LIMIT_STATUS:
            if not response.ok:
                raise _error_for_status(response, url)
            return response

        if attempt < MAX_RATE_LIMIT_RETRIES:
            time.sleep(_retry_delay_seconds(response, attempt))

    raise EconomicRateLimitError(
        f"e-conomic rate limit on {url}: still 429 after {MAX_RATE_LIMIT_RETRIES} retries"
    )


def get_account(account_number: int) -> EconomicAccount:
    """The account's name and e-conomic's own current balance for it.

    This IS the source of truth for "what does this alumne currently owe" — not a sum we compute
    ourselves. GAHK's real alumni accounts are type "status" (balance-sheet: Danish e-conomic UI
    calls this "Balance") and carry a continuous running balance across fiscal periods, so e-conomic's
    own balance figure is already correct; trusting it also means this code does not need to know or
    guess an account's type. (An earlier version of this function/get_period_entries assumed every
    account was `profitAndLoss` — reset to zero each fiscal period — based on account 1101, which
    turned out not to be representative of GAHK's real per-alumne accounts; see git history.)
    """
    data = _get(f"{BASE_URL}/accounts/{account_number}").json()
    return EconomicAccount(
        account_number=data["accountNumber"],
        name=data.get("name", ""),
        balance=data.get("balance", 0.0),
    )


def get_draft_entries_for_account(account_number: int, date_from: date, date_to: date) -> list[LedgerEntry]:
    """Unposted/draft entries touching this account, dated within [date_from, date_to] — from
    e-conomic's journals (GAHK has exactly one, "Daglig"/journal 1, but this reads whatever journals
    exist rather than hardcoding that). Regnskab wants drafts included in a statement, not just
    formally posted entries (F-010) — e-conomic's own kontokort UI shows them too.

    Matches on EITHER side of the double-entry pair: a draft line's own "account" field is only
    populated for the side it was originally keyed against (e.g. "Print af billede" against this
    account directly); a batch voucher charging many residents from one shared income account
    (e.g. "Gæstespisere" — guest dining) only has this account as the "contraAccount" on its line,
    never as "account". Missing that side means missing real drafts the e-conomic UI does show —
    confirmed against the real API/UI together: a batch voucher's charge to one resident showed up
    only via contraAccount, not account. When matched via contraAccount, the amount is negated to
    give this account's own perspective (e-conomic's amount there is from the OTHER account's side).
    """
    entries: list[LedgerEntry] = []
    journals = _get(f"{BASE_URL}/journals", params={"pagesize": 1000}).json()
    for journal in journals.get("collection", []):
        data = _get(
            f"{BASE_URL}/journals/{journal['journalNumber']}/entries",
            params={
                "pagesize": 1000,
                "filter": (
                    f"account.accountNumber$eq:{account_number}"
                    f"$or:contraAccount.accountNumber$eq:{account_number}"
                ),
            },
        ).json()
        for item in data.get("collection", []):
            if not (date_from <= date.fromisoformat(item["date"]) <= date_to):
                continue
            is_primary = item.get("account", {}).get("accountNumber") == account_number
            amount = item.get("amount", 0.0) if is_primary else -item.get("amount", 0.0)
            entries.append(
                LedgerEntry(
                    entry_number=item["journalEntryNumber"],
                    is_draft=True,
                    date=item["date"],
                    text=item.get("text", ""),
                    amount=amount,
                    entry_type=item.get("entryType", ""),
                    voucher_number=item.get("voucher", {}).get("voucherNumber", 0),
                    due_date=None,
                )
            )
    return entries


def get_entries_in_range(account_number: int, date_from: date, date_to: date) -> list[LedgerEntry]:
    """Every posting on this account whose own date falls within [date_from, date_to] — e.g. exactly
    "August 2026", independent of e-conomic's own ~6-month fiscal period boundaries — INCLUDING draft
    entries not yet formally posted (get_draft_entries_for_account; regnskab wants those included,
    not just posted ones). This is the period-specific detail shown on a statement; the balance
    figure (get_account) is separate and NOT derived from these entries, since an alumne can have a
    nonzero balance with nothing posted in a given range (prior debt carried forward) — see
    residents.alumneregning.

    e-conomic's periods do not align with calendar months, so [date_from, date_to] can span more than
    one fiscal period; this reads every period overlapping the range and keeps only entries whose own
    date is actually inside it. The account-scoped entries URL has to be built from the period's URL
    id (the "self" link's last path segment, e.g. "2015_6_2016" for the fiscal year e-conomic
    displays as "2015/2016" — an internal slug, not standard percent-encoding of the display value) —
    and NOT from the period's own "entries" link, which is the whole-COMPANY entries for that period
    (confirmed against the real API: it returned 19108 rows, every posting on every account, for an
    account that only has 124 of its own across its entire ~10-year history), despite being reached
    from an account-scoped collection.

    pagesize=1000 on the years list itself, not just the entries calls: e-conomic defaults to 20 per
    page, and a long-lived account can have more than 20 fiscal periods by now (confirmed against the
    real API — a resident's account had 23) — an unpaginated call silently drops the newest ones,
    which is exactly backwards: those are the ones a billing range is usually asking about.
    """
    years = _get(f"{BASE_URL}/accounts/{account_number}/accounting-years", params={"pagesize": 1000}).json()
    overlapping = [
        year
        for year in years.get("collection", [])
        if date.fromisoformat(year["fromDate"]) <= date_to and date_from <= date.fromisoformat(year["toDate"])
    ]

    entries: list[LedgerEntry] = []
    for period in overlapping:
        period_id = period["self"].rsplit("/", 1)[-1]
        data = _get(
            f"{BASE_URL}/accounts/{account_number}/accounting-years/{period_id}/entries",
            params={"pagesize": 1000},
        ).json()
        entries.extend(
            LedgerEntry(
                entry_number=item["entryNumber"],
                is_draft=False,
                date=item["date"],
                text=item.get("text", ""),
                amount=item.get("amount", 0.0),
                entry_type=item.get("entryType", ""),
                voucher_number=item.get("voucherNumber", 0),
                due_date=item.get("dueDate"),
            )
            for item in data.get("collection", [])
            if date_from <= date.fromisoformat(item["date"]) <= date_to
        )

    entries.extend(get_draft_entries_for_account(account_number, date_from, date_to))
    return entries
