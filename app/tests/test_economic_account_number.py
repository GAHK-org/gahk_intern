"""Regnskab sets a resident's e-conomic kontonr. on its own page, checked against the account's name in
e-conomic, and alumneregninger refuse an account named for someone else (F-010 regnskab role)."""

from collections.abc import Callable
from datetime import date

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse

from core.models import Room
from regnskab import alumneregning, economic
from residents.models import Residency, Resident

AUGUST = (date(2026, 8, 1), date(2026, 8, 31))


def _accounts(monkeypatch: pytest.MonkeyPatch, names: dict[int, str]) -> None:
    """e-conomic's chart of accounts, by number; any other number is a 404 like the real API."""

    def get_account(account_number: int) -> economic.EconomicAccount:
        if account_number not in names:
            raise economic.EconomicApiError("not found", status_code=404)
        return economic.EconomicAccount(account_number, names[account_number], 100.0)

    monkeypatch.setattr(economic, "get_account", get_account)
    monkeypatch.setattr(economic, "get_posted_entries_in_range", lambda n, f, t: [])
    monkeypatch.setattr(economic, "get_draft_entries_for_account", lambda n: [])


def _regnskab_client(make_resident: Callable) -> Client:
    c = Client()
    c.force_login(make_resident(email="acc@gahk.dk", roles=("regnskab",)))
    return c


def _anna(make_resident: Callable, number: int | None = None) -> Resident:
    return make_resident(
        email="anna@gahk.dk", first_name="Anna", last_name="Jensen", economic_account_number=number
    )


@pytest.mark.parametrize(
    ("first", "last", "account_name", "matches"),
    [
        ("Anna", "Jensen", "Anna Jensen", True),
        ("anna", "jensen", "ANNA JENSEN", True),
        ("Anna", "Jensen", "Anna Marie Jensen", True),
        ("Anne-Sofie", "Holm", "Anne-Sofie Holm", True),
        ("Anna", "Jensen", "Anna Hansen", False),
        ("Anna", "Jensen", "Ny alumne", False),
        ("Anna", "Jensen", "", False),
    ],
)
def test_account_name_matches(first: str, last: str, account_name: str, matches: bool) -> None:
    resident = Resident(first_name=first, last_name=last)
    assert alumneregning.account_name_matches(resident, account_name) is matches


@pytest.mark.django_db
def test_the_page_shows_the_resident_and_the_account_name_in_e_conomic(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    anna = _anna(make_resident, 4207)
    _accounts(monkeypatch, {4207: "Anna Jensen"})

    html = (
        _regnskab_client(make_resident)
        .get(reverse("regnskab:edit_economic_account_number", args=[anna.pk]))
        .content.decode()
    )

    assert "anna@gahk.dk" in html
    assert "Hedder i e-conomic: <strong>Anna Jensen</strong>" in html
    assert 'name="first_name"' not in html  # only the kontonr. is editable


@pytest.mark.django_db
def test_a_number_whose_account_has_the_residents_name_is_saved(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    anna = _anna(make_resident)
    _accounts(monkeypatch, {4207: "Anna Jensen"})
    nxt = f"{reverse('regnskab:overview')}?period=2026-8"

    response = _regnskab_client(make_resident).post(
        f"{reverse('regnskab:edit_economic_account_number', args=[anna.pk])}?next={nxt}",
        {"economic_account_number": "4207"},
    )

    assert response.status_code == 302
    assert response.url == nxt
    anna.refresh_from_db()
    assert anna.economic_account_number == 4207


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("number", "error"),
    [
        (4208, "Kontonr. 4208 hedder &quot;Bo Hansen&quot; i e-conomic, ikke Anna Jensen."),
        (9999, "Kontonr. 9999 findes ikke i e-conomic."),
    ],
)
def test_a_number_for_someone_else_or_nobody_is_not_saved(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable, number: int, error: str
) -> None:
    anna = _anna(make_resident, 4207)
    _accounts(monkeypatch, {4207: "Anna Jensen", 4208: "Bo Hansen"})

    response = _regnskab_client(make_resident).post(
        reverse("regnskab:edit_economic_account_number", args=[anna.pk]),
        {"economic_account_number": str(number)},
    )

    assert response.status_code == 200
    assert error in response.content.decode()
    anna.refresh_from_db()
    assert anna.economic_account_number == 4207


@pytest.mark.django_db
def test_the_number_can_be_cleared_without_asking_e_conomic(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    anna = _anna(make_resident, 4207)
    monkeypatch.setattr(economic, "get_account", lambda n: pytest.fail("looked up an empty number"))

    _regnskab_client(make_resident).post(
        reverse("regnskab:edit_economic_account_number", args=[anna.pk]), {"economic_account_number": ""}
    )

    anna.refresh_from_db()
    assert anna.economic_account_number is None


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "post"])
def test_the_page_requires_the_regnskab_role(make_resident: Callable, method: str) -> None:
    anna = _anna(make_resident)
    c = Client()
    c.force_login(make_resident(email="plain@gahk.dk"))

    url = reverse("regnskab:edit_economic_account_number", args=[anna.pk])
    response = getattr(c, method)(url, {"economic_account_number": "4207"} if method == "post" else {})

    assert response.status_code == 403
    anna.refresh_from_db()
    assert anna.economic_account_number is None


def _anna_in_august(make_resident: Callable, number: int) -> Resident:
    anna = _anna(make_resident, number)
    room = Room.objects.create(legacy_index=1, number=1, floor="stuen", side="mod gaden")
    Residency.objects.create(resident=anna, room=room, year=2026, month=8)
    return anna


@pytest.mark.django_db
def test_the_preview_fails_an_account_named_for_someone_else(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    _anna_in_august(make_resident, 4208)
    _accounts(monkeypatch, {4208: "Bo Hansen"})

    [row] = alumneregning.preview_alumneregninger(*AUGUST)

    assert row.status == "failed"
    assert 'hedder "Bo Hansen" i e-conomic' in row.detail


@pytest.mark.django_db
def test_no_alumneregning_is_sent_for_an_account_named_for_someone_else(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    _anna_in_august(make_resident, 4208)
    _accounts(monkeypatch, {4208: "Bo Hansen"})

    [result] = alumneregning.send_alumneregninger(*AUGUST)

    assert result.outcome == "failed"
    assert 'hedder "Bo Hansen" i e-conomic' in result.detail
    assert not mail.outbox


@pytest.mark.django_db
def test_an_alumneregning_is_sent_when_the_names_match(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    _anna_in_august(make_resident, 4207)
    _accounts(monkeypatch, {4207: "Anna Jensen"})

    [result] = alumneregning.send_alumneregninger(*AUGUST)

    assert result.outcome == "sent"
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_with_send_mail_off_nothing_is_mailed_and_the_status_says_so(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable, settings: object
) -> None:
    """Locally (DEBUG) alumneregninger only go to the console, whatever EMAIL_BACKEND the .env sets."""
    settings.ALUMNEREGNING_SEND_MAIL = False  # type: ignore[attr-defined]
    _anna_in_august(make_resident, 4207)
    _accounts(monkeypatch, {4207: "Anna Jensen"})

    [result] = alumneregning.send_alumneregninger(*AUGUST)

    assert not mail.outbox
    assert "Ikke mailet" in result.detail
