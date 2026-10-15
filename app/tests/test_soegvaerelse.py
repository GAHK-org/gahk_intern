"""Room lottery: the target/offer month is always the subsequent month (next_period),
stored as an absolute index but shown human-readably. See rooms/views_soegvaerelse.py."""

from collections.abc import Callable
from datetime import date

import pytest
from django.test import Client
from django.urls import reverse

from core.models import Room
from residents.models import Residency, Resident, next_period
from rooms.kvotient import month_index, month_label
from rooms.models import KvotientApplication, KvotientPriority, RoomOffer
from rooms.views_soegvaerelse import allocate_round


def test_month_label_is_inverse_of_month_index() -> None:
    assert month_label(month_index(2026, 8)) == "August 2026"
    assert month_label(24319) == "August 2026"  # the raw int users used to see


@pytest.mark.django_db
def test_create_offer_targets_next_period(make_resident: Callable) -> None:
    ind = make_resident(email="ind@gahk.dk", roles=("indstilling",))
    Room.objects.create(legacy_index=1, number=3, floor="stuen", side="mod gaden")
    c = Client()
    c.force_login(ind)
    c.post(reverse("soegvaerelse:create_offer"), {"room": "3"})  # no year/month sent
    offer = RoomOffer.objects.get()
    assert offer.month == month_index(*next_period())  # auto: the upcoming month


@pytest.mark.django_db
def test_application_targets_next_period_without_month_input(make_resident: Callable) -> None:
    r = make_resident(email="r@gahk.dk", move_in_date=date(2024, 8, 1))
    room = Room.objects.create(legacy_index=2, number=101, floor="1. sal", side="mod gaden")
    RoomOffer.objects.create(room=room, month=month_index(*next_period()))
    c = Client()
    c.force_login(r)
    # Note: no target_year / target_month in the payload — the view fixes it to next_period.
    c.post(
        reverse("soegvaerelse:soeg"),
        {"done_year": "2028", "done_month": "6", "orlov_months": "0", "priority": [str(room.id)]},
    )
    app = KvotientApplication.objects.get()
    assert app.move_month == month_index(*next_period())


@pytest.mark.django_db
def test_apply_page_shows_fixed_month_and_drops_manual_input(make_resident: Callable) -> None:
    r = make_resident(email="r2@gahk.dk", move_in_date=date(2024, 8, 1))
    room = Room.objects.create(legacy_index=3, number=201, floor="2. sal", side="mod gaden")
    RoomOffer.objects.create(room=room, month=month_index(*next_period()))
    c = Client()
    c.force_login(r)
    html = c.get(reverse("soegvaerelse:soeg")).content.decode()
    assert month_label(month_index(*next_period())) in html  # human-readable month shown
    assert 'name="target_year"' not in html  # manual month input is gone
    assert 'name="target_month"' not in html
    # Study-end is month/year dropdowns (Danish names), not raw number spinners.
    assert 'name="done_month"' in html and '<option value="6">Juni</option>' in html
    assert 'name="done_year"' in html


def _application(resident: Resident, month: int, k: float) -> KvotientApplication:
    return KvotientApplication.objects.create(
        resident=resident, move_month=month, move_in_month=0, done_studying_month=month + 12, k=k
    )


@pytest.mark.django_db
def test_allocate_round_is_global_greedy(make_resident: Callable) -> None:
    """Higher-K applicant takes their 1st choice; that frees a lower choice for a lower-K applicant."""
    m = month_index(*next_period())
    a = Room.objects.create(legacy_index=10, number=10, floor="stuen", side="mod gaden")
    b = Room.objects.create(legacy_index=11, number=11, floor="stuen", side="mod gaden")
    RoomOffer.objects.create(room=a, month=m)
    RoomOffer.objects.create(room=b, month=m)
    hi = _application(make_resident(email="hi@gahk.dk"), m, k=9.0)
    lo = _application(make_resident(email="lo@gahk.dk"), m, k=5.0)
    for app in (hi, lo):  # both rank A first, then B
        KvotientPriority.objects.create(application=app, room=a, priority=1, month=m)
        KvotientPriority.objects.create(application=app, room=b, priority=2, month=m)

    winners = allocate_round(m).winners
    assert winners[a.id].id == hi.id  # highest K wins their 1st choice
    assert winners[b.id].id == lo.id  # A taken -> next applicant wins their 2nd choice


