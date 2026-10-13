"""Reparationer — reporting a repair, and the two crews who move it through the board.

The shape of the feature is two axes that move independently (reparationer/models.py): `status` says
where a ticket is in the pipeline, `responsible` says which crew is holding it. Almost everything
worth testing is a rule about who may change which axis, so that is what most of this file is:

  * a beboer may report and comment, and nothing else;
  * a Vicevært may move a ticket anywhere except the two manager-only columns;
  * Reppergruppen/Inspektionen/administrator may move it anywhere, close it, and delete it;
  * either crew may hand a ticket to the other, in either direction.

The board's drag-and-drop posts to the same set_status view the detail page's buttons do, with an
XMLHttpRequest header — so the two cases share the rules and differ only in what comes back
(views._move_response). Both are covered here; the dragging itself lives in
frontend/src/reparationer.ts and is not testable from Python.
"""

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from core import push
from core.models import PushSubscription
from reparationer.models import ARCHIVE_AFTER_DAYS, RepairComment, RepairTask
from residents.models import WORKGROUP_ROLE, Resident, Role

BOARD = "/intern/reparationer/"
pytestmark = pytest.mark.django_db


@pytest.fixture
def beboer(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="beboer@gahk.dk", first_name="Bo", last_name="Beboer")


@pytest.fixture
def vicevaert(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="vice@gahk.dk", first_name="Vig", roles=(Role.VICEVAERT,))


@pytest.fixture
def repper(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="repper@gahk.dk", first_name="Rep", roles=(Role.REPPER,))


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch, settings: object) -> list[tuple[list[int], dict]]:
    """Records (recipient user ids, payload) for every notification the code would send —
    same fixture as tests/test_opslagstavle.py, which tests the shared transport."""
    settings.VAPID_PUBLIC_KEY = "test-public-key"  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = "test-private-key"  # type: ignore[attr-defined]
    recorded: list[tuple[list[int], dict]] = []

    def fake_dispatch(subscriptions: object, payload: dict) -> int:
        ids = sorted(subscriptions.values_list("user_id", flat=True))  # type: ignore[attr-defined]
        recorded.append((ids, payload))
        return len(ids)

    monkeypatch.setattr(push, "_dispatch", fake_dispatch)
    monkeypatch.setattr(push, "_run_in_background", lambda fn: fn())
    return recorded


def subscribe(user: Resident, endpoint: str) -> PushSubscription:
    return PushSubscription.objects.create(
        user=user, endpoint=endpoint, auth="a" * 22, p256dh="p" * 87, wants_reparationer=True
    )


def make_task(reporter: Resident, **kwargs: object) -> RepairTask:
    return RepairTask.objects.create(
        reported_by=reporter,
        title=kwargs.pop("title", "Utæt vandhane"),  # type: ignore[arg-type]
        **kwargs,
    )


def login(client: Client, user: Resident) -> Client:
    client.force_login(user)
    return client


# --------------------------------------------------------------------------------- reporting


def test_any_resident_can_report_a_repair(client: Client, beboer: Resident) -> None:
    login(client, beboer)
    response = client.post(
        f"{BOARD}opret", {"title": "Utæt vandhane", "location": "Hallen", "description": "Drypper."}
    )
    assert response.status_code == 302
    task = RepairTask.objects.get()
    assert task.reported_by == beboer
    # A new ticket lands on Viceværterne's desk, at the start of the pipeline.
    assert task.status == RepairTask.Status.NY
    assert task.responsible == RepairTask.Responsible.VICEVAERT


def test_reporting_notifies_the_vicevaerter_but_not_the_reporter(
    client: Client, beboer: Resident, vicevaert: Resident, repper: Resident, pushes: list
) -> None:
    subscribe(beboer, "https://push.example/beboer")
    subscribe(vicevaert, "https://push.example/vice")
    subscribe(repper, "https://push.example/repper")
    login(client, beboer)

    client.post(f"{BOARD}opret", {"title": "Utæt vandhane", "location": "", "description": ""})

    assert len(pushes) == 1
    recipients, payload = pushes[0]
    assert recipients == [vicevaert.id]
    assert "Ny reparation" in payload["head"]


