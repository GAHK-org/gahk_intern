"""The general audit trail (audit.trail): append-only, what an event captures, the unclaimed fallback,
the security events, coverage of every model, and the audit page."""

import datetime
from collections.abc import Callable

import pytest
from django.apps import apps
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.db import DatabaseError, connection, transaction
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from audit import trail
from audit.models import AppendOnlyError, AuditChange, AuditEntity, AuditEvent
from core.models import OutgoingMail
from residents.models import AdminAccessGrant, Resident, Role, RoleAssignment, active_period

LOGIN = "/admin/login"

# Apps whose models are not in the trail yet; each is taken off as it is audited. The coverage test
# below holds every other app to: each model registered, or exempted with a reason.
PENDING_APPS = {
    "admissions",
    "ak",
    "cms",
    "core",
    "gahkflix",
    "notifications",
    "oelkaelder",
    "photo_album",
    "regnskab",
    "reparationer",
    "rooms",
    "stats",
}


def _changes(event: AuditEvent, kind: str) -> dict[str, tuple[object, object]]:
    entity = event.entities.get(kind=kind)
    return {c.field: (c.before, c.after) for c in entity.changes.all()}


# --- append-only -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_database_refuses_to_change_or_delete_a_row() -> None:
    trail.record("test.thing", actor=None, details={"n": 1})
    event = AuditEvent.objects.get()
    for sql in ("UPDATE audit_auditevent SET action = 'x'", "DELETE FROM audit_auditevent"):
        with (
            pytest.raises(DatabaseError, match="append-only"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(sql)
    assert AuditEvent.objects.get().action == event.action == "test.thing"


def _backdated_event(action: str, days: int) -> AuditEvent:
    with trail.backdated(timezone.now() - datetime.timedelta(days=days)):
        trail.record(action, actor=None)
    return AuditEvent.objects.latest("id")


@pytest.mark.django_db
def test_the_database_lets_a_row_go_only_once_its_retention_ran_out(
    make_resident: Callable[..., Resident],
) -> None:
    anna = make_resident(email="anna@gahk.dk", phone="111")
    with trail.backdated(timezone.now() - datetime.timedelta(days=2 * 365)):
        with trail.event("residents.resident.updated", actor=None, subject=anna):
            anna.phone = "222"
            anna.save()
    kept = AuditEvent.objects.get()
    entity = kept.entities.get()
    for sql in (
        f"DELETE FROM audit_auditchange WHERE entity_id = {entity.pk}",
        f"DELETE FROM audit_auditentity WHERE id = {entity.pk}",
        f"DELETE FROM audit_auditevent WHERE id = {kept.pk}",
    ):
        with (
            pytest.raises(DatabaseError, match="append-only"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(sql)  # two years old, and kept for five

    expired = _backdated_event("auth.login", days=366)
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM audit_auditevent WHERE id = {expired.pk}")
    assert not AuditEvent.objects.filter(pk=expired.pk).exists()


@pytest.mark.django_db
def test_the_nightly_purge_deletes_what_expired_and_says_how_much(
    make_resident: Callable[..., Resident],
) -> None:
    from audit.tasks import purge_expired

    anna = make_resident(email="anna@gahk.dk", phone="111")
    for action in ("auth.login", "auth.login.failed", "auth.logout", "auth.password_reset.requested"):
        _backdated_event(action, days=366)
    young_login = _backdated_event("auth.login", days=300)
    young_change = _backdated_event("residents.role.added", days=366)
    with trail.backdated(timezone.now() - datetime.timedelta(days=5 * 366)):
        with trail.event("residents.resident.updated", actor=None, subject=anna):
            anna.phone = "222"
            anna.save()

    assert purge_expired() == 5
    assert set(AuditEvent.objects.values_list("action", flat=True)) == {
        young_login.action,
        young_change.action,
        "audit.expired.purged",
    }
    assert not AuditEntity.objects.filter(kind="residents.resident").exists()
    assert not AuditChange.objects.exists()
    assert AuditEvent.objects.get(action="audit.expired.purged").details == {"count": 5}
    assert purge_expired() == 0
    assert AuditEvent.objects.filter(action="audit.expired.purged").count() == 1, "nothing purged, no event"


@pytest.mark.django_db
def test_the_models_refuse_before_the_database_has_to() -> None:
    trail.record("test.thing", actor=None)
    event = AuditEvent.objects.get()
    event.action = "x"
    with pytest.raises(AppendOnlyError):
        event.save()
    with pytest.raises(AppendOnlyError):
        event.delete()
    with pytest.raises(AppendOnlyError):
        AuditEvent.objects.update(action="x")
    with pytest.raises(AppendOnlyError):
        AuditEntity.objects.all().delete()


@pytest.mark.django_db
def test_deleting_a_resident_leaves_their_history_as_it_was(make_resident: Callable[..., Resident]) -> None:
    anna = make_resident(email="anna@gahk.dk", first_name="Anna", last_name="Berg")
    with trail.event("residents.resident.updated", actor=anna, subject=anna):
        anna.phone = "12345678"
        anna.save()
    Resident.objects.filter(pk=anna.pk).delete()

    event = AuditEvent.objects.get()
    assert (event.actor_id, event.actor_name) == (anna.pk, "Anna Berg")
    assert event.entities.get().name == "Anna Berg"


# --- what an event captures ------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_event_captures_creations_changes_and_deletions(make_resident: Callable[..., Resident]) -> None:
    admin = make_resident(email="admin@gahk.dk", first_name="Ada", last_name="Min")
    anna = make_resident(email="anna@gahk.dk", first_name="Anna", last_name="Berg", phone="111")
    year, month = active_period()
    old_role = RoleAssignment.objects.create(resident=anna, role=Role.AK, year=year, month=month)

    with trail.event("test.several", actor=admin, subject=anna, details={"why": "test"}):
        anna.phone = "222"
        anna.save()
        anna.phone = "333"  # two writes of one field: one change, first before to last after
        anna.save()
        RoleAssignment.objects.create(resident=anna, role=Role.PR, year=year, month=month)
        old_role.delete()

    event = AuditEvent.objects.get()
    assert (event.action, event.actor_id, event.actor_name, event.details) == (
        "test.several",
        admin.pk,
        "Ada Min",
        {"why": "test"},
    )
    subject = event.entities.get(kind="residents.resident")
    assert (subject.role, subject.operation, subject.name) == ("subject", "updated", "Anna Berg")
    assert _changes(event, "residents.resident") == {"phone": ("111", "333")}
    roles = {e.operation: e for e in event.entities.filter(kind="residents.role")}
    assert {c.field: c.after for c in roles["created"].changes.all()}["role"] == Role.PR
    assert {c.field: c.before for c in roles["deleted"].changes.all()}["role"] == Role.AK


@pytest.mark.django_db
def test_a_password_is_recorded_as_changed_never_its_value(make_resident: Callable[..., Resident]) -> None:
    anna = make_resident(email="anna@gahk.dk")
    with trail.event("auth.password.changed", actor=anna, subject=anna):
        anna.set_password("helt-ny")
        anna.save()

    change = AuditChange.objects.get(field="password")
    assert (change.redacted, change.before, change.after) == (True, None, None)


@pytest.mark.django_db
def test_ignored_fields_alone_record_nothing(make_resident: Callable[..., Resident]) -> None:
    anna = make_resident(email="anna@gahk.dk")
    with trail.event("test.login", actor=anna, only_if_changed=True):
        anna.save(update_fields=["last_login"])
        anna.is_staff = True
        anna.save()
    assert not AuditEvent.objects.exists()


@pytest.mark.django_db
def test_a_subject_made_inside_the_event_is_named_with_its_id() -> None:
    with trail.event("residents.resident.created", actor=None) as event:
        made = Resident.objects.create(email="ny@gahk.dk", first_name="Ny", last_name="Beboer")
        event.involve(made, "subject")
    entity = AuditEntity.objects.get()
    assert (entity.entity_id, entity.role, entity.operation, entity.name) == (
        made.pk,
        "subject",
        "created",
        "Ny Beboer",
    )


@pytest.mark.django_db
def test_a_backdated_event_is_dated_when_the_demo_history_says() -> None:
    """Only the demo database's made-up history (audit.demo) backdates; everything else is now."""
    from datetime import timedelta

    from django.utils import timezone

    then = timezone.now() - timedelta(days=10)
    with trail.backdated(then):
        trail.record("test.thing", actor=None)
    trail.record("test.thing", actor=None)
    backdated, current = AuditEvent.objects.order_by("id")
    assert backdated.at == then
    assert current.at > then + timedelta(days=9)


@pytest.mark.django_db
def test_unaudited_work_leaves_no_trail_not_even_a_named_event(
    make_resident: Callable[..., Resident],
) -> None:
    """An import or the demo seed may call a service that names its own event: still nothing."""
    anna = make_resident(email="anna@gahk.dk", phone="111")
    with trail.unaudited():
        with trail.event("test.thing", actor=None, subject=anna):
            anna.phone = "222"
            anna.save()
        trail.record("test.thing", actor=None)
    assert not AuditEvent.objects.exists()


@pytest.mark.django_db
def test_work_that_fails_leaves_no_trail(make_resident: Callable[..., Resident]) -> None:
    anna = make_resident(email="anna@gahk.dk", phone="111")
    with pytest.raises(RuntimeError), trail.event("test.fails", actor=None):
        anna.phone = "222"
        anna.save()
        raise RuntimeError
    assert not AuditEvent.objects.exists()
    assert Resident.objects.get(pk=anna.pk).phone == "111"


@pytest.mark.django_db
def test_a_change_no_signal_sees_can_be_recorded_by_hand(make_resident: Callable[..., Resident]) -> None:
    anna = make_resident(email="anna@gahk.dk", phone="111")
    with trail.event("test.bulk", actor=None) as event:
        Resident.objects.filter(pk=anna.pk).update(phone="999")
        event.change(anna, "phone", "111", "999")
    assert _changes(AuditEvent.objects.get(), "residents.resident") == {"phone": ("111", "999")}


# --- outside an event ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_request_writing_outside_an_event_is_an_error_in_tests(
    make_resident: Callable[..., Resident],
) -> None:
    anna = make_resident(email="anna@gahk.dk")
    with (
        trail.context("web", request=RequestFactory().post("/somewhere")),
        pytest.raises(trail.UnauditedWrite),
    ):
        anna.phone = "222"
        anna.save()


@pytest.mark.django_db
def test_outside_tests_it_is_recorded_as_unclaimed(
    make_resident: Callable[..., Resident], settings: object, caplog: pytest.LogCaptureFixture
) -> None:
    settings.AUDIT_STRICT = False  # type: ignore[attr-defined]
    settings.AUDIT_FALLBACK = True  # type: ignore[attr-defined]
    anna = make_resident(email="anna@gahk.dk", phone="111")
    AuditEvent.objects.all()  # the creation above is recorded too (as "system": no request)
    request = RequestFactory().post("/somewhere")
    request.user = anna
    with trail.context("web", request=request):
        anna.phone = "222"
        anna.save()

    event = AuditEvent.objects.filter(action="residents.resident.updated").get()
    assert (event.unclaimed, event.source, event.actor_id) == (True, "web", anna.pk)
    assert _changes(event, "residents.resident") == {"phone": ("111", "222")}
    assert "Unclaimed audit write" in caplog.text
    created = AuditEvent.objects.get(action="residents.resident.created")
    assert (created.unclaimed, created.source) == (True, "system")


@pytest.mark.django_db
def test_the_django_admin_is_recorded_unclaimed_without_a_warning(
    make_resident: Callable[..., Resident], settings: object, caplog: pytest.LogCaptureFixture
) -> None:
    anna = make_resident(email="anna@gahk.dk", phone="111")
    request = RequestFactory().post("/django-admin/residents/resident/1/change/")
    request.user = anna
    with trail.context("web", request=request):
        anna.phone = "222"
        anna.save()
    assert AuditEvent.objects.get().unclaimed
    assert "Unclaimed" not in caplog.text


# --- security events -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_login_and_a_logout(make_resident: Callable[..., Resident]) -> None:
    anna = make_resident(email="anna@gahk.dk", password="rigtig")
    client = Client(REMOTE_ADDR="10.0.0.7")
    client.post(LOGIN, {"username": "anna@gahk.dk", "password": "rigtig"})
    client.post("/admin/logout")

    login, logout = AuditEvent.objects.order_by("id")
    assert (login.action, login.actor_id, login.ip, login.source) == (
        "auth.login",
        anna.pk,
        "10.0.0.7",
        "web",
    )
    assert login.entities.get().entity_id == anna.pk
    assert (logout.action, logout.actor_id) == ("auth.logout", anna.pk)


@pytest.mark.django_db
def test_failed_logins_say_why(make_resident: Callable[..., Resident]) -> None:
    anna = make_resident(email="anna@gahk.dk", password="rigtig")
    Client().post(LOGIN, {"username": "anna@gahk.dk", "password": "forkert"})
    Client().post(LOGIN, {"username": "ukendt@gahk.dk", "password": "x"})

    wrong, unknown = AuditEvent.objects.order_by("id")
    assert (wrong.action, wrong.actor_id, wrong.details) == (
        "auth.login.failed",
        None,
        {"reason": "wrong password"},
    )
    assert wrong.entities.get().entity_id == anna.pk
    assert unknown.details == {"reason": "no account", **trail.typed_address("ukendt@gahk.dk")}
    assert unknown.details["email_domain"] == "gahk.dk"
    assert "ukendt" not in str(unknown.details)
    assert not unknown.entities.exists()


@pytest.mark.django_db
def test_a_password_typed_as_the_address_is_not_kept() -> None:
    Client().post(LOGIN, {"username": "Hemmeligt@kodeord", "password": "x"})
    Client().post(LOGIN, {"username": "hemmeligt@kodeord ", "password": "y"})
    first, second = AuditEvent.objects.order_by("id")
    assert "emmeligt" not in str(first.details) and "kodeord" not in str(first.details)
    assert set(first.details) == {"reason", "email_hash"}, "no domain: it does not look like one"
    assert first.details == second.details, "the same address typed twice is one hash"


@pytest.mark.django_db
def test_an_old_password_upgraded_at_login_is_part_of_the_login(
    make_resident: Callable[..., Resident],
) -> None:
    make_resident(email="gammel@gahk.dk", password="rigtig", legacy=True)
    Client().post(LOGIN, {"username": "gammel@gahk.dk", "password": "rigtig"})
    login = AuditEvent.objects.get()
    assert login.action == "auth.login"
    assert AuditChange.objects.get(entity__event=login).field == "password"


@pytest.mark.django_db
def test_a_login_form_with_nothing_in_it_records_nothing() -> None:
    Client().post(LOGIN, {"username": "", "password": ""})
    assert not AuditEvent.objects.exists()


@pytest.mark.django_db
def test_password_change_and_reset(make_resident: Callable[..., Resident]) -> None:
    anna = make_resident(email="anna@gahk.dk", password="rigtig")
    client = Client()
    client.force_login(anna)
    client.post(
        "/admin/password-change",
        {
            "old_password": "rigtig",
            "new_password1": "Et-nyt-kodeord-42",
            "new_password2": "Et-nyt-kodeord-42",
        },
    )
    Client().post("/admin/password-reset", {"email": "anna@gahk.dk"})
    Client().post("/admin/password-reset", {"email": "ingen@gahk.dk"})
    assert len(mail.outbox) == 1
    uid = urlsafe_base64_encode(force_bytes(anna.pk))
    token = default_token_generator.make_token(Resident.objects.get(pk=anna.pk))
    reset = Client()
    url = reset.get(f"/admin/reset/{uid}/{token}/").url
    reset.post(url, {"new_password1": "Endnu-et-kodeord-7", "new_password2": "Endnu-et-kodeord-7"})

    changed, asked, asked_unknown, completed = AuditEvent.objects.exclude(action="auth.login").order_by("id")
    assert (changed.action, changed.actor_id) == ("auth.password.changed", anna.pk)
    assert AuditChange.objects.get(entity__event=changed).redacted
    assert (asked.action, asked.details, asked.entities.get().entity_id) == (
        "auth.password_reset.requested",
        {"accounts": 1},
        anna.pk,
    )
    assert asked_unknown.details == {"accounts": 0, **trail.typed_address("ingen@gahk.dk")}
    assert "ingen" not in str(asked_unknown.details)
    assert (completed.action, completed.actor_id) == ("auth.password_reset.completed", anna.pk)


@pytest.mark.django_db
def test_roles_admin_access_preview_and_refusals(make_resident: Callable[..., Resident]) -> None:
    admin = make_resident(email="admin@gahk.dk", roles=[Role.ADMINISTRATOR], is_superuser=True)
    second = make_resident(email="second@gahk.dk", is_superuser=True)
    anna = make_resident(email="anna@gahk.dk")
    client = Client()
    client.force_login(admin)
    client.post("/admin/roles", {"resident": anna.pk, "role": Role.ADMINISTRATOR, "action": "add"})
    other = Client()
    other.force_login(second)  # not the one who granted it
    other.post(
        "/admin/adminadgang", {"grant": AdminAccessGrant.objects.get(resident=anna).pk, "action": "deny"}
    )
    client.post("/admin/roles", {"resident": anna.pk, "role": Role.ADMINISTRATOR, "action": "remove"})
    client.post("/admin/preview/set", {"mode": "role", "role": Role.PR})
    client.post("/admin/preview/set", {"mode": "clear"})
    plain = Client()
    plain.force_login(anna)
    plain.get("/admin/roles")

    # (force_login is a login like any other: leave those out)
    actions = list(
        AuditEvent.objects.exclude(action="auth.login").order_by("id").values_list("action", flat=True)
    )
    assert actions == [
        "residents.role.added",
        "residents.admin_access.denied",
        "residents.role.removed",
        "residents.preview.set",
        "residents.preview.cleared",
        "auth.access.denied",
    ]
    added = AuditEvent.objects.get(action="residents.role.added")
    kinds = set(added.entities.values_list("kind", "operation"))
    # The role, and the approval it opened, with who asked for it.
    assert {("residents.role", "created"), ("residents.admin_access", "created")} <= kinds
    assert _changes(added, "residents.admin_access")["requested_by"] == (None, admin.pk)
    removed = AuditEvent.objects.get(action="residents.role.removed")
    assert set(removed.entities.values_list("kind", "operation")) >= {
        ("residents.role", "deleted"),
        ("residents.admin_access", "deleted"),
    }
    denied = AuditEvent.objects.get(action="auth.access.denied")
    assert (denied.actor_id, denied.details) == (
        anna.pk,
        {"path": "/admin/roles", "roles": ["administrator"]},
    )


# --- sending mail again ---------------------------------------------------------------------------


def _failed_mail(recipient: str, status: str = "failed") -> OutgoingMail:
    from datetime import timedelta

    from django.utils import timezone

    return OutgoingMail.objects.create(
        subject="Alumneregning",
        recipients=recipient,
        status=status,
        payload={"body": "Hej"},
        expires_at=timezone.now() + timedelta(days=3),
    )


@pytest.mark.django_db
def test_the_outbox_itself_is_left_out_but_sending_a_mail_again_is_an_event(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deliverer rewrites a mail on every attempt (core.auditing); a person sending one again acts."""
    from core import mail as outbox

    monkeypatch.setattr(outbox, "_schedule_delivery", lambda mail_kind: None)
    admin = make_resident(email="admin@gahk.dk", roles=[Role.ADMINISTRATOR], is_superuser=True)
    failed, sent = _failed_mail("anna@gahk.dk"), _failed_mail("bo@gahk.dk", status="sent")
    assert not AuditEvent.objects.exists(), "queueing and delivering a mail writes no event"
    client = Client()
    client.force_login(admin)

    client.post(
        "/django-admin/core/outgoingmail/",
        {"action": "send_again", "_selected_action": [failed.pk, sent.pk]},
    )

    event = AuditEvent.objects.get(action="core.mail.sent_again")
    assert (event.actor, event.details, event.unclaimed) == (admin, {"count": 1}, False)
    [mail_entity] = event.entities.all()
    assert (mail_entity.kind, mail_entity.entity_id) == ("core.mail", failed.pk)
    assert mail_entity.name == "Alumneregning → anna@gahk.dk"
    assert OutgoingMail.objects.get(pk=failed.pk).status == OutgoingMail.Status.QUEUED

    client.post("/django-admin/core/outgoingmail/", {"action": "send_again", "_selected_action": [sent.pk]})
    assert AuditEvent.objects.filter(action="core.mail.sent_again").count() == 1, "nothing sent again"


@pytest.mark.django_db
def test_regnskab_sending_the_alumneregninger_and_one_again_are_events(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    from regnskab import tasks
    from regnskab.models import AlumneregningSend

    def broker_down(*args: object) -> None:
        raise ConnectionError("no broker")

    monkeypatch.setattr(tasks.send_alumneregninger_task, "delay", broker_down)
    regnskab = make_resident(email="acc@gahk.dk", roles=("regnskab",))
    anna = make_resident(email="anna@gahk.dk", phone="111")
    period = {"date_from": "2026-08-01", "date_to": "2026-08-31"}
    lost = AlumneregningSend.objects.create(
        resident=anna, outcome="sent", mail=_failed_mail("anna@gahk.dk", status="unknown"), **period
    )
    client = Client()
    client.force_login(regnskab)

    send = reverse("regnskab:send_alumneregninger")
    with pytest.raises(ConnectionError):
        client.post(send, {"fra": "2026-08-01", "til": "2026-08-31"})
    assert not AuditEvent.objects.filter(action="regnskab.alumneregninger.sent").exists(), "never queued"

    monkeypatch.setattr(tasks.send_alumneregninger_task, "delay", lambda *args: None)
    client.post(send, {"fra": "2026-08-01", "til": "2026-08-31"})
    sent = AuditEvent.objects.get(action="regnskab.alumneregninger.sent")
    assert (sent.actor, sent.details) == (regnskab, period)

    not_received = reverse("regnskab:alumneregning_not_received", args=[lost.pk])
    client.post(not_received)
    client.post(not_received)  # the mail is failed now: nothing more to send again, and no event

    [again] = AuditEvent.objects.filter(action="regnskab.alumneregning.not_received")
    assert (again.actor, again.details) == (regnskab, period)
    entities = {(e.kind, e.role): e.entity_id for e in again.entities.all()}
    assert entities == {("residents.resident", "subject"): anna.pk, ("core.mail", ""): lost.mail_id}


# --- coverage --------------------------------------------------------------------------------------


def test_every_model_is_audited_or_exempted_on_purpose() -> None:
    missing = [
        f"{model._meta.app_label}.{model.__name__}"
        for model in apps.get_models()
        if model._meta.app_label not in PENDING_APPS and not trail.is_covered(model)
    ]
    assert missing == [], "register these in their app's auditing.py, or exempt them with a reason"


def test_every_named_action_is_a_dotted_name() -> None:
    for action in trail.known_actions():
        assert action.count(".") >= 2 or action.startswith("auth."), action


# --- the audit page --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_audit_page_is_for_administrators(make_resident: Callable[..., Resident]) -> None:
    anna = make_resident(email="anna@gahk.dk")
    client = Client()
    client.force_login(anna)
    assert client.get("/admin/audit").status_code == 403


@pytest.mark.django_db
def test_the_audit_page_shows_and_filters_the_trail(make_resident: Callable[..., Resident]) -> None:
    admin = make_resident(
        email="admin@gahk.dk", first_name="Ada", last_name="Min", roles=[Role.ADMINISTRATOR]
    )
    anna = make_resident(email="anna@gahk.dk", first_name="Anna", last_name="Berg", phone="111")
    with trail.event("residents.resident.updated", actor=admin, subject=anna):
        anna.phone = "222"
        anna.set_password("ny")
        anna.save()
    trail.record(
        "auth.login.failed", actor=None, details={"reason": "no account", **trail.typed_address("x@y.dk")}
    )
    client = Client()
    client.force_login(admin)

    page = client.get("/admin/audit").content.decode()
    assert "Ada Min" in page and "rettede beboer" in page and "Anna Berg" in page
    assert "111" in page and "222" in page  # before and after
    assert "ændret (værdien gemmes ikke)" in page  # the password
    assert "mislykket login" in page

    history = client.get(f"/admin/audit?kind=residents.resident&id={anna.pk}").content.decode()
    assert "Historik: Beboer «Anna Berg»" in history
    assert "mislykket login" not in history
    by_area = client.get("/admin/audit?area=auth").content.decode()
    assert "mislykket login" in by_area and "rettede beboer" not in by_area
    assert "mislykket login" not in client.get("/admin/audit?q=anna").content.decode()


@pytest.mark.django_db
def test_the_audit_page_finds_actions_by_their_names_and_typed_addresses_by_hash(
    make_resident: Callable[..., Resident],
) -> None:
    from audit import presentation

    admin = make_resident(email="admin@gahk.dk", roles=[Role.ADMINISTRATOR])
    anna = make_resident(email="anna@gahk.dk", phone="111")
    trail.record("residents.role.added", actor=admin, subject=anna, details={"role": "ak"})
    with trail.event("residents.resident.updated", actor=admin, subject=anna):
        anna.phone = "222"
        anna.save()
    trail.record(
        "auth.login.failed", actor=None, details={"reason": "no account", **trail.typed_address("x@y.dk")}
    )

    def found(q: str) -> set[str]:
        return set(presentation.Filters(q=q).queryset().values_list("action", flat=True))

    assert found("Gav rolle") == {"residents.role.added"}
    assert found("rettede beboer") == {"residents.resident.updated"}
    assert "scheduler.task.updated" in trail.actions_called("ændret: planlagt"), "a kind's generic name"
    assert found("residents.role") == {"residents.role.added"}, "the code still works"
    assert found(" X@Y.dk") == {"auth.login.failed"}, "the address typed, found by its hash"
    assert found("z@y.dk") == set()

    client = Client()
    client.force_login(admin)
    page = client.get("/admin/audit?q=gav+rolle").content.decode()
    assert "gav rolle" in page.lower() and "mislykket login" not in page


@pytest.mark.django_db
def test_the_audit_page_pages_back_by_id(make_resident: Callable[..., Resident]) -> None:
    from audit import presentation

    for n in range(5):
        trail.record("test.thing", actor=None, details={"n": n})
    first, older = presentation.page(presentation.Filters(), size=3)
    rest, none = presentation.page(presentation.Filters(), before=older, size=3)
    assert [dict(e.details)["n"] for e in first + rest] == ["4", "3", "2", "1", "0"]
    assert none is None
