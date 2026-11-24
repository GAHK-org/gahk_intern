"""Access levels (access.policy, access.levels): what someone moving in, living here, or moved out gets.

Marked `access_levels`, so accounts here get their real level from their months on the alumneliste
rather than the other tests' default resident (conftest._accounts_live_here).
"""

from collections.abc import Callable, Iterator

import pytest
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver

from access.levels import levels_of, real_level
from access.policy import POLICY, Access, AccessLevel, Feature, access, feature_of
from core import live
from core.clock import current_date
from core.models import Room
from events.models import Answer, CalendarFeedToken, Event, Rsvp
from notifications import categories, services
from notifications.models import Notification
from residents.models import Residency, Resident, Role

pytestmark = [pytest.mark.django_db, pytest.mark.access_levels]

L = AccessLevel


def _month(months_ago: int) -> tuple[int, int]:
    today = current_date()
    index = today.year * 12 + today.month - 1 - months_ago
    return index // 12, index % 12 + 1


def _room(number: int) -> Room:
    return Room.objects.get_or_create(
        number=number, defaults={"legacy_index": number, "floor": "1. sal", "side": "mod gaden"}
    )[0]


def _on_lists(resident: Resident, *months_ago: int) -> Resident:
    """Put `resident` on the alumneliste `months_ago` months back (negative: a coming month)."""
    for ago in months_ago:
        year, month = _month(ago)
        Residency.objects.create(resident=resident, room=_room(100 + resident.pk), year=year, month=month)
    return resident


@pytest.fixture(autouse=True)
def this_month(make_resident: Callable[..., Resident]) -> Resident:
    """Someone on this month's list, so this month is the one in effect."""
    return _on_lists(make_resident(email="nabo@gahk.dk"), 0)


@pytest.fixture
def person(make_resident: Callable[..., Resident]) -> Callable[..., Resident]:
    counter = iter(range(1000))

    def make(*months_ago: int, **extra: object) -> Resident:
        return _on_lists(make_resident(email=f"p{next(counter)}@gahk.dk", **extra), *months_ago)

    return make


def _client(user: Resident) -> Client:
    c = Client()
    c.force_login(user)
    return c


# --- who is what -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("months_ago", "level"),
    [
        ((0,), L.RESIDENT),
        ((3, 2, 1, 0), L.RESIDENT),
        ((-1,), L.MOVING_IN),
        ((0, -1), L.RESIDENT),
        ((8, -1), L.MOVING_IN),  # moved out once, coming back
        ((1,), L.MOVED_OUT_UNDER_1_MONTH),  # last on the list the month before this one
        ((2,), L.MOVED_OUT_1_TO_3_MONTHS),
        ((3,), L.MOVED_OUT_1_TO_3_MONTHS),
        ((4,), L.MOVED_OUT_3_TO_12_MONTHS),
        ((12,), L.MOVED_OUT_3_TO_12_MONTHS),
        ((13,), L.MOVED_OUT_OVER_A_YEAR),
        ((40, 30, 20), L.MOVED_OUT_OVER_A_YEAR),
        ((), L.NO_ACCESS),
    ],
)
def test_the_level_follows_the_months_on_the_alumneliste(
    person: Callable[..., Resident], months_ago: tuple[int, ...], level: AccessLevel
) -> None:
    someone = person(*months_ago)
    assert levels_of([someone.pk])[someone.pk] == level  # as notifications ask, many at a time
    assert real_level(Resident.objects.get(pk=someone.pk)) == level  # as a request asks


def test_a_role_this_month_a_superuser_and_the_kitchen(person: Callable[..., Resident]) -> None:
    left_with_a_role = person(20, roles=[Role.INDSTILLING])
    superuser = person(is_superuser=True)
    kitchen = person(is_kitchen=True)
    assert levels_of([left_with_a_role.pk, superuser.pk, kitchen.pk]) == {
        left_with_a_role.pk: L.RESIDENT,
        superuser.pk: L.RESIDENT,
        kitchen.pk: L.KITCHEN,
    }


# --- the table -------------------------------------------------------------------------------------