@pytest.mark.django_db
def test_end_round_assigns_winner_to_room_and_clears(make_resident: Callable) -> None:
    ind = make_resident(email="ind2@gahk.dk", roles=("indstilling",))
    winner = make_resident(email="win@gahk.dk")
    m = month_index(*next_period())
    ny, nm = next_period()
    room = Room.objects.create(legacy_index=20, number=20, floor="stuen", side="mod gaden")
    RoomOffer.objects.create(room=room, month=m)
    app = _application(winner, m, k=7.0)
    KvotientPriority.objects.create(application=app, room=room, priority=1, month=m)

    c = Client()
    c.force_login(ind)
    c.post(reverse("soegvaerelse:end_round"))

    assert Residency.objects.filter(resident=winner, room=room, year=ny, month=nm).exists()
    assert not RoomOffer.objects.exists()  # round cleared
    assert not KvotientApplication.objects.exists()


@pytest.mark.django_db
def test_end_round_carries_non_participants_forward(make_resident: Callable) -> None:
    """Regression: ending a round must carry residents who weren't part of it into the target month.

    Previously end_round wrote only the winners, so when the target month became active every other
    resident vanished from the list."""
    from residents.models import active_period

    cy, cm = active_period()  # empty DB -> current calendar month
    r1 = Room.objects.create(legacy_index=30, number=30, floor="stuen", side="mod gaden")
    r2 = Room.objects.create(legacy_index=31, number=31, floor="stuen", side="mod gaden")
    bystander = make_resident(email="bystander@gahk.dk")
    Residency.objects.create(resident=bystander, room=r1, year=cy, month=cm)
    Residency.objects.create(resident=make_resident(email="leaver@gahk.dk"), room=r2, year=cy, month=cm)

    ny, nm = next_period((cy, cm))
    m = month_index(ny, nm)
    winner = make_resident(email="win2@gahk.dk")
    app = _application(winner, m, k=9.0)
    KvotientPriority.objects.create(application=app, room=r2, priority=1, month=m)
    RoomOffer.objects.create(room=r2, month=m)

    ind = make_resident(email="ind3@gahk.dk", roles=("indstilling",))
    c = Client()
    c.force_login(ind)
    c.post(reverse("soegvaerelse:end_round"))

    # the bystander (not in the round) is carried forward — the bug was that they disappeared
    assert Residency.objects.filter(resident=bystander, room=r1, year=ny, month=nm).exists()
    # the winner is placed into the offered room on the target month
    assert Residency.objects.filter(resident=winner, room=r2, year=ny, month=nm).exists()


@pytest.mark.django_db
def test_end_round_winner_keeps_embedsgruppe_and_cleaning(make_resident: Callable) -> None:
    """Regression: a winner carried to next month keeps their embedsgruppe/rengøring (only the room
    changes) - they used to be wiped to blank."""
    from core.models import Cleaning, Workgroup
    from residents.models import active_period

    cy, cm = active_period()
    old = Room.objects.create(legacy_index=40, number=40, floor="stuen", side="mod gaden")
    won = Room.objects.create(legacy_index=41, number=41, floor="stuen", side="mod gaden")
    wg = Workgroup.objects.create(name="Haven")
    cl = Cleaning.objects.create(name="Trappe")
    winner = make_resident(email="w@gahk.dk")
    Residency.objects.create(resident=winner, room=old, workgroup=wg, cleaning=cl, year=cy, month=cm)

    ny, nm = next_period((cy, cm))
    m = month_index(ny, nm)
    app = _application(winner, m, k=9.0)
    KvotientPriority.objects.create(application=app, room=won, priority=1, month=m)
    RoomOffer.objects.create(room=won, month=m)

    ind = make_resident(email="ind4@gahk.dk", roles=("indstilling",))
    c = Client()
    c.force_login(ind)
    c.post(reverse("soegvaerelse:end_round"))

    res = Residency.objects.get(resident=winner, year=ny, month=nm)
    assert res.room_id == won.id  # moved into the won room
    assert res.workgroup_id == wg.id  # embedsgruppe preserved
    assert res.cleaning_id == cl.id  # rengøring preserved


