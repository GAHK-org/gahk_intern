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
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from core import push
from core.models import PushSubscription
from notifications import categories
from notifications.models import Notification
from reparationer.models import (
    ARCHIVE_AFTER_DAYS,
    OVERDUE_AFTER_DAYS,
    STALE_AFTER_DAYS,
    RepairComment,
    RepairImage,
    RepairTask,
)
from residents.models import WORKGROUP_ROLE, Account, Role, RoleAssignment, active_period

BOARD = "/reparationer/"
pytestmark = pytest.mark.django_db


@pytest.fixture
def beboer(make_resident: Callable[..., Account]) -> Account:
    return make_resident(email="beboer@gahk.dk", first_name="Bo", last_name="Beboer")


@pytest.fixture
def vicevaert(make_resident: Callable[..., Account]) -> Account:
    return make_resident(email="vice@gahk.dk", first_name="Vig", roles=(Role.VICEVAERT,))


@pytest.fixture
def repper(make_resident: Callable[..., Account]) -> Account:
    return make_resident(email="repper@gahk.dk", first_name="Rep", roles=(Role.REPPER,))


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch, settings: object) -> list[tuple[list[int], dict]]:
    """Records (recipient user ids, payload) for every notification the code would send —
    same fixture as tests/test_opslagstavle.py, which tests the shared transport."""
    settings.VAPID_PUBLIC_KEY = "test-public-key"  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = "test-private-key"  # type: ignore[attr-defined]
    recorded: list[tuple[list[int], dict]] = []

    def fake_dispatch(subscriptions: object, payload: dict, ttl: int = 0) -> int:
        ids = sorted(subscriptions.values_list("user_id", flat=True))  # type: ignore[attr-defined]
        recorded.append((ids, payload))
        return len(ids)

    monkeypatch.setattr(push, "_dispatch", fake_dispatch)
    monkeypatch.setattr(push, "_run_in_background", lambda fn: fn())
    return recorded


def subscribe(user: Account, endpoint: str) -> PushSubscription:
    """One device, for everything — there is no per-feature opt-in any more."""
    return PushSubscription.objects.create(user=user, endpoint=endpoint, auth="a" * 22, p256dh="p" * 87)


def told(category: str = categories.REPAIRS) -> list[int]:
    """Who has a notification row in `category`, by id — who was TOLD, device or not."""
    return sorted(Notification.objects.filter(category=category).values_list("recipient_id", flat=True))


def make_task(reporter: Account, **kwargs: object) -> RepairTask:
    return RepairTask.objects.create(
        reported_by=reporter,
        title=kwargs.pop("title", "Utæt vandhane"),  # type: ignore[arg-type]
        **kwargs,
    )


def login(client: Client, user: Account) -> Client:
    client.force_login(user)
    return client


@pytest.fixture
def media_tmp(settings: object, tmp_path: Path) -> Path:
    """Redirect MEDIA_ROOT so uploads land in a tmp dir, not the repo's app/media/."""
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    return tmp_path