def test_the_table_says_what_was_decided() -> None:
    every = set(Feature) - {Feature.ACCOUNT, Feature.PUBLIC, Feature.FILES}

    def full(level: AccessLevel) -> set[Feature]:
        return {f for f in every if access(level, f) == Access.FULL}

    assert full(L.RESIDENT) == every
    assert full(L.MOVING_IN) == every - {Feature.PHOTO_ALBUM, Feature.GAHKFLIX}
    assert full(L.MOVED_OUT_UNDER_1_MONTH) == every - {Feature.SOEGVAERELSE}
    assert full(L.MOVED_OUT_1_TO_3_MONTHS) == every - {
        Feature.SOEGVAERELSE,
        Feature.VAERELSESTJEK,
        Feature.REPARATIONER,
        Feature.AK,
        Feature.OELKAELDER,
    }
    assert {f for f in every if access(L.MOVED_OUT_1_TO_3_MONTHS, f) == Access.READ} == {
        Feature.AK,
        Feature.OELKAELDER,
    }
    assert full(L.MOVED_OUT_3_TO_12_MONTHS) == {
        Feature.PHOTO_ALBUM,
        Feature.OPSLAGSTAVLE,
        Feature.DEN_HURTIGE,
        Feature.EVENTS,
        Feature.NOTIFICATIONS,
        Feature.MENTIONS,
    }
    assert full(L.MOVED_OUT_OVER_A_YEAR) == {Feature.STAMTREE}
    assert full(L.KITCHEN) == {
        Feature.DEN_HURTIGE,
        Feature.OPSLAGSTAVLE,
        Feature.REPARATIONER,
        Feature.NOTIFICATIONS,
    }
    assert full(L.NO_ACCESS) == set()
    for level in POLICY:
        assert access(level, Feature.ACCOUNT) == Access.FULL, "everyone can log out"
        assert access(level, None) == Access.NONE, "a view in no feature is for residents only"


def _url_names(patterns: list[URLPattern | URLResolver], namespace: str = "") -> Iterator[tuple[str, object]]:
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            inner = f"{namespace}{pattern.namespace}:" if pattern.namespace else namespace
            yield from _url_names(pattern.url_patterns, inner)
        else:
            yield f"{namespace}{pattern.name}", pattern.callback


def test_only_the_role_tools_are_in_no_feature() -> None:
    """A view in no feature is closed to everyone but residents. That is right for the role tools
    and wrong for anything a former resident should keep, so a new view outside them must be put in
    a feature (residents.urls) or added here on purpose."""
    unmarked = {name for name, view in _url_names(get_resolver().url_patterns) if feature_of(view) is None}
    namespaces = {name.split(":")[0] for name in unmarked if ":" in name}
    assert namespaces == {"admin", "admissions", "cms", "regnskab", "siteadmin"}
    assert {name for name in unmarked if ":" not in name} == {"edit_resident"}
    assert "siteadmin:preview_set" not in unmarked, "a previewing admin must be able to end it"


# --- the gate --------------------------------------------------------------------------------------

OPEN, CLOSED = "open", "closed"


@pytest.mark.parametrize(
    ("months_ago", "path", "expected"),
    [
        # Moving in: everything but the photo album and GAHKFlix.
        ((-1,), "/soegvaerelse/", OPEN),
        ((-1,), "/alumneliste/", OPEN),
        ((-1,), "/fotoalbum/", CLOSED),
        # Under a month: everything but Søg værelse.
        ((1,), "/soegvaerelse/", CLOSED),
        ((1,), "/reparationer/", OPEN),
        ((1,), "/vaerelsestjek/", OPEN),
        ((1,), "/ak/", OPEN),
        # One to three months: no Reparationer or Værelsestjek; AK and Ølkælder to read.
        ((2,), "/reparationer/", CLOSED),
        ((2,), "/vaerelsestjek/", CLOSED),
        ((2,), "/soegvaerelse/", CLOSED),
        ((2,), "/ak/", OPEN),
        ((2,), "/oelkaelder/min-saldo", OPEN),
        ((2,), "/alumneliste/", OPEN),
        ((2,), "/fotoalbum/", OPEN),
        # Three to twelve months: four features.
        ((5,), "/opslagstavle/", OPEN),
        ((5,), "/den-hurtige/", OPEN),
        ((5,), "/begivenheder/", OPEN),
        ((5,), "/fotoalbum/", OPEN),
        ((5,), "/alumneliste/", CLOSED),
        ((5,), "/stamtree/", CLOSED),
        ((5,), "/ak/", CLOSED),
        ((5,), f"/beboer/{1}/profil", CLOSED),
        # Over a year: Stamtræ only.
        ((20,), "/stamtree/", OPEN),
        ((20,), "/opslagstavle/", CLOSED),
        ((20,), "/den-hurtige/", CLOSED),
        ((20,), "/notifikationer/", CLOSED),
        # Never on a list.
        ((), "/stamtree/", CLOSED),
        ((), "/opslagstavle/", CLOSED),
    ],
)
def test_each_level_reaches_its_features(
    person: Callable[..., Resident], months_ago: tuple[int, ...], path: str, expected: str
) -> None:
    status = _client(person(*months_ago)).get(path).status_code
    assert (status == 403) == (expected == CLOSED), (path, status)