@pytest.mark.django_db
def test_resubmit_overwrites_and_keeps_apply_time(make_resident: Callable) -> None:
    """One application per resident per round: a second submit updates the existing one (new
    priorities + K) instead of stacking a duplicate, and keeps the original apply_datetime."""
    r = make_resident(email="edit@gahk.dk", move_in_date=date(2024, 8, 1))
    a = Room.objects.create(legacy_index=50, number=50, floor="stuen", side="mod gaden")
    b = Room.objects.create(legacy_index=51, number=51, floor="stuen", side="mod gaden")
    m = month_index(*next_period())
    RoomOffer.objects.create(room=a, month=m)
    RoomOffer.objects.create(room=b, month=m)
    c = Client()
    c.force_login(r)

    c.post(
        reverse("soegvaerelse:soeg"),
        {"done_year": "2028", "done_month": "6", "orlov_months": "0", "priority": [str(a.id)]},
    )
    app = KvotientApplication.objects.get()
    first_time, first_k = app.apply_datetime, app.k
    assert [p.room_id for p in app.priorities.all()] == [a.id]

    c.post(
        reverse("soegvaerelse:soeg"),
        {"done_year": "2030", "done_month": "6", "orlov_months": "0", "priority": [str(b.id), str(a.id)]},
    )

    assert KvotientApplication.objects.count() == 1  # overwritten, not duplicated
    app.refresh_from_db()
    assert [p.room_id for p in app.priorities.all()] == [b.id, a.id]  # new list
    assert app.k != first_k  # recomputed (study-end changed)
    assert app.apply_datetime == first_time  # queue position preserved


@pytest.mark.django_db
def test_one_application_per_resident_per_round_enforced(make_resident: Callable) -> None:
    from django.db import IntegrityError

    r = make_resident(email="dup@gahk.dk")
    m = month_index(*next_period())
    _application(r, m, k=5.0)
    with pytest.raises(IntegrityError):
        _application(r, m, k=6.0)  # same (resident, move_month)


@pytest.mark.django_db
def test_apply_form_prefills_existing_application(make_resident: Callable) -> None:
    r = make_resident(email="pre@gahk.dk", move_in_date=date(2024, 8, 1))
    room = Room.objects.create(legacy_index=52, number=52, floor="stuen", side="mod gaden")
    m = month_index(*next_period())
    RoomOffer.objects.create(room=room, month=m)
    c = Client()
    c.force_login(r)
    c.post(
        reverse("soegvaerelse:soeg"),
        {"done_year": "2028", "done_month": "6", "orlov_months": "0", "priority": [str(room.id)]},
    )

    html = c.get(reverse("soegvaerelse:soeg")).content.decode()
    assert "Gem ændringer" in html  # edit mode
    assert f'<option value="{room.id}" selected>' in html  # priority preselected
    assert '<option value="6" selected>Juni</option>' in html  # study-end month preselected


@pytest.mark.django_db
def test_equal_k_contest_is_flagged_not_auto_awarded(make_resident: Callable) -> None:
    """Two equal-K applicants for the same room → contested, and neither is silently placed there."""
    m = month_index(*next_period())
    room = Room.objects.create(legacy_index=60, number=60, floor="stuen", side="mod gaden")
    RoomOffer.objects.create(room=room, month=m)
    x = _application(make_resident(email="x@gahk.dk"), m, k=7.0)
    y = _application(make_resident(email="y@gahk.dk"), m, k=7.0)
    for app in (x, y):
        KvotientPriority.objects.create(application=app, room=room, priority=1, month=m)

    alloc = allocate_round(m)
    assert room.id not in alloc.winners  # not auto-awarded
    assert {a.id for a in alloc.contested[room.id]} == {x.id, y.id}


