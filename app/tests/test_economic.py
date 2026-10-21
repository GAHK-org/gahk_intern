"""residents.economic — the e-conomic REST API client (F-010 regnskab, alumneregning automation).

Mocks requests.get directly rather than hitting the network: these are unit tests of the client's own
parsing/error-handling, not of e-conomic's API. The client was developed and manually verified against
the real restapi.e-conomic.com API (GAHK's real company, account 1101) — these tests pin known response
shapes (including the account-scoped-years-link-is-actually-company-wide quirk, and split fiscal years
needing an internal URL slug) so a future e-conomic API change is caught here instead of only at
alumneregning send time.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import pytest
import requests

from residents import economic


@dataclass
class FakeResponse:
    status_code: int = 200
    _json: object = None
    content: bytes = b""
    text: str = ""
    headers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> object:
        return self._json


def _years_response(*periods: tuple[str, str, str]) -> FakeResponse:
    """periods: (year_label, fromDate, toDate) tuples, e.g. ("2025", "2025-04-01", "2025-09-30")."""
    return FakeResponse(
        _json={
            "collection": [
                {
                    "year": label,
                    "fromDate": from_date,
                    "toDate": to_date,
                    "self": f"https://restapi.e-conomic.com/accounting-years/{label.replace('/', '_6_')}",
                    "entries": "https://restapi.e-conomic.com/accounting-years/WRONG-whole-company-link",
                }
                for label, from_date, to_date in periods
            ]
        }
    )


_NO_JOURNALS = FakeResponse(_json={"collection": []})


def _no_drafts_fake_get(inner: Callable[..., FakeResponse]) -> Callable[..., FakeResponse]:
    """Wraps a fake_get so "/journals" short-circuits to no journals (no draft entries found) —
    for tests that only care about posted entries. get_draft_entries_for_account's own tests cover
    the draft-matching logic directly."""

    def fake_get(url: str, **kw: object) -> FakeResponse:
        if url.endswith("/journals"):
            return _NO_JOURNALS
        return inner(url, **kw)

    return fake_get


def test_get_account_parses_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        economic.requests,
        "get",
        lambda url, **kw: FakeResponse(_json={"accountNumber": 1101, "name": "Alumne-Sten", "balance": 0.0}),
    )

    account = economic.get_account(1101)

    assert account == economic.EconomicAccount(account_number=1101, name="Alumne-Sten", balance=0.0)


def test_get_entries_in_range_uses_year_self_link_not_entries_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """The year's own "entries" hyperlink is whole-company, not account-scoped (a real e-conomic API
    quirk) — the client must build the URL from the year's "self" link instead."""
    calls: list[str] = []

    def fake_get(url: str, **kw: object) -> FakeResponse:
        calls.append(url)
        if url.endswith("/accounting-years"):
            return _years_response(("2026", "2026-01-01", "2026-12-31"))
        return FakeResponse(_json={"collection": [{"entryNumber": 1, "date": "2026-08-15", "amount": 100.0}]})

    monkeypatch.setattr(economic.requests, "get", _no_drafts_fake_get(fake_get))

    entries = economic.get_entries_in_range(1101, date(2026, 8, 1), date(2026, 8, 31))

    assert entries == [
        economic.LedgerEntry(
            entry_number=1,
            is_draft=False,
            date="2026-08-15",
            text="",
            amount=100.0,
            entry_type="",
            voucher_number=0,
            due_date=None,
        )
    ]
    # The second call must hit the account-scoped URL (built from "self"), never the "entries" link.
    assert calls[1] == "https://restapi.e-conomic.com/accounts/1101/accounting-years/2026/entries"