def test_the_board_lists_open_tickets_and_the_login_gate_holds(client: Client, beboer: Resident) -> None:
    make_task(beboer, title="Utæt vandhane")
    assert client.get(BOARD).status_code == 302  # anonymous -> login
    login(client, beboer)
    assert "Utæt vandhane" in client.get(BOARD).content.decode()


# ------------------------------------------------------------------------------ moving a ticket


def test_a_plain_resident_cannot_move_a_ticket(client: Client, beboer: Resident) -> None:
    task = make_task(beboer)
    login(client, beboer)
    assert client.post(f"{BOARD}{task.pk}/status", {"status": "i_gang"}).status_code == 403
    task.refresh_from_db()
    assert task.status == RepairTask.Status.NY


def test_a_vicevaert_moves_a_ticket_through_the_triage_columns(
    client: Client, beboer: Resident, vicevaert: Resident
) -> None:
    task = make_task(beboer)
    login(client, vicevaert)
    client.post(f"{BOARD}{task.pk}/status", {"status": "i_gang"})
    task.refresh_from_db()
    assert task.status == RepairTask.Status.I_GANG


@pytest.mark.parametrize("status", ["ak_projekt", "faerdig"])
def test_a_vicevaert_cannot_move_a_ticket_into_a_manager_only_column(
    client: Client, beboer: Resident, vicevaert: Resident, status: str
) -> None:
    """Committing AK hours, and closing a ticket, are Reppergruppen's calls — see
    views.MANAGER_ONLY_STATUSES. The ticket does not move."""
    task = make_task(beboer, status=RepairTask.Status.I_GANG)
    login(client, vicevaert)
    client.post(f"{BOARD}{task.pk}/status", {"status": status})
    task.refresh_from_db()
    assert task.status == RepairTask.Status.I_GANG


@pytest.mark.parametrize("status", ["ak_projekt", "faerdig"])
def test_a_manager_may_use_every_column(
    client: Client, beboer: Resident, repper: Resident, status: str
) -> None:
    task = make_task(beboer, status=RepairTask.Status.I_GANG)
    login(client, repper)
    client.post(f"{BOARD}{task.pk}/status", {"status": status})
    task.refresh_from_db()
    assert task.status == status


def test_an_unknown_status_leaves_the_ticket_alone(
    client: Client, beboer: Resident, repper: Resident
) -> None:
    task = make_task(beboer)
    login(client, repper)
    client.post(f"{BOARD}{task.pk}/status", {"status": "noget-andet"})
    task.refresh_from_db()
    assert task.status == RepairTask.Status.NY


# ------------------------------------------------------- dragging a card (the board's move control)


def drag(client: Client, task: RepairTask, status: str) -> dict:
    """What frontend/src/reparationer.ts sends when a card is dropped in another column."""
    response = client.post(
        f"{BOARD}{task.pk}/status", {"status": status}, headers={"x-requested-with": "XMLHttpRequest"}
    )
    assert response.status_code == 200  # JSON, never a redirect the fetch would have to follow
    return response.json()


def test_a_drag_moves_the_ticket_and_answers_in_json(
    client: Client, beboer: Resident, repper: Resident
) -> None:
    task = make_task(beboer)
    login(client, repper)
    body = drag(client, task, "faerdig")
    assert body["ok"] is True
    assert body["status"] == "faerdig"
    task.refresh_from_db()
    assert task.status == RepairTask.Status.FAERDIG


def test_a_refused_drag_says_why_and_reports_the_unchanged_status(
    client: Client, beboer: Resident, vicevaert: Resident
) -> None:
    """The card is put back on the strength of this answer, so the refusal has to carry both a
    reason to show and the status the ticket really has."""
    task = make_task(beboer, status=RepairTask.Status.I_GANG)
    login(client, vicevaert)
    body = drag(client, task, "faerdig")
    assert body["ok"] is False
    assert body["error"]
    assert body["status"] == "i_gang"
    task.refresh_from_db()
    assert task.status == RepairTask.Status.I_GANG