@pytest.mark.django_db
def test_tied_resident_is_held_not_cascaded(make_resident: Callable) -> None:
    """A resident tied for their 1st choice is not dropped into a free 2nd choice before the coin flip."""
    m = month_index(*next_period())
    a = Room.objects.create(legacy_index=61, number=61, floor="stuen", side="mod gaden")
    b = Room.objects.create(legacy_index=62, number=62, floor="stuen", side="mod gaden")
    RoomOffer.objects.create(room=a, month=m)
    RoomOffer.objects.create(room=b, month=m)
    x = _application(make_resident(email="x2@gahk.dk"), m, k=7.0)
    y = _application(make_resident(email="y2@gahk.dk"), m, k=7.0)
    for app in (x, y):  # both want A first
        KvotientPriority.objects.create(application=app, room=a, priority=1, month=m)
    KvotientPriority.objects.create(application=x, room=b, priority=2, month=m)  # x also lists B

    alloc = allocate_round(m)
    assert a.id in alloc.contested  # A is the tie
    assert b.id not in alloc.winners  # x is held, NOT given B while A is unresolved


@pytest.mark.django_db
def test_resolve_tie_awards_room_and_clears_contest(make_resident: Callable) -> None:
    m = month_index(*next_period())
    room = Room.objects.create(legacy_index=63, number=63, floor="stuen", side="mod gaden")
    offer = RoomOffer.objects.create(room=room, month=m)
    x = _application(make_resident(email="x3@gahk.dk"), m, k=7.0)
    y = _application(make_resident(email="y3@gahk.dk"), m, k=7.0)
    for app in (x, y):
        KvotientPriority.objects.create(application=app, room=room, priority=1, month=m)

    ind = make_resident(email="ind5@gahk.dk", roles=("indstilling",))
    c = Client()
    c.force_login(ind)
    c.post(reverse("soegvaerelse:resolve_tie", args=[offer.id]), {"application": str(y.id)})

    alloc = allocate_round(m)
    assert room.id not in alloc.contested  # resolved
    assert alloc.winners[room.id].id == y.id  # the chosen applicant


@pytest.mark.django_db
def test_end_round_leaves_contested_rooms_for_resolution(make_resident: Callable) -> None:
    """A settled room is assigned and cleared; a contested room's offer and applications survive."""
    from residents.models import active_period

    cy, cm = active_period()
    ny, nm = next_period((cy, cm))
    m = month_index(ny, nm)
    settled = Room.objects.create(legacy_index=70, number=70, floor="stuen", side="mod gaden")
    tied = Room.objects.create(legacy_index=71, number=71, floor="stuen", side="mod gaden")
    RoomOffer.objects.create(room=settled, month=m)
    RoomOffer.objects.create(room=tied, month=m)

    solo = _application(make_resident(email="solo@gahk.dk"), m, k=8.0)
    KvotientPriority.objects.create(application=solo, room=settled, priority=1, month=m)
    x = _application(make_resident(email="tx@gahk.dk"), m, k=7.0)
    y = _application(make_resident(email="ty@gahk.dk"), m, k=7.0)
    for app in (x, y):
        KvotientPriority.objects.create(application=app, room=tied, priority=1, month=m)

    ind = make_resident(email="ind6@gahk.dk", roles=("indstilling",))
    c = Client()
    c.force_login(ind)
    c.post(reverse("soegvaerelse:end_round"))

    assert Residency.objects.filter(resident=solo.resident, room=settled, year=ny, month=nm).exists()
    assert not RoomOffer.objects.filter(room=settled).exists()  # settled offer cleared
    assert RoomOffer.objects.filter(room=tied).exists()  # contested offer kept
    assert KvotientApplication.objects.filter(id__in=[x.id, y.id]).count() == 2  # tied apps kept
    assert not KvotientApplication.objects.filter(id=solo.id).exists()  # winner's app cleared