def test_get_entries_in_range_filters_out_entries_outside_the_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fiscal period can be wider than the requested date range (e.g. a 6-month e-conomic period
    vs. a one-month billing range) — only entries whose own date is inside [date_from, date_to] are
    kept, even though the whole period's entries are fetched."""

    def fake_get(url: str, **kw: object) -> FakeResponse:
        if url.endswith("/accounting-years"):
            return _years_response(("2026", "2026-04-01", "2026-09-30"))
        return FakeResponse(
            _json={
                "collection": [
                    {"entryNumber": 1, "date": "2026-07-01", "amount": 50.0},  # before range
                    {"entryNumber": 2, "date": "2026-08-15", "amount": 100.0},  # inside range
                    {"entryNumber": 3, "date": "2026-09-01", "amount": 25.0},  # after range
                ]
            }
        )

    monkeypatch.setattr(economic.requests, "get", _no_drafts_fake_get(fake_get))

    entries = economic.get_entries_in_range(1101, date(2026, 8, 1), date(2026, 8, 31))

    assert [e.entry_number for e in entries] == [2]


def test_get_entries_in_range_handles_split_fiscal_year_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fiscal year not aligned to the calendar displays as "2015/2016" but its real URL id is an
    e-conomic-internal slug ("2015_6_2016") — pinned here so a format change is caught."""
    calls: list[str] = []

    def fake_get(url: str, **kw: object) -> FakeResponse:
        calls.append(url)
        if url.endswith("/accounting-years"):
            return _years_response(("2015/2016", "2015-10-01", "2016-03-31"))
        return FakeResponse(_json={"collection": []})

    monkeypatch.setattr(economic.requests, "get", _no_drafts_fake_get(fake_get))

    economic.get_entries_in_range(1101, date(2015, 11, 1), date(2015, 12, 1))

    assert calls[1] == "https://restapi.e-conomic.com/accounts/1101/accounting-years/2015_6_2016/entries"


def test_get_entries_in_range_spans_multiple_overlapping_periods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A requested range crossing an e-conomic fiscal period boundary reads every period it overlaps."""
    calls: list[str] = []

    def fake_get(url: str, **kw: object) -> FakeResponse:
        calls.append(url)
        if url.endswith("/accounting-years"):
            return _years_response(
                ("2026-H1", "2026-01-01", "2026-06-30"), ("2026-H2", "2026-07-01", "2026-12-31")
            )
        return FakeResponse(_json={"collection": []})

    monkeypatch.setattr(economic.requests, "get", _no_drafts_fake_get(fake_get))

    economic.get_entries_in_range(1101, date(2026, 6, 15), date(2026, 7, 15))

    assert len(calls) == 3  # years list + both overlapping periods' entries (/journals is intercepted)


def test_get_entries_in_range_returns_empty_when_no_period_overlaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        economic.requests,
        "get",
        _no_drafts_fake_get(lambda url, **kw: _years_response(("2025", "2025-04-01", "2025-09-30"))),
    )

    assert economic.get_entries_in_range(1101, date(2026, 10, 1), date(2026, 10, 31)) == []


def test_get_draft_entries_matches_account_side_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """A draft line keyed directly against this account uses e-conomic's amount unchanged."""
    monkeypatch.setattr(
        economic.requests,
        "get",
        lambda url, **kw: (
            FakeResponse(_json={"collection": [{"journalNumber": 1}]})
            if url.endswith("/journals")
            else FakeResponse(
                _json={
                    "collection": [
                        {
                            "journalEntryNumber": 21,
                            "date": "2026-10-05",
                            "text": "Print af billede",
                            "amount": 5.0,
                            "account": {"accountNumber": 4207},
                            "contraAccount": {"accountNumber": 2115},
                            "entryType": "financeVoucher",
                            "voucher": {"voucherNumber": 960},
                        }
                    ]
                }
            )
        ),
    )

    entries = economic.get_draft_entries_for_account(4207, date(2026, 10, 1), date(2026, 10, 31))

    assert entries == [
        economic.LedgerEntry(
            entry_number=21,
            is_draft=True,
            date="2026-10-05",
            text="Print af billede",
            amount=5.0,
            entry_type="financeVoucher",
            voucher_number=960,
            due_date=None,
        )
    ]