def png(name: str = "billede.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"\x89PNG\r\n\x1a\n" + b"x" * 64, content_type="image/png")


def age(task: RepairTask, days: int) -> None:
    """Put `task` `days` into its current column, behind the model's back."""
    RepairTask.objects.filter(pk=task.pk).update(status_changed_at=timezone.now() - timedelta(days=days))


# --------------------------------------------------------------------------------- reporting


def test_any_resident_can_report_a_repair(client: Client, beboer: Account) -> None:
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
    client: Client, beboer: Account, vicevaert: Account, repper: Account, pushes: list
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
    task = RepairTask.objects.get()
    assert told() == [vicevaert.id]
    row = Notification.objects.get()
    assert (row.title, row.body, row.url) == ("Ny reparation meldt ind", "Utæt vandhane", f"{BOARD}{task.pk}")


def test_a_vicevaert_without_a_device_still_sees_a_new_repair_in_app(
    client: Client, beboer: Account, vicevaert: Account, pushes: list
) -> None:
    """The audience is the role, not who pressed a subscribe button: the crew's bell shows the
    ticket whether or not anyone's phone is set up for push."""
    login(client, beboer)

    client.post(f"{BOARD}opret", {"title": "Utæt vandhane", "location": "", "description": ""})

    assert pushes == []
    row = Notification.objects.get()
    assert (row.recipient_id, row.in_app, row.pushed) == (vicevaert.id, True, False)


def test_administrators_and_superusers_hear_about_every_repair(
    client: Client, beboer: Account, vicevaert: Account, make_resident: Callable[..., Account]
) -> None:
    """administrator implies every role (residents.permissions.real_roles), and a superuser may hold
    it with no RoleAssignment at all — so both are part of either crew's audience."""
    admin = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    superuser = make_resident(email="super@gahk.dk", is_superuser=True)
    login(client, beboer)

    client.post(f"{BOARD}opret", {"title": "Utæt vandhane", "location": "", "description": ""})

    assert told() == sorted([vicevaert.id, admin.id, superuser.id])


def test_only_the_current_role_holders_are_told(
    client: Client, beboer: Account, vicevaert: Account, make_resident: Callable[..., Account]
) -> None:
    """Someone who was a Vicevært last year and rotated off must not keep getting paged."""
    former = make_resident(email="former@gahk.dk")
    year, month = active_period()
    RoleAssignment.objects.create(resident=former, role=Role.VICEVAERT, year=year - 1, month=month)
    login(client, beboer)

    client.post(f"{BOARD}opret", {"title": "Utæt vandhane", "location": "", "description": ""})

    assert told() == [vicevaert.id]


def test_a_vicevaert_reporting_a_repair_is_not_told_about_it(
    client: Client, vicevaert: Account, make_resident: Callable[..., Account]
) -> None:
    colleague = make_resident(email="vice2@gahk.dk", roles=(Role.VICEVAERT,))
    login(client, vicevaert)

    client.post(f"{BOARD}opret", {"title": "Utæt vandhane", "location": "", "description": ""})

    assert told() == [colleague.id]


def test_the_board_lists_open_tickets_and_the_login_gate_holds(client: Client, beboer: Account) -> None:
    make_task(beboer, title="Utæt vandhane")
    assert client.get(BOARD).status_code == 302  # anonymous -> login
    login(client, beboer)
    assert "Utæt vandhane" in client.get(BOARD).content.decode()


# ------------------------------------------------------------------------------ moving a ticket


def test_a_plain_resident_cannot_move_a_ticket(client: Client, beboer: Account) -> None:
    task = make_task(beboer)
    login(client, beboer)
    assert client.post(f"{BOARD}{task.pk}/status", {"status": "i_gang"}).status_code == 403
    task.refresh_from_db()
    assert task.status == RepairTask.Status.NY


def test_a_vicevaert_moves_a_ticket_through_the_triage_columns(
    client: Client, beboer: Account, vicevaert: Account
) -> None:
    task = make_task(beboer)
    login(client, vicevaert)
    client.post(f"{BOARD}{task.pk}/status", {"status": "i_gang"})
    task.refresh_from_db()
    assert task.status == RepairTask.Status.I_GANG


@pytest.mark.parametrize("status", ["ak_projekt", "faerdig"])
def test_a_vicevaert_cannot_move_a_ticket_into_a_manager_only_column(
    client: Client, beboer: Account, vicevaert: Account, status: str
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
    client: Client, beboer: Account, repper: Account, status: str
) -> None:
    task = make_task(beboer, status=RepairTask.Status.I_GANG)
    login(client, repper)
    client.post(f"{BOARD}{task.pk}/status", {"status": status})
    task.refresh_from_db()
    assert task.status == status


def test_an_unknown_status_leaves_the_ticket_alone(client: Client, beboer: Account, repper: Account) -> None:
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
    client: Client, beboer: Account, repper: Account
) -> None:
    task = make_task(beboer)
    login(client, repper)
    body = drag(client, task, "faerdig")
    assert body["ok"] is True
    assert body["status"] == "faerdig"
    task.refresh_from_db()
    assert task.status == RepairTask.Status.FAERDIG