@pytest.mark.django_db
def test_dev_clock_override_only_applies_under_debug() -> None:
    """The simulated clock is honoured only when DEBUG; in prod (DEBUG=False) it is ignored, so it
    can never shift production's sense of 'now'."""
    import datetime

    from django.test import override_settings

    from core.clock import current_date
    from core.models import DevClock

    DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": datetime.date(2030, 3, 15)})

    with override_settings(DEBUG=True):
        assert current_date() == datetime.date(2030, 3, 15)
    with override_settings(DEBUG=False):
        assert current_date() != datetime.date(2030, 3, 15)  # real clock


@pytest.mark.django_db
def test_dev_clock_shifts_active_period(make_resident: Callable) -> None:
    import datetime

    from django.test import override_settings

    from core.models import DevClock
    from residents.models import active_period

    r = Room.objects.create(legacy_index=80, number=80, floor="stuen", side="mod gaden")
    Residency.objects.create(resident=make_resident(email="ap@gahk.dk"), room=r, year=2030, month=5)
    DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": datetime.date(2030, 6, 1)})
    with override_settings(DEBUG=True):
        assert active_period() == (2030, 5)  # the May list is the latest that has started by June


@pytest.mark.django_db
def test_dev_clock_set_is_404_in_prod(make_resident: Callable) -> None:
    from django.test import override_settings

    ind = make_resident(email="ind7@gahk.dk", roles=("indstilling",))
    c = Client()
    c.force_login(ind)
    with override_settings(DEBUG=False):
        assert c.post(reverse("siteadmin:dev_clock_set"), {"action": "advance"}).status_code == 404


@pytest.mark.django_db
def test_end_round_evicts_departing_occupant_no_clash(make_resident: Callable) -> None:
    """The room-clash bug: a winner taking an offered room must not end up sharing it with the
    previous occupant carried forward. That occupant (the leaver) is dropped from the room."""
    from residents.models import RoleAssignment, active_period

    cy, cm = active_period()
    ny, nm = next_period((cy, cm))
    m = month_index(ny, nm)
    room = Room.objects.create(legacy_index=90, number=90, floor="stuen", side="mod gaden")

    leaver = make_resident(email="leaver2@gahk.dk")
    Residency.objects.create(resident=leaver, room=room, year=cy, month=cm)  # currently in 090
    RoleAssignment.objects.create(resident=leaver, role="ak", year=cy, month=cm)

    winner = make_resident(email="winner3@gahk.dk")
    app = _application(winner, m, k=9.0)
    KvotientPriority.objects.create(application=app, room=room, priority=1, month=m)
    RoomOffer.objects.create(room=room, month=m)

    ind = make_resident(email="ind8@gahk.dk", roles=("indstilling",))
    c = Client()
    c.force_login(ind)
    c.post(reverse("soegvaerelse:end_round"))

    occupants = list(
        Residency.objects.filter(room=room, year=ny, month=nm).values_list("resident_id", flat=True)
    )
    assert occupants == [winner.id]  # exactly one — the winner, not the leaver
    assert not Residency.objects.filter(resident=leaver, year=ny, month=nm).exists()  # leaver evicted
    # and the leaver's carried-forward role for the target month is cleared too
    assert not RoleAssignment.objects.filter(resident=leaver, year=ny, month=nm).exists()


