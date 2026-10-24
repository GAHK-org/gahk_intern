"""Regnskab (accounting) role: gated settlement view of moved-out residents' AK + ØK status (F-010)."""

from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ak.models import AkEntry
from core.models import Room, Workgroup
from oelkaelder.models import Deposit, Shopper
from regnskab import economic
from residents.models import Residency, RoleAssignment, prev_period
from residents.views import _sync_month_roles


def _setup_leaver_and_stayer(make_resident: Callable) -> None:
    """A leaver (present last month, gone this month) with AK debt + ØK balance, plus a current stayer."""
    now = timezone.localtime()
    cy, cm = now.year, now.month
    py, pm = prev_period((cy, cm))

    stayer = make_resident(email="stay@gahk.dk", first_name="Sta", last_name="Yer")
    Residency.objects.create(
        resident=stayer,
        year=cy,
        month=cm,
        room=Room.objects.create(legacy_index=2, number=4, floor="stuen", side="mod gården"),
    )

    leaver = make_resident(email="leaver@gahk.dk", first_name="Lea", last_name="Ver")
    Residency.objects.create(
        resident=leaver,
        year=py,
        month=pm,
        room=Room.objects.create(legacy_index=1, number=3, floor="stuen", side="mod gaden"),
    )
    AkEntry.objects.create(resident=leaver, delta=-4)  # owes 4 krydser
    for amount in (5000, 2000):  # two accounts → 70,00 kr total, exercises the cross-account sum
        Deposit.objects.create(shopper=Shopper.objects.create(resident=leaver), amount_ore=amount)


def _mock_entries(
    monkeypatch: pytest.MonkeyPatch,
    posted: list[economic.LedgerEntry] | None = None,
    drafts: list[economic.LedgerEntry] | None = None,
) -> None:
    monkeypatch.setattr(economic, "get_posted_entries_in_range", lambda n, f, t: posted or [])
    monkeypatch.setattr(economic, "get_draft_entries_for_account", lambda n: drafts or [])


@pytest.mark.django_db
def test_leavers_show_ak_and_oek_status(make_resident: Callable) -> None:
    _setup_leaver_and_stayer(make_resident)
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    html = c.get(reverse("regnskab:overview")).content.decode()

    assert "Lea Ver" in html  # the leaver is listed
    assert "Sta Yer" not in html  # a current resident is NOT a leaver
    assert "-4 krydser" in html  # AK shown as raw krydser
    assert "70,00 kr" in html  # ØK balance summed across both accounts
    assert "003" in html  # prior-month room number


@pytest.mark.django_db
def test_lookup_finds_any_resident(make_resident: Callable) -> None:
    _setup_leaver_and_stayer(make_resident)
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    # The stayer is not a leaver, so finding them proves the free lookup works.
    html = c.get(reverse("regnskab:overview"), {"q": "Sta"}).content.decode()
    assert "Sta Yer" in html


@pytest.mark.django_db
def test_view_is_regnskab_gated(make_resident: Callable) -> None:
    plain = make_resident(email="plain@gahk.dk")
    admin = make_resident(email="admin@gahk.dk", roles=("administrator",))

    plain_c = Client()
    plain_c.force_login(plain)
    assert plain_c.get(reverse("regnskab:overview")).status_code == 403

    admin_c = Client()
    admin_c.force_login(admin)  # administrator implies every role
    assert admin_c.get(reverse("regnskab:overview")).status_code == 200


