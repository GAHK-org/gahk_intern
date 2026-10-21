"""e-conomic REST API client for alumneregninger (F-010 regnskab role).

A thin wrapper around only the three calls the alumneregning flow needs: a customer's current
balance/dueAmount, their booked invoices, and an invoice's PDF. Not a general e-conomic client —
e-conomic exposes far more (orders, products, suppliers, ...) this app has no use for.

Auth is two tokens as HTTP headers (X-AppSecretToken + X-AgreementGrantToken), both "demo" in dev by
default (settings.ECONOMIC_APP_SECRET_TOKEN/_AGREEMENT_GRANT_TOKEN) — restapi.e-conomic.com
authenticates "demo"/"demo" against its own public demo company, so this client is exercisable (and
was developed and tested against the real API) before GAHK's e-conomic account is verified for real
tokens. See app/.env.example for how real tokens are obtained.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings

BASE_URL = "https://restapi.e-conomic.com"
# PDF downloads are e-conomic's priciest rate-limit tier (observed x-callcost: XXL vs S/M/L for the
# JSON lookups below) — rendering a statement server-side is slower than a lookup, hence the longer
# timeout.
_JSON_TIMEOUT = 15
_PDF_TIMEOUT = 30

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
    """400/422 — the request itself was malformed (bad filter syntax, unknown id, ...). Retrying the
    identical request fails the same way every time; this signals a bug in the caller, not a
    transient condition, so _get does not retry it the way it retries a 429."""


class EconomicRateLimitError(EconomicApiError):
    """429 — e-conomic's call-cost rate limit was hit and MAX_RATE_LIMIT_RETRIES backoff retries
    were all still rate-limited. Raised only after _get has already tried to recover; a caller
    seeing this should wait longer than _get's own backoff before trying again."""


@dataclass(frozen=True)
class EconomicCustomer:
    customer_number: int
    name: str
    balance: float  # e-conomic's own sign convention: positive = the customer owes GAHK
    due_amount: float


@dataclass(frozen=True)
class BookedInvoice:
    booked_invoice_number: int
    date: str
    due_date: str | None
    remainder: float
    pdf_url: str


@dataclass(frozen=True)
class LedgerEntry:
    """One posting from e-conomic's own ledger (GET .../entries) touching this customer's account —
    the raw material residents.ledger mirrors into an immutable local row. Not booked-invoice data:
    this also covers payments, manual postings, anything entryType covers."""

    entry_number: int
    date: str
    text: str
    amount: float  # e-conomic's own sign convention: positive = the customer owes GAHK
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


def get_customer(customer_number: int) -> EconomicCustomer:
    """The customer's current balance/dueAmount — what the manual process calls "economics status"."""
    data = _get(f"{BASE_URL}/customers/{customer_number}").json()
    return EconomicCustomer(
        customer_number=data["customerNumber"],
        name=data.get("name", ""),
        balance=data.get("balance", 0.0),
        due_amount=data.get("dueAmount", 0.0),
    )


def get_booked_invoices(customer_number: int) -> list[BookedInvoice]:
    """Every booked invoice for this customer, in e-conomic's own (oldest-first) order."""
    data = _get(f"{BASE_URL}/customers/{customer_number}/invoices/booked", params={"pagesize": 1000}).json()
    return [
        BookedInvoice(
            booked_invoice_number=item["bookedInvoiceNumber"],
            date=item["date"],
            due_date=item.get("dueDate"),
            remainder=item.get("remainder", 0.0),
            pdf_url=item["pdf"]["download"],
        )
        for item in data.get("collection", [])
    ]


def _accounting_years() -> list[str]:
    data = _get(f"{BASE_URL}/accounting-years").json()
    return [item["year"] for item in data.get("collection", [])]


def get_entries(customer_number: int) -> list[LedgerEntry]:
    """Every ledger posting touching this customer's account, across every accounting year.

    e-conomic has no flat /entries collection — GET /entries itself returns a developerHint saying so
    — entries only exist nested under an accounting year (or account+year). So this enumerates years
    first (cheap: one small JSON call) and queries each year's entries filtered to this customer.
    Small enough at GAHK's volume (a handful of years, each with a handful of alumni entries) to do
    unconditionally rather than caching/narrowing to "recent" years — see residents.ledger for how
    this gets made idempotent (entry_number is the sync key, so re-fetching already-seen years is
    wasted calls but never wrong).
    """
    entries: list[LedgerEntry] = []
    for year in _accounting_years():
        data = _get(
            f"{BASE_URL}/accounting-years/{year}/entries",
            params={"filter": f"customer.customerNumber$eq:{customer_number}", "pagesize": 1000},
        ).json()
        entries.extend(
            LedgerEntry(
                entry_number=item["entryNumber"],
                date=item["date"],
                text=item.get("text", ""),
                amount=item.get("amount", 0.0),
                entry_type=item.get("entryType", ""),
                voucher_number=item.get("voucherNumber", 0),
                due_date=item.get("dueDate"),
            )
            for item in data.get("collection", [])
        )
    return entries


def get_invoice_pdf(pdf_url: str) -> bytes:
    """The rendered PDF for one booked invoice, fetched from its own `pdf.download` link."""
    return _get(pdf_url, timeout=_PDF_TIMEOUT).content
