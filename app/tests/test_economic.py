"""residents.economic — the e-conomic REST API client (F-010 regnskab, alumneregning automation).

Mocks requests.get directly rather than hitting the network: these are unit tests of the client's own
parsing/error-handling, not of e-conomic's API. The client was developed and manually verified against
the real restapi.e-conomic.com demo company (customerNumber=1); these tests pin that known response
shape so a future e-conomic API change is caught here instead of only at alumneregning send time.
"""

from dataclasses import dataclass

import pytest
import requests

from residents import economic


@dataclass
class FakeResponse:
    status_code: int = 200
    _json: object = None
    content: bytes = b""
    text: str = ""

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> object:
        return self._json


def test_get_customer_parses_balance_and_due_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append((url, kwargs))
        return FakeResponse(
            _json={"customerNumber": 1, "name": "Decathlon", "balance": -1600.0, "dueAmount": 0.0}
        )

    monkeypatch.setattr(economic.requests, "get", fake_get)

    customer = economic.get_customer(1)

    assert customer == economic.EconomicCustomer(
        customer_number=1, name="Decathlon", balance=-1600.0, due_amount=0.0
    )
    url, kwargs = calls[0]
    assert url == f"{economic.BASE_URL}/customers/1"
    assert kwargs["headers"]["X-AppSecretToken"]
    assert kwargs["headers"]["X-AgreementGrantToken"]


def test_get_customer_raises_on_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        economic.requests, "get", lambda url, **kw: FakeResponse(status_code=404, text="Customer not found")
    )

    with pytest.raises(economic.EconomicApiError, match="404"):
        economic.get_customer(999)


def test_get_customer_raises_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_connection_error(url: str, **kwargs: object) -> FakeResponse:
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(economic.requests, "get", raise_connection_error)

    with pytest.raises(economic.EconomicApiError):
        economic.get_customer(1)


def test_get_booked_invoices_parses_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "collection": [
            {
                "bookedInvoiceNumber": 1,
                "date": "2022-06-02",
                "dueDate": "2022-06-10",
                "remainder": 0.0,
                "pdf": {"download": "https://restapi.e-conomic.com/invoices/booked/1/pdf"},
            }
        ]
    }
    monkeypatch.setattr(economic.requests, "get", lambda url, **kw: FakeResponse(_json=payload))

    invoices = economic.get_booked_invoices(1)

    assert invoices == [
        economic.BookedInvoice(
            booked_invoice_number=1,
            date="2022-06-02",
            due_date="2022-06-10",
            remainder=0.0,
            pdf_url="https://restapi.e-conomic.com/invoices/booked/1/pdf",
        )
    ]


def test_get_booked_invoices_empty_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(economic.requests, "get", lambda url, **kw: FakeResponse(_json={"collection": []}))

    assert economic.get_booked_invoices(1) == []


def test_get_invoice_pdf_returns_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(economic.requests, "get", lambda url, **kw: FakeResponse(content=b"%PDF-1.7 fake"))

    assert economic.get_invoice_pdf("https://restapi.e-conomic.com/invoices/booked/1/pdf") == b"%PDF-1.7 fake"