@pytest.mark.django_db
def test_end_round_cascade_relocating_winner_keeps_room_and_roles(make_resident: Callable) -> None:
    """Musical chairs: two rooms settled in one round where a winner's OLD room is also being won.
    The eviction must remove only the true leaver, never a relocating winner (nor strip their roles),
    regardless of processing order."""
    from residents.models import RoleAssignment, active_period

    cy, cm = active_period()
    ny, nm = next_period((cy, cm))
    m = month_index(ny, nm)
    a = Room.objects.create(legacy_index=100, number=100, floor="stuen", side="mod gaden")
    b = Room.objects.create(legacy_index=101, number=101, floor="stuen", side="mod gaden")

    leaver = make_resident(email="leave3@gahk.dk")
    Residency.objects.create(resident=leaver, room=a, year=cy, month=cm)  # A freed by leaver
    w = make_resident(email="w4@gahk.dk")  # currently in B, wins A -> frees B
    Residency.objects.create(resident=w, room=b, year=cy, month=cm)
    RoleAssignment.objects.create(resident=w, role="ak", year=cy, month=cm)
    w2 = make_resident(email="w5@gahk.dk")  # newcomer-ish, wins B (w's freed room)
    Residency.objects.create(resident=w2, room=a, year=cy, month=cm)  # placeholder current room

    app_w = _application(w, m, k=9.0)
    KvotientPriority.objects.create(application=app_w, room=a, priority=1, month=m)
    app_w2 = _application(w2, m, k=8.0)
    KvotientPriority.objects.create(application=app_w2, room=b, priority=1, month=m)
    RoomOffer.objects.create(room=a, month=m)
    RoomOffer.objects.create(room=b, month=m)

    ind = make_resident(email="ind9@gahk.dk", roles=("indstilling",))
    c = Client()
    c.force_login(ind)
    c.post(reverse("soegvaerelse:end_round"))

    # w relocated B->A and is intact, with roles preserved (never caught by the eviction)
    assert Residency.objects.get(resident=w, year=ny, month=nm).room_id == a.id
    assert RoleAssignment.objects.filter(resident=w, role="ak", year=ny, month=nm).exists()
    assert Residency.objects.get(resident=w2, year=ny, month=nm).room_id == b.id
    assert not Residency.objects.filter(
        resident=leaver, year=ny, month=nm
    ).exists()  # only the leaver removed
    # no clashes anywhere in the target month
    from residents.views import _clash_rooms

    assert _clash_rooms(ny, nm) == []


@pytest.mark.django_db
def test_resident_can_delete_own_application(make_resident: Callable) -> None:
    r = make_resident(email="del@gahk.dk")
    m = month_index(*next_period())
    app = _application(r, m, k=5.0)
    KvotientPriority.objects.create(
        application=app,
        room=Room.objects.create(legacy_index=110, number=110, floor="1. sal", side="mod gaden"),
        priority=1,
        month=m,
    )
    c = Client()
    c.force_login(r)

    assert c.get(reverse("soegvaerelse:delete_application", args=[app.id])).status_code == 405  # POST-only
    assert KvotientApplication.objects.filter(id=app.id).exists()

    resp = c.post(reverse("soegvaerelse:delete_application", args=[app.id]))
    assert resp.status_code == 302
    assert not KvotientApplication.objects.filter(id=app.id).exists()  # gone (cascades priorities)
    assert not KvotientPriority.objects.filter(application_id=app.id).exists()


@pytest.mark.django_db
def test_resident_cannot_delete_another_persons_application(make_resident: Callable) -> None:
    owner = make_resident(email="owner@gahk.dk")
    other = make_resident(email="other@gahk.dk")
    m = month_index(*next_period())
    app = _application(owner, m, k=5.0)
    c = Client()
    c.force_login(other)

    assert c.post(reverse("soegvaerelse:delete_application", args=[app.id])).status_code == 404
    assert KvotientApplication.objects.filter(id=app.id).exists()  # untouched


@pytest.mark.django_db
def test_end_round_shows_move_overview_on_admin(make_resident: Callable) -> None:
    """After 'Afslut runde', the admin page shows an overview of the moves: winner from→to, and any
    departing occupant removed."""
    from residents.models import active_period

    cy, cm = active_period()
    ny, nm = next_period((cy, cm))
    m = month_index(ny, nm)
    a = Room.objects.create(legacy_index=120, number=120, floor="stuen", side="mod gaden")
    b = Room.objects.create(legacy_index=121, number=121, floor="stuen", side="mod gaden")

    mover = make_resident(email="mover@gahk.dk", first_name="Mette", last_name="Mover")
    Residency.objects.create(resident=mover, room=b, year=cy, month=cm)  # in 121, wins 120
    leaver = make_resident(email="leaver9@gahk.dk", first_name="Lars", last_name="Leaver")
    Residency.objects.create(resident=leaver, room=a, year=cy, month=cm)  # in 120, leaving
    app = _application(mover, m, k=9.0)
    KvotientPriority.objects.create(application=app, room=a, priority=1, month=m)
    RoomOffer.objects.create(room=a, month=m)

    ind = make_resident(email="ind10@gahk.dk", roles=("indstilling",))
    c = Client()
    c.force_login(ind)
    c.post(reverse("soegvaerelse:end_round"))  # PRG → summary stashed in session

    html = c.get(reverse("soegvaerelse:admin")).content.decode()
    assert "Seneste runde" in html
    assert "Mette Mover" in html and "121" in html and "120" in html  # from 121 → 120
    assert "Lars Leaver" in html  # departing occupant listed as removed
    # shown once then cleared
    assert "Seneste runde" not in c.get(reverse("soegvaerelse:admin")).content.decode()