def test_the_board_only_arms_dragging_for_someone_who_may_move(
    client: Client, beboer: Resident, vicevaert: Resident
) -> None:
    make_task(beboer)
    login(client, beboer)
    assert 'data-can-move="0"' in client.get(BOARD).content.decode()
    login(client, vicevaert)
    page = client.get(BOARD).content.decode()
    assert 'data-can-move="1"' in page
    assert 'data-can-manage="0"' in page  # ... but not into the manager-only columns


# ------------------------------------------------------------------------------------- handoff


def test_a_vicevaert_hands_a_ticket_to_reppergruppen(
    client: Client, beboer: Resident, vicevaert: Resident, repper: Resident, pushes: list
) -> None:
    subscribe(repper, "https://push.example/repper")
    task = make_task(beboer)
    login(client, vicevaert)

    client.post(f"{BOARD}{task.pk}/ansvarlig", {"responsible": "repper"})

    task.refresh_from_db()
    assert task.responsible == RepairTask.Responsible.REPPER
    assert [ids for ids, _ in pushes] == [[repper.id]]


def test_a_ticket_can_be_handed_back_to_the_vicevaerter(
    client: Client, beboer: Resident, vicevaert: Resident, repper: Resident, pushes: list
) -> None:
    """The reverse of the handoff above: a ticket that turns out not to need Reppergruppen goes
    back where it came from, and the crew inheriting it is the one that gets told."""
    subscribe(vicevaert, "https://push.example/vice")
    subscribe(repper, "https://push.example/repper")
    task = make_task(beboer, responsible=RepairTask.Responsible.REPPER)
    login(client, repper)

    client.post(f"{BOARD}{task.pk}/ansvarlig", {"responsible": "vicevaert"})

    task.refresh_from_db()
    assert task.responsible == RepairTask.Responsible.VICEVAERT
    assert [ids for ids, _ in pushes] == [[vicevaert.id]]


def test_handing_a_ticket_where_it_already_is_changes_and_notifies_nothing(
    client: Client, beboer: Resident, vicevaert: Resident, repper: Resident, pushes: list
) -> None:
    subscribe(repper, "https://push.example/repper")
    task = make_task(beboer, responsible=RepairTask.Responsible.REPPER)
    login(client, vicevaert)

    client.post(f"{BOARD}{task.pk}/ansvarlig", {"responsible": "repper"})

    task.refresh_from_db()
    assert task.responsible == RepairTask.Responsible.REPPER
    assert pushes == []


def test_an_unknown_responsible_leaves_the_ticket_alone(
    client: Client, beboer: Resident, vicevaert: Resident
) -> None:
    task = make_task(beboer)
    login(client, vicevaert)
    client.post(f"{BOARD}{task.pk}/ansvarlig", {"responsible": "hvem-som-helst"})
    task.refresh_from_db()
    assert task.responsible == RepairTask.Responsible.VICEVAERT


def test_a_plain_resident_cannot_hand_a_ticket_over(client: Client, beboer: Resident) -> None:
    task = make_task(beboer)
    login(client, beboer)
    assert client.post(f"{BOARD}{task.pk}/ansvarlig", {"responsible": "repper"}).status_code == 403
    task.refresh_from_db()
    assert task.responsible == RepairTask.Responsible.VICEVAERT


def test_the_detail_page_offers_the_handoff_in_whichever_direction_is_left(
    client: Client, beboer: Resident, vicevaert: Resident
) -> None:
    task = make_task(beboer)
    login(client, vicevaert)
    assert "Overdrag til Repper" in client.get(f"{BOARD}{task.pk}").content.decode()

    task.responsible = RepairTask.Responsible.REPPER
    task.save(update_fields=["responsible"])
    assert "Send tilbage til Viceværterne" in client.get(f"{BOARD}{task.pk}").content.decode()


# ------------------------------------------------------------------------------------- comments