def test_read_only_lets_reading_through_but_not_writing(person: Callable[..., Resident]) -> None:
    c = _client(person(2))
    page = c.get("/ak/")
    assert page.status_code == 200
    assert "Tilføj krydser" not in page.content.decode(), "no form it could not send"
    assert c.post("/ak/tilfoej", {"krydser": "1", "reason": "Fejede"}).status_code == 403

    under_a_month = _client(person(1))
    assert "Tilføj krydser" in under_a_month.get("/ak/").content.decode()
    assert under_a_month.post("/ak/tilfoej", {"krydser": "1", "reason": "Fejede"}).status_code == 302


@pytest.mark.parametrize(
    ("months_ago", "home"),
    [((5,), "/den-hurtige/"), ((20,), "/stamtree/")],
)
def test_without_the_dashboard_the_front_page_leads_to_its_first_feature(
    person: Callable[..., Resident], months_ago: tuple[int, ...], home: str
) -> None:
    response = _client(person(*months_ago)).get("/")
    assert (response.status_code, response["Location"]) == (302, home)


def test_an_account_never_on_a_list_is_told_so_and_can_log_out(person: Callable[..., Resident]) -> None:
    c = _client(person())
    page = c.get("/")
    assert page.status_code == 403
    assert "Ingen adgang" in page.content.decode()
    assert c.post("/logout").status_code == 302


# --- what the page shows -------------------------------------------------------------------------


def test_the_sidebar_and_topbar_show_only_what_the_level_can_open(person: Callable[..., Resident]) -> None:
    html = _client(person(5)).get("/opslagstavle/").content.decode()
    for shown in ("Den Hurtige", "Begivenheder", "Fotoalbum", 'class="notif-bell"'):
        assert shown in html, shown
    for hidden in ("Alumneliste", "Stamtræ", "Reparationer", "AK-krydser", "Søg beboer", "Min profil"):
        assert hidden not in html, hidden

    html = _client(person(20)).get("/stamtree/").content.decode()
    assert "Stamtræ" in html
    for hidden in ("Opslagstavle", "Den Hurtige", 'class="notif-bell"', "Notifikationsindstillinger"):
        assert hidden not in html, hidden


# --- notifications, live updates, the calendar feed -------------------------------------------------


def test_nobody_is_notified_about_a_page_they_cannot_open(person: Callable[..., Resident]) -> None:
    resident, recent, long_ago = person(0), person(5), person(20)
    services.notify(
        [resident.pk, recent.pk, long_ago.pk],
        category=categories.POSTS_NEW,
        title="Nyt opslag",
        url="/opslagstavle/1",
    )
    services.notify(
        [resident.pk, recent.pk, long_ago.pk],
        category=categories.REPAIRS,
        title="Ny reparation",
        url="/reparationer/1",
    )
    got = {(n.recipient_id, n.title) for n in Notification.objects.all()}
    assert got == {(resident.pk, "Nyt opslag"), (recent.pk, "Nyt opslag"), (resident.pk, "Ny reparation")}


def test_a_socket_listens_only_to_features_its_level_has(person: Callable[..., Resident]) -> None:
    from django.contrib.sessions.backends.db import SessionStore
    from django.http import HttpRequest

    def socket(user: Resident) -> HttpRequest:
        request = HttpRequest()
        request.user = Resident.objects.get(pk=user.pk)
        request.session = SessionStore()
        return request

    recent, long_ago = socket(person(5)), socket(person(20))
    assert live.may_subscribe(recent, "opslagstavle")
    assert not live.may_subscribe(recent, "reparationer")
    assert not live.may_subscribe(long_ago, "opslagstavle")
    assert not live.may_subscribe(long_ago, f"notifikationer.{long_ago.user.pk}")