def test_compute_k_parts_matches_compute_k() -> None:
    """The a/b breakdown is the single source of the K number the form and applications both use."""
    from rooms.kvotient import compute_k, compute_k_parts

    parts = compute_k_parts(move_in_index=100, done_studying_index=136, target_index=112, orlov_months=0)
    assert parts == {"a": 12, "b": 24, "k": compute_k(100, 136, 112, 0)}
    assert compute_k_parts(112, 100, 112, 0)["k"] == 0.0  # non-positive denominator guarded


@pytest.mark.django_db
def test_kvotient_endpoint_shows_live_k(make_resident: Callable) -> None:
    from datetime import date

    r = make_resident(email="kv@gahk.dk", move_in_date=date(2024, 8, 1))
    c = Client()
    c.force_login(r)

    html = c.get(
        reverse("soegvaerelse:kvotient"), {"done_year": "2028", "done_month": "6", "orlov_months": "0"}
    ).content.decode()
    assert "K =" in html and "beregnet frem til" in html  # a real number + breakdown

    # incomplete input → a hint, not a crash
    assert "Udfyld" in c.get(reverse("soegvaerelse:kvotient"), {"orlov_months": "0"}).content.decode()
    # junk orlov is tolerated (no 500)
    assert (
        c.get(
            reverse("soegvaerelse:kvotient"), {"done_year": "2028", "done_month": "6", "orlov_months": "abc"}
        ).status_code
        == 200
    )


@pytest.mark.django_db
def test_kvotient_endpoint_handles_missing_move_in_date(make_resident: Callable) -> None:
    r = make_resident(email="kvnodate@gahk.dk")  # no move_in_date
    c = Client()
    c.force_login(r)
    html = c.get(reverse("soegvaerelse:kvotient"), {"done_year": "2028", "done_month": "6"}).content.decode()
    assert "mangler din indflytningsdato" in html
    assert "K =" not in html


@pytest.mark.django_db
def test_soeg_shows_kvotient_calculator_even_without_a_round(make_resident: Callable) -> None:
    """The proposal: a resident can see/compute their kvotient outside room-round periods too."""
    from datetime import date

    r = make_resident(email="noround@gahk.dk", move_in_date=date(2024, 8, 1))
    c = Client()
    c.force_login(r)
    html = c.get(reverse("soegvaerelse:soeg")).content.decode()  # no offers exist

    assert "Din kvotient" in html  # calculator present
    assert "K = a · 100" in html  # the formula is shown
    assert "ingen værelser i udbud" in html  # and it's clear there's no active round
    assert 'name="priority"' not in html  # but no application form without offers


@pytest.mark.django_db
def test_soegvaerelse_pages_leak_no_template_syntax(make_resident: Callable) -> None:
    """Django {# … #} is single-line only; a multi-line one renders verbatim on the page. Assert the
    søgvaerelse pages contain no stray template syntax (the værelsestjek suite guards its own pages)."""
    from datetime import date

    from core.models import Room
    from rooms.models import RoomOffer

    r = make_resident(email="leak-sv@gahk.dk", move_in_date=date(2024, 8, 1))
    room = Room.objects.create(legacy_index=130, number=130, floor="stuen", side="mod gaden")
    RoomOffer.objects.create(room=room, month=month_index(*next_period()))
    ind = make_resident(email="leak-ind@gahk.dk", roles=("indstilling",))

    checks = [
        (r, "/intern/soegvaerelse/"),
        (r, "/intern/soegvaerelse/mine"),
        (ind, "/intern/soegvaerelse/admin"),
    ]
    for user, path in checks:
        c = Client()
        c.force_login(user)
        html = c.get(path).content.decode()
        for marker in ("{#", "#}", "{% comment", "{{", "{%"):
            assert marker not in html, f"{path} leaked template syntax: {marker}"