def test_any_resident_may_comment_and_delete_their_own_note(
    client: Client, beboer: Resident, vicevaert: Resident
) -> None:
    task = make_task(vicevaert)
    login(client, beboer)
    client.post(f"{BOARD}{task.pk}/kommentar", {"body": "Det drypper stadig."})
    comment = RepairComment.objects.get()
    assert comment.author == beboer

    client.post(f"{BOARD}kommentar/{comment.pk}/slet")
    assert not RepairComment.objects.exists()


def test_a_resident_cannot_delete_someone_elses_note(
    client: Client, beboer: Resident, vicevaert: Resident
) -> None:
    task = make_task(vicevaert)
    comment = RepairComment.objects.create(task=task, author=vicevaert, body="Kigger på det.")
    login(client, beboer)
    assert client.post(f"{BOARD}kommentar/{comment.pk}/slet").status_code == 403
    assert RepairComment.objects.exists()


# -------------------------------------------------------------------------- deleting / archiving


def test_only_a_manager_may_delete_a_ticket(
    client: Client, beboer: Resident, vicevaert: Resident, repper: Resident
) -> None:
    task = make_task(beboer)
    login(client, vicevaert)
    assert client.post(f"{BOARD}{task.pk}/slet").status_code == 403
    assert RepairTask.objects.filter(pk=task.pk).exists()

    login(client, repper)
    client.post(f"{BOARD}{task.pk}/slet")
    assert not RepairTask.objects.filter(pk=task.pk).exists()


def test_the_nightly_sweep_archives_only_long_finished_tickets(client: Client, beboer: Resident) -> None:
    old = make_task(beboer, title="Gammel", status=RepairTask.Status.FAERDIG)
    fresh = make_task(beboer, title="Frisk", status=RepairTask.Status.FAERDIG)
    open_one = make_task(beboer, title="Åben", status=RepairTask.Status.I_GANG)
    # updated_at is auto_now, so age it behind the model's back rather than by saving.
    stale = timezone.now() - timedelta(days=ARCHIVE_AFTER_DAYS + 1)
    RepairTask.objects.filter(pk__in=[old.pk, open_one.pk]).update(updated_at=stale)

    call_command("archive_finished_repairs")

    assert RepairTask.objects.get(pk=old.pk).archived_at is not None
    assert RepairTask.objects.get(pk=fresh.pk).archived_at is None
    assert RepairTask.objects.get(pk=open_one.pk).archived_at is None  # still open, however old


def test_an_archived_ticket_leaves_the_board_but_stays_searchable(client: Client, beboer: Resident) -> None:
    task = make_task(beboer, title="Utæt vandhane", status=RepairTask.Status.FAERDIG)
    task.archived_at = timezone.now()
    task.save(update_fields=["archived_at"])
    login(client, beboer)

    assert "Utæt vandhane" not in client.get(BOARD).content.decode()
    assert "Utæt vandhane" in client.get(f"{BOARD}arkiv?q=vandhane").content.decode()
    assert "Utæt vandhane" in client.get(f"{BOARD}{task.pk}").content.decode()  # still readable


def test_search_matches_the_fields_a_card_shows(client: Client, beboer: Resident) -> None:
    make_task(beboer, title="Utæt vandhane", location="Hallen")
    make_task(beboer, title="Knirkende dør", location="Batik")
    login(client, beboer)

    page = client.get(f"{BOARD}?q=batik").content.decode()
    assert "Knirkende dør" in page
    assert "Utæt vandhane" not in page


def test_the_two_crews_get_their_role_from_their_embedsgruppe() -> None:
    """The rest of this file hands out `repper`/`vicevaert` directly, so it would still pass if no
    real resident could ever hold them. In production a role comes from the month's embedsgruppe
    (residents.views._sync_month_roles reads WORKGROUP_ROLE), keyed by the Workgroup name the ETL
    imported verbatim from intern_alumne_workgroup — 'Repperne' (id 22) and 'Vicevært' (id 20).
    Spelling either differently silently grants nothing, which is exactly the bug this pins."""
    assert WORKGROUP_ROLE["Repperne"] == Role.REPPER
    assert WORKGROUP_ROLE["Vicevært"] == Role.VICEVAERT
