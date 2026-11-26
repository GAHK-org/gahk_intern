"""What regnskab leaves in the audit trail: the alumneregning settings, and each run of the send."""

from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse

from audit.models import AuditEntity
from core.models import Room
from regnskab import alumneregning, economic, tasks
from regnskab.models import AlumneregningSend, AlumneregningSettings, EconomicLedgerEntry
from residents.models import Residency, Resident
from tests.audit_support import actions, changes, events

pytestmark = pytest.mark.django_db

SETTINGS_URL = "regnskab:alumneregning_settings"


@pytest.fixture
def regnskab(make_resident: Callable[..., Resident]) -> Client:
    client = Client()
    client.force_login(make_resident(email="acc@gahk.dk", first_name="Regn", roles=("regnskab",)))
    return client


def test_opening_the_settings_writes_nothing(regnskab: Client) -> None:
    assert regnskab.get(reverse(SETTINGS_URL)).status_code == 200

    assert not AlumneregningSettings.objects.exists()
    assert actions("regnskab") == []


def test_editing_the_mail_text_is_recorded_with_before_and_after(regnskab: Client) -> None:
    regnskab.post(reverse(SETTINGS_URL), {"message_template": "Hej $FORNAVN"})
    regnskab.post(reverse(SETTINGS_URL), {"message_template": "Kære $FORNAVN"})
    regnskab.post(reverse(SETTINGS_URL), {"message_template": "Kære $FORNAVN"})  # saved as it was

    first, second = events("regnskab.alumneregning_settings.updated")
    assert second.actor_name == "Regn Beboer"
    assert changes(first)["message_template"] == [None, "Hej $FORNAVN"]
    assert changes(second) == {"message_template": ["Hej $FORNAVN", "Kære $FORNAVN"]}


def _two_alumner(monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident]) -> list[Resident]:
    monkeypatch.setattr(economic, "get_account", lambda n: economic.EconomicAccount(n, "Alumne", 0.0))
    monkeypatch.setattr(economic, "get_posted_entries_in_range", lambda n, f, t: [])
    monkeypatch.setattr(
        economic,
        "get_draft_entries_for_account",
        lambda n: [
            economic.LedgerEntry(
                entry_number=n,
                is_draft=True,
                date="2026-08-02",
                text="Udkast",
                amount=10.0,
                entry_type="financeVoucher",
                voucher_number=1,
                due_date=None,
            )
        ],
    )
    # Anna's account is named for someone else, so she gets no statement.
    monkeypatch.setattr(
        alumneregning, "account_name_matches", lambda resident, name: resident.first_name != "Anna"
    )
    alumner = []
    for i, name in enumerate(["Anna", "Bo"], start=1):
        alumne = make_resident(email=f"{name.lower()}@gahk.dk", first_name=name, economic_account_number=i)
        room = Room.objects.create(legacy_index=i, number=i, floor="stuen", side="mod gaden")
        Residency.objects.create(resident=alumne, room=room, year=2026, month=8)
        alumner.append(alumne)
    return alumner


def test_each_run_of_the_send_says_whom_it_billed_and_whom_not(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident]
) -> None:
    anna, bo = _two_alumner(monkeypatch, make_resident)

    with pytest.raises(tasks.AlumneregningerFailed):
        tasks.send_alumneregninger_task.apply(args=("2026-08-01", "2026-08-31"), throw=True)

    [run] = events("regnskab.alumneregninger.made")
    assert run.actor_id is None
    assert run.source == "worker"
    assert run.details == {"date_from": "2026-08-01", "date_to": "2026-08-31", "made": 1, "failed": 1}
    roles = {e.entity_id: e.role for e in run.entities.all()}
    assert roles == {anna.pk: "failed", bo.pk: "billed"}
    # The sends, the mirrored ledger and the mails are bookkeeping: nothing of them in the trail.
    assert AlumneregningSend.objects.count() == 2
    assert EconomicLedgerEntry.objects.exists()
    assert set(AuditEntity.objects.values_list("kind", flat=True)) == {"residents.resident"}


def test_a_run_with_nobody_left_to_bill_records_nothing(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident]
) -> None:
    monkeypatch.setattr(alumneregning, "send_alumneregninger", lambda date_from, date_to: [])

    assert tasks.send_alumneregninger_task("2026-08-01", "2026-08-31") == 0

    assert actions() == []
