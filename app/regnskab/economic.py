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
    """401/403 — the AppSecretToken/AgreementGrantToken pair is invalid, expired, or revoked. No
    silent refresh is possible; needs a human to re-run the grant flow in e-conomic's developer portal."""


class EconomicValidationError(EconomicApiError):
    """400/422 — the request itself was malformed; not retried, unlike a 429."""


class EconomicRateLimitError(EconomicApiError):
    """429 — still rate-limited after MAX_RATE_LIMIT_RETRIES backoff retries."""


@dataclass(frozen=True)
class EconomicAccount:
    account_number: int
    name: str
    balance: float  # e-conomic's own running balance for this account, at the time of the call


@dataclass(frozen=True)
class LedgerEntry:
    """One posting from e-conomic's ledger. entry_number is entryNumber (posted) or journalEntryNumber
    (draft) — two independent counters that can collide, hence is_draft is part of the identity."""

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
    """The account's name and e-conomic's own current balance — the source of truth for what an
    alumne owes, not a sum we compute ourselves (GAHK's accounts carry a continuous running balance)."""
    data = _get(f"{BASE_URL}/accounts/{account_number}").json()
    return EconomicAccount(
        account_number=data["accountNumber"],
        name=data.get("name", ""),
        balance=data.get("balance", 0.0),
    )


def get_draft_entries_for_account(
    account_number: int, date_from: date | None = None, date_to: date | None = None
) -> list[LedgerEntry]:
    """Unposted/draft entries touching this account — regnskab wants these on a statement too, not
    just posted ones. date_from/date_to are optional: omit both to get every current draft regardless
    of date, which is what the account's true current balance needs (a draft from an earlier period
    that is still unposted is still real, uncounted debt — see alumneregning._prepare_one). Matches on
    EITHER side of the double-entry pair: a batch voucher (e.g. "Gæstespisere") only has this account
    as "contraAccount", never "account" — amount is negated in that case, since e-conomic's amount
    there is the other side's."""
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
            item_date = date.fromisoformat(item["date"])
            if (date_from is not None and item_date < date_from) or (
                date_to is not None and item_date > date_to
            ):
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


def get_posted_entries_in_range(account_number: int, date_from: date, date_to: date) -> list[LedgerEntry]:
    """Every POSTED posting on this account dated within [date_from, date_to] — drafts are a separate
    call (get_draft_entries_for_account), deliberately unscoped by the caller rather than fetched here,
    since a draft's own date does not bound how long it can sit unposted (regnskab.alumneregning wants
    every current draft counted, not just ones dated in the billed period). Reads every e-conomic
    fiscal period overlapping the range (periods don't align to calendar months), building each
    period's entries URL from its "self" link — NOT its own "entries" link, which is actually the
    whole company's entries despite looking account-scoped. pagesize=1000 throughout: e-conomic
    defaults to 20/page and would otherwise silently drop a long-lived account's newest periods."""
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

    return entries