@pytest.mark.django_db
def test_set_economic_account_number_updates_resident(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    target = make_resident(email="target@gahk.dk")
    c = Client()
    c.force_login(acc)

    response = c.post(
        reverse("regnskab:set_economic_account_number", args=[target.pk]),
        {"economic_account_number": "1101", "next": reverse("regnskab:overview")},
    )

    target.refresh_from_db()
    assert target.economic_account_number == 1101
    assert response.status_code == 302
    assert response.url == reverse("regnskab:overview")


@pytest.mark.django_db
def test_set_economic_account_number_can_be_cleared(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    target = make_resident(email="target@gahk.dk", economic_account_number=1101)
    c = Client()
    c.force_login(acc)

    c.post(reverse("regnskab:set_economic_account_number", args=[target.pk]), {"economic_account_number": ""})

    target.refresh_from_db()
    assert target.economic_account_number is None


@pytest.mark.django_db
def test_set_economic_account_number_requires_regnskab_role(make_resident: Callable) -> None:
    plain = make_resident(email="plain@gahk.dk")
    target = make_resident(email="target@gahk.dk")
    c = Client()
    c.force_login(plain)

    response = c.post(
        reverse("regnskab:set_economic_account_number", args=[target.pk]),
        {"economic_account_number": "1101"},
    )

    assert response.status_code == 403
    target.refresh_from_db()
    assert target.economic_account_number is None


@pytest.mark.django_db
def test_set_economic_account_number_rejects_get(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    target = make_resident(email="target@gahk.dk")
    c = Client()
    c.force_login(acc)

    assert c.get(reverse("regnskab:set_economic_account_number", args=[target.pk])).status_code == 405


@pytest.mark.django_db
def test_economic_account_number_not_editable_via_django_admin_by_regnskab_alone(
    make_resident: Callable,
) -> None:
    """A plain regnskab-role user must not reach the Django Admin change form for a Resident."""
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    target = make_resident(email="target@gahk.dk", economic_account_number=4207)
    c = Client()
    c.force_login(acc)

    response = c.get(f"/django-admin/residents/resident/{target.pk}/change/")

    assert response.status_code != 200


@pytest.mark.django_db
def test_economic_account_number_editable_via_django_admin_by_approved_administrator(
    make_resident: Callable,
) -> None:
    from residents.models import AdminAccessGrant

    admin = make_resident(email="admin@gahk.dk", roles=("administrator",))
    AdminAccessGrant.objects.filter(resident=admin).update(status=AdminAccessGrant.Status.APPROVED)
    target = make_resident(email="target@gahk.dk", economic_account_number=4207)
    c = Client()
    c.force_login(admin)

    response = c.get(f"/django-admin/residents/resident/{target.pk}/change/")

    assert response.status_code == 200


@pytest.mark.django_db
def test_send_alumneregninger_requires_regnskab_role(make_resident: Callable) -> None:
    plain = make_resident(email="plain@gahk.dk")
    c = Client()
    c.force_login(plain)

    assert c.post(reverse("regnskab:send_alumneregninger")).status_code == 403


@pytest.mark.django_db
def test_send_alumneregninger_rejects_get(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    assert c.get(reverse("regnskab:send_alumneregninger")).status_code == 405


@pytest.mark.django_db
def test_send_alumneregninger_queues_the_task_and_the_status_page_shows_the_result(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    """The send is a background task (Celery), not inline in the request — the POST just queues it and
    redirects to the status page. CELERY_TASK_ALWAYS_EAGER (test settings) runs the task inline, so by
    the time the redirect is followed the result is already there."""
    alumne = make_resident(
        email="alumne@gahk.dk", economic_account_number=1101, first_name="Alu", last_name="Mne"
    )
    Residency.objects.create(
        resident=alumne,
        year=2026,
        month=8,
        room=Room.objects.create(legacy_index=1, number=1, floor="stuen", side="mod gaden"),
    )
    # Created AFTER the Residency above: active_period() picks the most recent Residency's (year,
    # month) over today's real calendar month once one exists, so "acc"'s role grant must be made
    # for whatever active_period() resolves to at that point, not whatever it was before.
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    monkeypatch.setattr(economic, "get_account", lambda n: economic.EconomicAccount(n, "x", 600.0))
    _mock_entries(monkeypatch)  # nothing posted this period
    c = Client()
    c.force_login(acc)

    response = c.post(reverse("regnskab:send_alumneregninger"), {"fra": "2026-08-01", "til": "2026-08-31"})
    assert response.status_code == 302
    html = c.get(response.url).content.decode()

    assert "Alu Mne" in html
    assert "Sendt" in html
    assert "videreført" in html


@pytest.mark.django_db
def test_send_alumneregninger_auth_error_does_not_crash_the_request(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    """An invalid/revoked e-conomic connection is handled inside the task (tasks.py), not the view —
    there is no HTTP response left for a background job to show an error on. The POST itself always
    just queues and redirects; the status page simply shows nobody processed yet."""
    alumne = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    Residency.objects.create(
        resident=alumne,
        year=2026,
        month=8,
        room=Room.objects.create(legacy_index=1, number=1, floor="stuen", side="mod gaden"),
    )
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))  # see note above: created after

    def get_account(account_number: int) -> economic.EconomicAccount:
        raise economic.EconomicAuthError("token revoked")

    monkeypatch.setattr(economic, "get_account", get_account)
    c = Client()
    c.force_login(acc)

    response = c.post(reverse("regnskab:send_alumneregninger"), {"fra": "2026-08-01", "til": "2026-08-31"})

    assert response.status_code == 302  # queuing always succeeds, whatever the task later does
    html = c.get(response.url).content.decode()
    assert "Venter" in html  # nobody was actually processed


@pytest.mark.django_db
def test_send_alumneregninger_rejects_missing_period(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    response = c.post(reverse("regnskab:send_alumneregninger"), {"fra": "2026-08-01"})  # no "til"

    assert response.status_code == 302
    assert response.url == reverse("regnskab:overview")


@pytest.mark.django_db
def test_send_alumneregninger_rejects_fra_after_til(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    response = c.post(reverse("regnskab:send_alumneregninger"), {"fra": "2026-08-31", "til": "2026-08-01"})

    assert response.status_code == 302
    assert response.url == reverse("regnskab:overview")


@pytest.mark.django_db
def test_status_rejects_invalid_period(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    response = c.get(reverse("regnskab:alumneregninger_status"), {"fra": "2026-08-31", "til": "2026-08-01"})

    assert response.status_code == 302
    assert response.url == reverse("regnskab:overview")


@pytest.mark.django_db
def test_preview_requires_regnskab_role(make_resident: Callable) -> None:
    plain = make_resident(email="plain@gahk.dk")
    c = Client()
    c.force_login(plain)

    response = c.get(reverse("regnskab:preview_alumneregninger"), {"fra": "2026-08-01", "til": "2026-08-31"})

    assert response.status_code == 403


@pytest.mark.django_db
def test_preview_renders_rows_and_pdf_link(monkeypatch: pytest.MonkeyPatch, make_resident: Callable) -> None:
    alumne = make_resident(
        email="alumne@gahk.dk", economic_account_number=1101, first_name="Alu", last_name="Mne"
    )
    Residency.objects.create(
        resident=alumne,
        year=2026,
        month=8,
        room=Room.objects.create(legacy_index=1, number=1, floor="stuen", side="mod gaden"),
    )
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))  # created after, see note above
    monkeypatch.setattr(economic, "get_account", lambda n: economic.EconomicAccount(n, "x", 100.0))
    _mock_entries(
        monkeypatch,
        posted=[
            economic.LedgerEntry(
                entry_number=1,
                is_draft=False,
                date="2026-08-01",
                text="Husleje",
                amount=100.0,
                entry_type="financeVoucher",
                voucher_number=1,
                due_date=None,
            )
        ],
    )
    c = Client()
    c.force_login(acc)

    html = c.get(
        reverse("regnskab:preview_alumneregninger"), {"fra": "2026-08-01", "til": "2026-08-31"}
    ).content.decode()

    assert "Alu Mne" in html
    assert "Vil blive sendt" in html
    assert "Bekræft og send" in html
    pdf_url = (
        f"{reverse('regnskab:alumneregning_statement_pdf', args=[alumne.pk])}?fra=2026-08-01&til=2026-08-31"
    )
    assert pdf_url in html  # ISO dates, not Danish-locale-formatted — the link must actually resolve
    assert c.get(pdf_url)["Content-Type"] == "application/pdf"


@pytest.mark.django_db
def test_preview_rejects_invalid_period(make_resident: Callable) -> None:
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    response = c.get(reverse("regnskab:preview_alumneregninger"), {"fra": "2026-08-31", "til": "2026-08-01"})

    assert response.status_code == 302
    assert response.url == reverse("regnskab:overview")


@pytest.mark.django_db
def test_statement_pdf_view_returns_pdf(monkeypatch: pytest.MonkeyPatch, make_resident: Callable) -> None:
    alumne = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    monkeypatch.setattr(economic, "get_account", lambda n: economic.EconomicAccount(n, "x", 100.0))
    _mock_entries(
        monkeypatch,
        posted=[
            economic.LedgerEntry(
                entry_number=1,
                is_draft=False,
                date="2026-08-01",
                text="Husleje",
                amount=100.0,
                entry_type="financeVoucher",
                voucher_number=1,
                due_date=None,
            )
        ],
    )
    c = Client()
    c.force_login(acc)

    response = c.get(
        reverse("regnskab:alumneregning_statement_pdf", args=[alumne.pk]),
        {"fra": "2026-08-01", "til": "2026-08-31"},
    )

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")


@pytest.mark.django_db
def test_statement_pdf_view_works_with_no_entries_in_range(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable
) -> None:
    """No postings in the chosen range is not a 404 — the account still has a balance to show."""
    alumne = make_resident(email="alumne@gahk.dk", economic_account_number=1101)
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    monkeypatch.setattr(economic, "get_account", lambda n: economic.EconomicAccount(n, "x", 600.0))
    _mock_entries(monkeypatch)
    c = Client()
    c.force_login(acc)

    response = c.get(
        reverse("regnskab:alumneregning_statement_pdf", args=[alumne.pk]),
        {"fra": "2026-08-01", "til": "2026-08-31"},
    )

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")


@pytest.mark.django_db
def test_statement_pdf_view_404s_without_account_number(make_resident: Callable) -> None:
    alumne = make_resident(email="alumne@gahk.dk")  # no economic_account_number set
    acc = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    c = Client()
    c.force_login(acc)

    response = c.get(
        reverse("regnskab:alumneregning_statement_pdf", args=[alumne.pk]),
        {"fra": "2026-08-01", "til": "2026-08-31"},
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_regnskab_workgroup_grants_role(make_resident: Callable) -> None:
    """Being placed in the 'Regnskab' embedsgruppe auto-grants the regnskab role for that month."""
    r = make_resident(email="d@gahk.dk")
    wg, _ = Workgroup.objects.get_or_create(name="Regnskabsgruppen")  # created by migration core.0003

    _sync_month_roles(r.id, wg, 2026, 8, is_admin=False)

    assert RoleAssignment.objects.filter(resident=r, role="regnskab", year=2026, month=8).exists()