def test_a_refused_drag_says_why_and_reports_the_unchanged_status(
    client: Client, beboer: Account, vicevaert: Account
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
    client: Client, beboer: Account, vicevaert: Account
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
    client: Client, beboer: Account, vicevaert: Account, repper: Account, pushes: list
) -> None:
    subscribe(repper, "https://push.example/repper")
    task = make_task(beboer)
    login(client, vicevaert)

    client.post(f"{BOARD}{task.pk}/ansvarlig", {"responsible": "repper"})

    task.refresh_from_db()
    assert task.responsible == RepairTask.Responsible.REPPER
    assert [ids for ids, _ in pushes] == [[repper.id]]
    assert told() == [repper.id]  # not the Vicevært who handed it over
    assert Notification.objects.get().title == "Reparation overdraget til Repper"


def test_a_ticket_can_be_handed_back_to_the_vicevaerter(
    client: Client, beboer: Account, vicevaert: Account, repper: Account, pushes: list
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
    assert told() == [vicevaert.id]
    assert Notification.objects.get().title == "Reparation sendt tilbage til Viceværterne"


def test_handing_a_ticket_where_it_already_is_changes_and_notifies_nothing(
    client: Client, beboer: Account, vicevaert: Account, repper: Account, pushes: list
) -> None:
    subscribe(repper, "https://push.example/repper")
    task = make_task(beboer, responsible=RepairTask.Responsible.REPPER)
    login(client, vicevaert)

    client.post(f"{BOARD}{task.pk}/ansvarlig", {"responsible": "repper"})

    task.refresh_from_db()
    assert task.responsible == RepairTask.Responsible.REPPER
    assert pushes == []
    assert not Notification.objects.exists()


def test_an_unknown_responsible_leaves_the_ticket_alone(
    client: Client, beboer: Account, vicevaert: Account
) -> None:
    task = make_task(beboer)
    login(client, vicevaert)
    client.post(f"{BOARD}{task.pk}/ansvarlig", {"responsible": "hvem-som-helst"})
    task.refresh_from_db()
    assert task.responsible == RepairTask.Responsible.VICEVAERT


def test_a_plain_resident_cannot_hand_a_ticket_over(client: Client, beboer: Account) -> None:
    task = make_task(beboer)
    login(client, beboer)
    assert client.post(f"{BOARD}{task.pk}/ansvarlig", {"responsible": "repper"}).status_code == 403
    task.refresh_from_db()
    assert task.responsible == RepairTask.Responsible.VICEVAERT


def test_the_detail_page_offers_the_handoff_in_whichever_direction_is_left(
    client: Client, beboer: Account, vicevaert: Account
) -> None:
    task = make_task(beboer)
    login(client, vicevaert)
    assert "Overdrag til Repper" in client.get(f"{BOARD}{task.pk}").content.decode()

    task.responsible = RepairTask.Responsible.REPPER
    task.save(update_fields=["responsible"])
    assert "Send tilbage til Viceværterne" in client.get(f"{BOARD}{task.pk}").content.decode()


# ------------------------------------------------------------------------------------- comments


def test_any_resident_may_comment_and_delete_their_own_note(
    client: Client, beboer: Account, vicevaert: Account
) -> None:
    task = make_task(vicevaert)
    login(client, beboer)
    client.post(f"{BOARD}{task.pk}/kommentar", {"body": "Det drypper stadig."})
    comment = RepairComment.objects.get()
    assert comment.author == beboer

    client.post(f"{BOARD}kommentar/{comment.pk}/slet")
    assert not RepairComment.objects.exists()


def test_a_resident_cannot_delete_someone_elses_note(
    client: Client, beboer: Account, vicevaert: Account
) -> None:
    task = make_task(vicevaert)
    comment = RepairComment.objects.create(task=task, author=vicevaert, body="Kigger på det.")
    login(client, beboer)
    assert client.post(f"{BOARD}kommentar/{comment.pk}/slet").status_code == 403
    assert RepairComment.objects.exists()


# -------------------------------------------------------------------------- deleting / archiving


def test_only_a_manager_may_delete_a_ticket(
    client: Client, beboer: Account, vicevaert: Account, repper: Account
) -> None:
    task = make_task(beboer)
    login(client, vicevaert)
    assert client.post(f"{BOARD}{task.pk}/slet").status_code == 403
    assert RepairTask.objects.filter(pk=task.pk).exists()

    login(client, repper)
    client.post(f"{BOARD}{task.pk}/slet")
    assert not RepairTask.objects.filter(pk=task.pk).exists()


def test_the_nightly_sweep_archives_only_long_finished_tickets(client: Client, beboer: Account) -> None:
    old = make_task(beboer, title="Gammel", status=RepairTask.Status.FAERDIG)
    fresh = make_task(beboer, title="Frisk", status=RepairTask.Status.FAERDIG)
    open_one = make_task(beboer, title="Åben", status=RepairTask.Status.I_GANG)
    stale = timezone.now() - timedelta(days=ARCHIVE_AFTER_DAYS + 1)
    RepairTask.objects.filter(pk__in=[old.pk, open_one.pk]).update(status_changed_at=stale)

    call_command("archive_finished_repairs")

    assert RepairTask.objects.get(pk=old.pk).archived_at is not None
    assert RepairTask.objects.get(pk=fresh.pk).archived_at is None
    assert RepairTask.objects.get(pk=open_one.pk).archived_at is None  # still open, however old


def test_an_archived_ticket_leaves_the_board_but_stays_searchable(client: Client, beboer: Account) -> None:
    task = make_task(beboer, title="Utæt vandhane", status=RepairTask.Status.FAERDIG)
    task.archived_at = timezone.now()
    task.save(update_fields=["archived_at"])
    login(client, beboer)

    assert "Utæt vandhane" not in client.get(BOARD).content.decode()
    assert "Utæt vandhane" in client.get(f"{BOARD}arkiv?q=vandhane").content.decode()
    assert "Utæt vandhane" in client.get(f"{BOARD}{task.pk}").content.decode()  # still readable


def test_search_matches_the_fields_a_card_shows(client: Client, beboer: Account) -> None:
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


# --------------------------------------------------------------------------------- photos


def test_a_report_can_carry_several_photos(client: Client, beboer: Account, media_tmp: Path) -> None:
    login(client, beboer)
    client.post(f"{BOARD}opret", {"title": "Knækket vindue", "images": [png("a.png"), png("b.png")]})
    task = RepairTask.objects.get()
    assert task.images.filter(comment__isnull=True).count() == 2
    page = client.get(f"{BOARD}{task.pk}").content.decode()
    assert all(image.file.url in page for image in task.images.all())


def test_a_refused_photo_is_dropped_but_the_report_is_kept(
    client: Client, beboer: Account, media_tmp: Path
) -> None:
    login(client, beboer)
    evil = SimpleUploadedFile("evil.svg", b"<svg/>", content_type="image/svg+xml")
    response = client.post(f"{BOARD}opret", {"title": "Knækket vindue", "images": [evil, png()]}, follow=True)
    task = RepairTask.objects.get()
    assert task.images.count() == 1  # the PNG survived, the SVG did not
    assert "evil.svg" in response.content.decode()  # and the resident is told which one


def test_a_note_may_be_photos_alone_but_not_nothing(client: Client, beboer: Account, media_tmp: Path) -> None:
    task = make_task(beboer)
    login(client, beboer)

    client.post(f"{BOARD}{task.pk}/kommentar", {"body": "", "images": [png()]})
    comment = RepairComment.objects.get()
    assert comment.images.count() == 1
    assert RepairImage.objects.get().task == task

    response = client.post(f"{BOARD}{task.pk}/kommentar", {"body": "  "}, follow=True)
    assert RepairComment.objects.count() == 1
    assert "vedhæft et billede" in response.content.decode()


def test_deleting_a_note_takes_its_photos_with_it(
    client: Client, beboer: Account, media_tmp: Path, django_capture_on_commit_callbacks: Callable
) -> None:
    task = make_task(beboer)
    login(client, beboer)
    client.post(f"{BOARD}{task.pk}/kommentar", {"body": "Se her", "images": [png()]})
    stored = media_tmp / RepairImage.objects.get().file.name
    assert stored.exists()

    # core.files removes the file only once the delete has committed.
    with django_capture_on_commit_callbacks(execute=True):
        client.post(f"{BOARD}kommentar/{RepairComment.objects.get().pk}/slet")
    assert not RepairImage.objects.exists()
    assert not stored.exists()


# --------------------------------------------------------------------------------- column age


def test_a_card_turns_yellow_then_red_the_longer_it_sits(client: Client, beboer: Account) -> None:
    fresh = make_task(beboer, title="Frisk")
    stale = make_task(beboer, title="Gammel")
    overdue = make_task(beboer, title="Glemt")
    age(stale, STALE_AFTER_DAYS)
    age(overdue, OVERDUE_AFTER_DAYS)

    assert RepairTask.objects.get(pk=fresh.pk).column_age == ""
    assert RepairTask.objects.get(pk=stale.pk).column_age == "stale"
    assert RepairTask.objects.get(pk=overdue.pk).column_age == "overdue"

    page = login(client, beboer).get(BOARD).content.decode()
    assert f'id="kanban-task-{stale.pk}" data-task-id="{stale.pk}"' in page
    assert page.count('data-age="stale"') == 1
    assert page.count('data-age="overdue"') == 1


def test_a_finished_ticket_is_always_green(beboer: Account) -> None:
    task = make_task(beboer, status=RepairTask.Status.FAERDIG)
    assert RepairTask.objects.get(pk=task.pk).column_age == "done"
    age(task, OVERDUE_AFTER_DAYS + 10)
    assert RepairTask.objects.get(pk=task.pk).column_age == "done"


def test_awaiting_approval_runs_on_a_shorter_clock(beboer: Account) -> None:
    """Godkend af Repper is a check, not a repair: red after two weeks, not two months."""
    task = make_task(beboer, status=RepairTask.Status.GODKENDT)
    age(task, 6)
    assert RepairTask.objects.get(pk=task.pk).column_age == ""
    age(task, 7)
    assert RepairTask.objects.get(pk=task.pk).column_age == "stale"
    age(task, 14)
    assert RepairTask.objects.get(pk=task.pk).column_age == "overdue"


def test_moving_a_card_resets_its_colour_but_a_handoff_does_not(
    client: Client, beboer: Account, vicevaert: Account
) -> None:
    task = make_task(beboer)
    age(task, OVERDUE_AFTER_DAYS)
    login(client, vicevaert)

    client.post(f"{BOARD}{task.pk}/ansvarlig", {"responsible": RepairTask.Responsible.REPPER})
    assert RepairTask.objects.get(pk=task.pk).column_age == "overdue"

    # The same column again is not a move.
    client.post(f"{BOARD}{task.pk}/status", {"status": RepairTask.Status.NY})
    assert RepairTask.objects.get(pk=task.pk).column_age == "overdue"

    client.post(f"{BOARD}{task.pk}/status", {"status": RepairTask.Status.I_GANG})
    assert RepairTask.objects.get(pk=task.pk).column_age == ""


def test_column_age_follows_the_dev_clock(client: Client, beboer: Account, vicevaert: Account) -> None:
    """Fast-forwarding the dev clock (core.clock) ages the cards — and a move made "in the future"
    is stamped there, so it starts fresh rather than already looking a month old."""
    from django.test import override_settings

    from core.models import DevClock

    task = make_task(beboer)
    future = timezone.localdate() + timedelta(days=OVERDUE_AFTER_DAYS + 1)
    DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": future})
    # Roles are per month, so the Vicevært needs one in the simulated month too.
    RoleAssignment.objects.create(
        resident=vicevaert, role=Role.VICEVAERT, year=future.year, month=future.month
    )
    with override_settings(DEBUG=True):
        assert RepairTask.objects.get(pk=task.pk).column_age == "overdue"
        login(client, vicevaert).post(f"{BOARD}{task.pk}/status", {"status": RepairTask.Status.I_GANG})
        assert RepairTask.objects.get(pk=task.pk).column_age == ""