@pytest.mark.django_db
def test_computed_orlov_counts_months_off_the_list(make_resident: Callable) -> None:
    """Auto-orlov = every month between move-in and the active period with no alumneliste row."""
    from residents.models import active_period
    from rooms.views_soegvaerelse import computed_orlov_months

    r = make_resident(email="orlov@gahk.dk", move_in_date=date(2024, 8, 1))
    rooms = [Room.objects.create(legacy_index=900 + i, number=900 + i, floor="s", side="x") for i in range(3)]
    # On the list Aug, Oct, Dec 2024 — gaps in Sep and Nov (the newest, Dec 2024, becomes active).
    for (yy, mm), rm in zip([(2024, 8), (2024, 10), (2024, 12)], rooms, strict=False):
        Residency.objects.create(resident=r, room=rm, year=yy, month=mm)

    y, m = active_period()
    window = (month_index(y, m) - month_index(2024, 8)) + 1
    assert computed_orlov_months(r) == window - 3  # 3 months present → the rest is orlov
    assert computed_orlov_months(r) == 2  # concretely: Sep + Nov 2024


@pytest.mark.django_db
def test_apply_form_prefills_computed_orlov(make_resident: Callable) -> None:
    r = make_resident(email="orlovform@gahk.dk", move_in_date=date(2024, 8, 1))
    for (yy, mm), i in zip([(2024, 8), (2024, 12)], range(2), strict=False):
        room = Room.objects.create(legacy_index=920 + i, number=920 + i, floor="s", side="x")
        Residency.objects.create(resident=r, room=room, year=yy, month=mm)
    c = Client()
    c.force_login(r)
    html = c.get(reverse("soegvaerelse:soeg")).content.decode()
    assert "Automatisk beregnet" in html
    assert 'name="orlov_months" value="3"' in html  # Sep–Nov 2024 = 3 months off the list, pre-filled


@pytest.mark.django_db
def test_detail_shows_suggested_vs_submitted_orlov_to_indstilling(make_resident: Callable) -> None:
    """Indstillingen sees the resident's declared orlov next to the auto-computed suggestion."""
    from rooms.models import KvotientOrlov

    resident = make_resident(email="applicant@gahk.dk", move_in_date=date(2024, 8, 1))
    for (yy, mm), i in zip([(2024, 8), (2024, 12)], range(2), strict=False):  # suggestion = 3 (Sep–Nov)
        room = Room.objects.create(legacy_index=940 + i, number=940 + i, floor="s", side="x")
        Residency.objects.create(resident=resident, room=room, year=yy, month=mm)
    target = month_index(*next_period())
    app = KvotientApplication.objects.create(
        resident=resident,
        move_month=target,
        move_in_month=month_index(2024, 8),
        done_studying_month=target + 12,
        k=50,
    )
    KvotientOrlov.objects.create(application=app, start_month=target, end_month=target + 1)  # declared 1

    ind = make_resident(email="ind-orlov@gahk.dk", roles=["indstilling"])
    c = Client()
    c.force_login(ind)
    html = c.get(reverse("soegvaerelse:detail", args=[app.id])).content.decode()
    assert "Orlov (kontrol)" in html
    assert "angivet <strong>1</strong>" in html and "beregnet <strong>3</strong>" in html
    assert "beboeren har ændret værdien" in html  # 1 ≠ 3 → mismatch flagged

    # The owner does not see the control line.
    owner_c = Client()
    owner_c.force_login(resident)
    owner_html = owner_c.get(reverse("soegvaerelse:detail", args=[app.id])).content.decode()
    assert "Orlov (kontrol)" not in owner_html
    assert "<strong>Orlov:</strong> 1 måned" in owner_html  # shown as a number of months, not a range