def test_a_calendar_feed_empties_when_begivenheder_is_gone(
    person: Callable[..., Resident], this_month: Resident
) -> None:
    from datetime import timedelta

    from django.utils import timezone

    def feed_of(someone: Resident) -> str:
        event = Event.objects.create(
            title=f"Fest {someone.pk}", starts_at=timezone.now() + timedelta(days=3), organiser=this_month
        )
        Rsvp.objects.create(event=event, resident=someone, answer=Answer.JA, answered_at=timezone.now())
        token = CalendarFeedToken.objects.create(resident=someone)
        return Client().get(f"/kalender/{token.token}.ics").content.decode()

    assert "Fest" in feed_of(person(5))
    long_ago = feed_of(person(20))
    assert "BEGIN:VCALENDAR" in long_ago and "Fest" not in long_ago


# --- previewing a level ----------------------------------------------------------------------------


def test_an_administrator_can_preview_a_level_and_end_it(person: Callable[..., Resident]) -> None:
    admin = _client(person(0, roles=[Role.ADMINISTRATOR]))
    page = admin.get("/admin/preview").content.decode()
    assert "Vælg adgangsniveau" in page and "Fraflyttet for 3–12 måneder siden" in page
    admin.post("/admin/preview/set", {"mode": "level", "level": L.MOVED_OUT_OVER_A_YEAR.value})
    assert admin.get("/alumneliste/").status_code == 403
    page = admin.get("/stamtree/").content.decode()
    assert "Fraflyttet for over et år siden" in page, "the banner says what is previewed"

    admin.post("/admin/preview/set", {"mode": "clear"})
    assert admin.get("/alumneliste/").status_code == 200


def test_only_an_administrator_can_preview_a_level(person: Callable[..., Resident]) -> None:
    resident = _client(person(0))
    resident.post("/admin/preview/set", {"mode": "level", "level": L.NO_ACCESS.value})
    assert resident.get("/alumneliste/").status_code == 200


# --- stored on the account, and kept current ----------------------------------------------------------


def _stored(someone: Resident) -> AccessLevel:
    return AccessLevel(Resident.objects.values_list("access_level", flat=True).get(pk=someone.pk))


def test_the_level_is_stored_and_a_request_asks_nothing_more(person: Callable[..., Resident]) -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    someone = person(5)
    assert _stored(someone) == L.MOVED_OUT_3_TO_12_MONTHS
    client = _client(someone)
    with CaptureQueriesContext(connection) as queries:
        assert client.get("/stamtree/").status_code == 403
    # The roles still ask which month is in effect; the level's aggregate of someone's months is gone.
    assert not [q for q in queries if "residents_residency" in q["sql"] and "MAX(" in q["sql"]]


def test_a_change_to_the_months_or_roles_moves_the_level_at_once(person: Callable[..., Resident]) -> None:
    from residents.models import RoleAssignment, active_period

    someone = person(20)
    assert _stored(someone) == L.MOVED_OUT_OVER_A_YEAR
    _on_lists(someone, -1)  # on next month's list
    assert _stored(someone) == L.MOVING_IN
    Residency.objects.filter(resident=someone).delete()
    assert _stored(someone) == L.NO_ACCESS

    year, month = active_period()
    role = RoleAssignment.objects.create(resident=someone, role=Role.AK, year=year, month=month)
    assert _stored(someone) == L.RESIDENT
    role.delete()
    assert _stored(someone) == L.NO_ACCESS

    someone.is_kitchen = True
    someone.save()
    assert _stored(someone) == L.KITCHEN
    assert someone.access_level == L.KITCHEN, "the instance in hand follows"


def test_a_list_for_a_newer_month_that_has_started_moves_everyone(
    person: Callable[..., Resident], this_month: Resident
) -> None:
    """Made late: the month in effect is still last month's list until this month's first row."""
    Residency.objects.filter(resident=this_month).delete()
    stayed = person(1)  # on last month's list, which is in effect again
    assert _stored(stayed) == L.RESIDENT
    _on_lists(this_month, 0)  # this month's list begins
    assert _stored(stayed) == L.MOVED_OUT_UNDER_1_MONTH
    Residency.objects.filter(resident=this_month).delete()  # and is taken back
    assert _stored(stayed) == L.RESIDENT


def test_the_night_catches_up_with_what_the_signals_did_not_see(person: Callable[..., Resident]) -> None:
    """What time alone moves (a list made ahead taking effect on the 1st, the moved-out limits) is
    no write at all; a queryset .update() stands in for it here."""
    from access.tasks import refresh_access_levels

    someone = person(2)
    Residency.objects.filter(resident=someone).update(year=2000)  # no signal
    assert _stored(someone) == L.MOVED_OUT_1_TO_3_MONTHS
    refresh_access_levels.apply()
    assert _stored(someone) == L.MOVED_OUT_OVER_A_YEAR