def test_get_draft_entries_negates_amount_when_matched_via_contra_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch voucher charging many accounts from one shared account only has THIS account as the
    contraAccount on its line — e-conomic's amount there is from the other account's perspective, so
    it must be negated to give this account's own (confirmed against the real API: a real "Gæstespisere"
    batch voucher line showed amount=-255 with contraAccount=4207, and the e-conomic UI's own kontokort
    for account 4207 showed that same posting as +255)."""
    monkeypatch.setattr(
        economic.requests,
        "get",
        lambda url, **kw: (
            FakeResponse(_json={"collection": [{"journalNumber": 1}]})
            if url.endswith("/journals")
            else FakeResponse(
                _json={
                    "collection": [
                        {
                            "journalEntryNumber": 47,
                            "date": "2026-10-05",
                            "text": "Gæstespisere - september",
                            "amount": -255.0,
                            "account": {"accountNumber": 5310},
                            "contraAccount": {"accountNumber": 4207},
                            "entryType": "financeVoucher",
                            "voucher": {"voucherNumber": 970},
                        }
                    ]
                }
            )
        ),
    )

    entries = economic.get_draft_entries_for_account(4207, date(2026, 10, 1), date(2026, 10, 31))

    assert len(entries) == 1
    assert entries[0].amount == 255.0  # negated from e-conomic's -255


def test_get_draft_entries_filters_by_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        economic.requests,
        "get",
        lambda url, **kw: (
            FakeResponse(_json={"collection": [{"journalNumber": 1}]})
            if url.endswith("/journals")
            else FakeResponse(
                _json={
                    "collection": [
                        {
                            "journalEntryNumber": 1,
                            "date": "2026-09-30",  # before range
                            "text": "x",
                            "amount": 1.0,
                            "account": {"accountNumber": 4207},
                            "voucher": {},
                        },
                        {
                            "journalEntryNumber": 2,
                            "date": "2026-10-15",  # inside range
                            "text": "x",
                            "amount": 1.0,
                            "account": {"accountNumber": 4207},
                            "voucher": {},
                        },
                    ]
                }
            )
        ),
    )

    entries = economic.get_draft_entries_for_account(4207, date(2026, 10, 1), date(2026, 10, 31))

    assert [e.entry_number for e in entries] == [2]


def test_401_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        economic.requests, "get", lambda url, **kw: FakeResponse(status_code=401, text="bad token")
    )

    with pytest.raises(economic.EconomicAuthError):
        economic.get_account(1)


def test_403_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        economic.requests, "get", lambda url, **kw: FakeResponse(status_code=403, text="revoked")
    )

    with pytest.raises(economic.EconomicAuthError):
        economic.get_account(1)


def test_400_raises_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        economic.requests, "get", lambda url, **kw: FakeResponse(status_code=400, text="bad account number")
    )

    with pytest.raises(economic.EconomicValidationError):
        economic.get_account(1)


def test_500_raises_plain_api_error_not_a_subclass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        economic.requests, "get", lambda url, **kw: FakeResponse(status_code=500, text="oops")
    )

    with pytest.raises(economic.EconomicApiError) as exc_info:
        economic.get_account(1)
    assert not isinstance(exc_info.value, economic.EconomicAuthError)
    assert not isinstance(exc_info.value, economic.EconomicValidationError)


def test_429_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(economic.time, "sleep", sleeps.append)
    responses = iter(
        [
            FakeResponse(status_code=429, headers={"Retry-After": "2"}),
            FakeResponse(status_code=429),  # no Retry-After this time -> falls back to backoff
            FakeResponse(status_code=200, _json={"accountNumber": 1, "name": "x", "balance": 0.0}),
        ]
    )
    monkeypatch.setattr(economic.requests, "get", lambda url, **kw: next(responses))

    account = economic.get_account(1)

    assert account.account_number == 1
    assert sleeps == [2.0, economic._BASE_BACKOFF_SECONDS * 2**1]  # Retry-After honoured, then exponential


def test_429_exhausted_raises_rate_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(economic.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(economic.requests, "get", lambda url, **kw: FakeResponse(status_code=429))

    with pytest.raises(economic.EconomicRateLimitError):
        economic.get_account(1)


def test_network_error_still_raises_plain_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(url: str, **kwargs: object) -> FakeResponse:
        raise requests.Timeout("too slow")

    monkeypatch.setattr(economic.requests, "get", raise_timeout)

    with pytest.raises(economic.EconomicApiError) as exc_info:
        economic.get_account(1)
    assert not isinstance(exc_info.value, economic.EconomicRateLimitError)