def test_every_migrate_works_the_levels_out(person: Callable[..., Resident]) -> None:
    """A deploy fills the new column, and picks up a change to the rule itself."""
    from django.core.management import call_command

    someone = person(0)
    Resident.objects.update(access_level=L.NO_ACCESS)
    call_command("migrate", verbosity=0)
    assert _stored(someone) == L.RESIDENT


def test_a_whole_list_is_worked_out_once_at_the_end(person: Callable[..., Resident]) -> None:
    from access.levels import refreshed_once

    someone = person(20)
    with refreshed_once():
        _on_lists(someone, 1, 0)
        assert _stored(someone) == L.MOVED_OUT_OVER_A_YEAR, "not yet"
    assert _stored(someone) == L.RESIDENT


def test_the_list_editor_works_the_levels_out(person: Callable[..., Resident]) -> None:
    """next_month_list writes in a refreshed_once(): the newcomer it adds is moving in after it."""
    from residents.models import next_period

    indstilling = _client(person(0, roles=[Role.INDSTILLING]))
    newcomer = person()
    room = _room(321)
    ny, nm = next_period()
    response = indstilling.post(
        "/alumneliste/naeste-maaned?period=next",
        {"action": "add_existing", "resident": newcomer.pk, "room": room.pk},
    )
    assert response.status_code == 302
    assert Residency.objects.filter(resident=newcomer, year=ny, month=nm).exists()
    assert _stored(newcomer) == L.MOVING_IN


# --- the Indstilling's override ------------------------------------------------------------------------


def test_the_indstilling_can_override_the_level_until_a_last_day(person: Callable[..., Resident]) -> None:
    from datetime import timedelta

    from access.tasks import refresh_access_levels

    indstilling = _client(person(0, roles=[Role.INDSTILLING]))
    guest = person()
    page = indstilling.get(f"/beboer/{guest.pk}/rediger").content.decode()
    assert "Adgang nu: <strong>Ingen adgang</strong>" in page
    last_day = current_date() + timedelta(days=10)
    form = {
        "first_name": guest.first_name,
        "last_name": guest.last_name or "X",
        "email": guest.email,
        "access_level_override": L.MOVED_OUT_3_TO_12_MONTHS.value,
        "access_level_override_until": last_day.isoformat(),
    }
    assert indstilling.post(f"/beboer/{guest.pk}/rediger", form).status_code == 302
    assert _stored(guest) == L.MOVED_OUT_3_TO_12_MONTHS
    assert _client(guest).get("/den-hurtige/").status_code == 200

    from audit.models import AuditChange, AuditEvent

    given = AuditEvent.objects.get(action="residents.resident.access_level_overridden")
    assert given.actor_id != guest.pk, "the Indstilling's act, named as such"
    assert set(AuditChange.objects.filter(entity__event=given).values_list("field", flat=True)) == {
        "access_level_override",
        "access_level_override_until",
    }

    # Past its last day, the night ends it and clears it.
    Resident.objects.filter(pk=guest.pk).update(
        access_level_override_until=current_date() - timedelta(days=1)
    )
    refresh_access_levels.apply()
    guest.refresh_from_db()
    assert guest.access_level == L.NO_ACCESS
    assert guest.access_level_override == "" and guest.access_level_override_until is None
    ended = AuditEvent.objects.get(action="residents.resident.access_level_override_expired")
    assert ended.actor_id is None, "nobody's act"
    assert dict(AuditChange.objects.filter(entity__event=ended).values_list("field", "after")) == {
        "access_level_override": "",
        "access_level_override_until": None,
    }
    assert not AuditEvent.objects.filter(action__endswith=".updated", unclaimed=True).exists()


def test_an_override_does_not_beat_a_role_or_the_kitchen(person: Callable[..., Resident]) -> None:
    holder = person(20, roles=[Role.AK], access_level_override=L.NO_ACCESS.value)
    kitchen = person(is_kitchen=True, access_level_override=L.RESIDENT.value)
    assert _stored(holder) == L.RESIDENT
    assert _stored(kitchen) == L.KITCHEN


def test_the_kitchen_is_no_override(person: Callable[..., Resident]) -> None:
    from residents.forms import ResidentEditForm

    field = ResidentEditForm().fields["access_level_override"]
    assert L.KITCHEN.value not in {value for value, _label in field.choices}  # type: ignore[attr-defined]
