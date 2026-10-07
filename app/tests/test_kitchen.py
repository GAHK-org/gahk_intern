"""Køkkenvagter (spec/features/kitchen-duty.md): calendar, ledger, enrolment, trading, completion,
absence, administration and the personal calendar feed."""

import datetime
from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import Room
from events.models import CalendarFeedToken
from kitchen import services
from kitchen.models import KitchenAssignment, KitchenPointEntry, KitchenShift
from residents.models import Residency, Role, active_period

pytestmark = pytest.mark.django_db


def _shift(
    starts_in: datetime.timedelta, *, hours: int = 1, spots: int = 1, points: int = 3, **extra: object
) -> KitchenShift:
    start = timezone.now() + starts_in
    return KitchenShift.objects.create(
        kind=KitchenShift.Kind.SPECIAL,
        date=timezone.localtime(start).date(),
        starts_at=start,
        ends_at=start + datetime.timedelta(hours=hours),
        spots=spots,
        points_per_spot=points,
        **extra,
    )


def _end(shift: KitchenShift) -> None:
    """Move a shift into the recent past so it can be completed."""
    shift.starts_at = timezone.now() - datetime.timedelta(hours=2)
    shift.ends_at = timezone.now() - datetime.timedelta(hours=1)
    shift.save()


def _balance(resident: object) -> int:
    return KitchenPointEntry.balance_for(resident.pk)  # type: ignore[attr-defined]


def test_free_spots_are_coloured_by_how_soon_the_shift_starts() -> None:
    from kitchen.views import _urgency

    now = timezone.now()
    assert _urgency(_shift(datetime.timedelta(hours=5)), now) == "critical"
    assert _urgency(_shift(datetime.timedelta(hours=48)), now) == "soon"
    assert _urgency(_shift(datetime.timedelta(days=5)), now) == ""
    assert _urgency(_shift(-datetime.timedelta(hours=1)), now) == ""


def test_standard_calendar_is_generated_three_months_ahead_and_idempotent() -> None:
    created = services.ensure_calendar()
    assert created > 0
    assert KitchenShift.objects.filter(date=services.calendar_horizon()).count() == 3
    assert services.ensure_calendar() == 0

    friday = KitchenShift.objects.filter(kind="evening", date__week_day=6).first()  # Django: Sunday=1
    sunday = KitchenShift.objects.filter(kind="evening", date__week_day=1).first()
    saturday = KitchenShift.objects.filter(kind="evening", date__week_day=7).first()
    monday_morning = KitchenShift.objects.filter(kind="morning", date__week_day=2).first()
    assert (friday.points_per_spot, friday.spots) == (4, 2)
    assert (sunday.points_per_spot, sunday.spots) == (2, 1)
    assert (saturday.points_per_spot, saturday.spots) == (1, 1)
    assert timezone.localtime(monday_morning.starts_at).time() == datetime.time(6, 30)


def test_ledger_records_balance_before_and_after(make_resident: Callable) -> None:
    r = make_resident()
    services.post_entry(r.pk, 5, "a", KitchenPointEntry.Kind.MANUAL)
    entry = services.post_entry(r.pk, -7, "b", KitchenPointEntry.Kind.MANUAL)
    assert (entry.balance_before, entry.amount, entry.balance_after) == (5, -7, -2)
    assert _balance(r) == -2
    assert KitchenPointEntry.balances()[r.pk] == -2


def test_monthly_charge_hits_current_residents_once(make_resident: Callable) -> None:
    year, month = active_period()
    member = make_resident(email="m@gahk.dk")
    outsider = make_resident(email="o@gahk.dk")
    Residency.objects.create(
        resident=member,
        room=Room.objects.create(legacy_index=1, number=1, floor="stuen", side="mod gaden"),
        year=year,
        month=month,
    )
    assert services.apply_monthly_charge(year, month) == 1
    assert services.apply_monthly_charge(year, month) == 0
    assert _balance(member) == -4
    assert _balance(outsider) == 0


def test_single_holder_of_multi_spot_shift_gets_all_spots(make_resident: Callable) -> None:
    r = make_resident()
    shift = _shift(datetime.timedelta(days=1), spots=2, points=3)
    services.enroll(shift, r)
    _end(shift)
    assert services.complete_finished_shifts() == 1
    assert _balance(r) == 6
    assert services.complete_finished_shifts() == 0  # never booked twice


def test_two_holders_each_get_their_own_spot(make_resident: Callable) -> None:
    a, b = make_resident(email="a@gahk.dk"), make_resident(email="b@gahk.dk")
    shift = _shift(datetime.timedelta(days=1), spots=2, points=3)
    services.enroll(shift, a)
    services.enroll(shift, b)
    _end(shift)
    services.complete_finished_shifts()
    assert (_balance(a), _balance(b)) == (3, 3)


def test_bonus_points_are_awarded_on_top(make_resident: Callable) -> None:
    r = make_resident()
    shift = _shift(datetime.timedelta(days=1), points=3)
    services.enroll(shift, r)
    services.set_bonus(shift, 2)
    _end(shift)
    services.complete_finished_shifts()
    assert _balance(r) == 5


def test_full_shift_refuses_but_multi_spot_holder_can_be_overridden(make_resident: Callable) -> None:
    a, b, c = (make_resident(email=f"{x}@gahk.dk") for x in "abc")
    shift = _shift(datetime.timedelta(days=1), spots=2)
    services.enroll(shift, a, spots=2)
    services.enroll(shift, b)
    assert KitchenAssignment.objects.get(resident=a).spots == 1
    assert KitchenAssignment.objects.get(resident=b).spots == 1
    with pytest.raises(services.ShiftError):
        services.enroll(shift, c)


def test_disabled_or_started_shift_cannot_be_taken(make_resident: Callable) -> None:
    r = make_resident()
    shift = _shift(datetime.timedelta(days=1))
    services.disable_shift(shift, "Køkkenet er lukket")
    with pytest.raises(services.ShiftError):
        services.enroll(shift, r)
    started = _shift(-datetime.timedelta(minutes=10))
    with pytest.raises(services.ShiftError):
        services.enroll(started, r)


def test_unenroll_only_more_than_30_days_ahead(make_resident: Callable) -> None:
    r = make_resident()
    far = _shift(datetime.timedelta(days=40))
    near = _shift(datetime.timedelta(days=10))
    services.enroll(far, r)
    services.enroll(near, r)
    KitchenAssignment.objects.update(self_enrolled_at=timezone.now() - datetime.timedelta(minutes=11))
    services.unenroll(KitchenAssignment.objects.get(shift=far))
    with pytest.raises(services.ShiftError):
        services.unenroll(KitchenAssignment.objects.get(shift=near))


def test_fresh_signup_can_be_regretted_unless_shift_is_within_an_hour(make_resident: Callable) -> None:
    r = make_resident()
    tomorrow = _shift(datetime.timedelta(days=1))
    imminent = _shift(datetime.timedelta(minutes=50))
    services.enroll(tomorrow, r)
    services.enroll(imminent, r)
    services.unenroll(KitchenAssignment.objects.get(shift=tomorrow))
    with pytest.raises(services.ShiftError):
        services.unenroll(KitchenAssignment.objects.get(shift=imminent))


def test_bought_shift_is_not_in_the_regret_window(make_resident: Callable) -> None:
    seller, buyer = make_resident(email="s@gahk.dk"), make_resident(email="k@gahk.dk")
    shift = _shift(datetime.timedelta(days=2))
    services.enroll(shift, seller)
    assignment = KitchenAssignment.objects.get(resident=seller)
    services.put_for_sale(assignment, 3)
    services.buy(assignment, buyer)
    with pytest.raises(services.ShiftError):
        services.unenroll(KitchenAssignment.objects.get(resident=buyer))


def test_trading_moves_shift_and_extra_points(make_resident: Callable) -> None:
    seller, buyer = make_resident(email="s@gahk.dk"), make_resident(email="k@gahk.dk")
    shift = _shift(datetime.timedelta(days=2), points=3)
    services.enroll(shift, seller)
    assignment = KitchenAssignment.objects.get(resident=seller)
    with pytest.raises(services.ShiftError):
        services.put_for_sale(assignment, 4)
    services.put_for_sale(assignment, 2)
    services.buy(assignment, buyer)
    assert KitchenAssignment.objects.get(shift=shift).resident == buyer
    assert (_balance(seller), _balance(buyer)) == (-2, 2)
    _end(shift)
    services.complete_finished_shifts()
    assert (_balance(seller), _balance(buyer)) == (-2, 5)


def test_absence_reverts_points_and_fines(make_resident: Callable) -> None:
    admin, r = make_resident(email="adm@gahk.dk"), make_resident()
    shift = _shift(datetime.timedelta(days=1), points=3)
    services.enroll(shift, r)
    _end(shift)
    services.complete_finished_shifts()
    services.mark_absent(KitchenAssignment.objects.get(resident=r), None, admin)
    assert _balance(r) == -3
    with pytest.raises(services.ShiftError):
        services.mark_absent(KitchenAssignment.objects.get(resident=r), None, admin)


def test_absence_fine_can_be_adjusted_and_window_is_seven_days(make_resident: Callable) -> None:
    admin, r = make_resident(email="adm@gahk.dk"), make_resident()
    shift = _shift(datetime.timedelta(days=1), points=3)
    services.enroll(shift, r)
    _end(shift)
    services.mark_absent(KitchenAssignment.objects.get(resident=r), 1, admin)  # completes first
    assert _balance(r) == -1

    old = _shift(-datetime.timedelta(days=8))
    KitchenAssignment.objects.create(shift=old, resident=r)
    with pytest.raises(services.ShiftError):
        services.mark_absent(KitchenAssignment.objects.get(shift=old), None, admin)


def test_admin_pages_require_kitchen_role(make_resident: Callable) -> None:
    resident = make_resident(email="r@gahk.dk")
    officer = make_resident(email="k@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    client = Client()
    client.force_login(resident)
    assert client.get(reverse("kitchen:market")).status_code == 200
    assert client.get(reverse("kitchen:account")).status_code == 200
    assert client.get(reverse("kitchen:admin")).status_code == 403

    client.force_login(officer)
    assert client.get(reverse("kitchen:admin")).status_code == 200
    day = timezone.localdate() + datetime.timedelta(days=5)
    response = client.post(
        reverse("kitchen:admin"),
        {
            "description": "Julefrokost",
            "date": day.isoformat(),
            "start": "16:00",
            "end": "23:00",
            "points_per_spot": 5,
            "spots": 4,
        },
    )
    special = KitchenShift.objects.get(kind="special")
    assert response.status_code == 302
    assert (special.title, special.spots, special.points_per_spot) == ("Julefrokost", 4, 5)
    assert client.get(reverse("kitchen:shift", args=[special.pk])).status_code == 200

    client.post(reverse("kitchen:admin_resident", args=[resident.pk]), {"amount": "3", "message": "Ekstra"})
    assert _balance(resident) == 3


def test_resident_enrolls_through_the_view(make_resident: Callable) -> None:
    r = make_resident()
    shift = _shift(datetime.timedelta(days=3))
    client = Client()
    client.force_login(r)
    client.post(reverse("kitchen:enroll", args=[shift.pk]), {"spots": "1"})
    assert KitchenAssignment.objects.filter(shift=shift, resident=r).exists()


def test_sheet_shows_slots_and_take_button_returns_to_it(make_resident: Callable) -> None:
    r = make_resident(first_name="Vilma")
    services.ensure_calendar()
    shift = KitchenShift.objects.filter(kind="evening", starts_at__gt=timezone.now()).first()
    client = Client()
    client.force_login(r)
    page = client.get(reverse("kitchen:market")).content.decode()
    assert "17:30–21:00" in page and "+ Tag" in page
    assert "data-duty-confirm=" in page and "data-duty-dialog" in page

    response = client.post(reverse("kitchen:enroll", args=[shift.pk]), {"spots": "1", "next": "market"})
    assert response["Location"].endswith(f"#dag-{shift.date:%Y-%m-%d}")
    page = client.get(response["Location"]).content.decode()
    assert "duty-slot is-mine" in page and "Vilma" in page


def test_extra_spots_of_a_multi_spot_holder_render_as_overridable(make_resident: Callable) -> None:
    holder = make_resident(email="h@gahk.dk", first_name="Holger")
    other = make_resident(email="o@gahk.dk")
    services.ensure_calendar()
    shift = KitchenShift.objects.filter(kind="evening", spots=2, starts_at__gt=timezone.now()).first()
    services.enroll(shift, holder, spots=2)
    client = Client()

    client.force_login(holder)  # the holder never sees their own spots as overridable
    page = client.get(reverse("kitchen:market")).content.decode()
    assert "duty-take-label" not in page

    client.force_login(other)
    page = client.get(reverse("kitchen:market")).content.decode()
    assert page.count("duty-take-label") == 1
    client.post(reverse("kitchen:enroll", args=[shift.pk]), {"spots": "1", "next": "market"})
    assert KitchenAssignment.objects.get(resident=holder).spots == 1
    assert KitchenAssignment.objects.get(resident=other).spots == 1


def test_sheet_shows_30_days_and_loads_more_up_to_the_horizon(make_resident: Callable) -> None:
    client = Client()
    client.force_login(make_resident())
    today = timezone.localdate()
    page = client.get(reverse("kitchen:market")).content.decode()
    assert f'id="dag-{today + datetime.timedelta(days=30):%Y-%m-%d}"' in page
    assert f'id="dag-{today + datetime.timedelta(days=31):%Y-%m-%d}"' not in page
    assert f"?fra={today + datetime.timedelta(days=31):%Y-%m-%d}" in page

    horizon = services.calendar_horizon()
    last = client.get(reverse("kitchen:sheet_rows"), {"fra": f"{horizon:%Y-%m-%d}"}).content.decode()
    assert f'id="dag-{horizon:%Y-%m-%d}"' in last and "duty-more" not in last

    # A loaded chunk only gets a month heading where a month actually begins.
    fra = today + datetime.timedelta(days=30)
    chunk = client.get(reverse("kitchen:sheet_rows"), {"fra": f"{fra:%Y-%m-%d}"}).content.decode()
    firsts = sum((fra + datetime.timedelta(days=i)).day == 1 for i in range(30))
    assert chunk.count('class="duty-month"') == firsts


def test_sheet_marks_where_free_unenrolment_starts(make_resident: Callable) -> None:
    client = Client()
    client.force_login(make_resident())
    cutoff = timezone.localdate() + datetime.timedelta(days=services.FREE_UNENROLL_DAYS)
    page = client.get(reverse("kitchen:market")).content.decode()
    assert page.count('class="duty-ruler"') == 1
    ruler, cutoff_row = page.index("duty-ruler"), page.index(f'id="dag-{cutoff:%Y-%m-%d}"')
    assert ruler < cutoff_row
    assert page.index(f'id="dag-{cutoff - datetime.timedelta(days=1):%Y-%m-%d}"') < ruler


def test_history_pages_through_the_past(make_resident: Callable) -> None:
    r = make_resident()
    old = _shift(-datetime.timedelta(days=40))
    KitchenAssignment.objects.create(shift=old, resident=r)
    client = Client()
    client.force_login(r)
    first = client.get(reverse("kitchen:history")).content.decode()
    assert f'id="dag-{old.date:%Y-%m-%d}"' not in first and "Ældre" in first
    older = timezone.localdate() - datetime.timedelta(days=30)
    page = client.get(reverse("kitchen:history"), {"til": f"{older:%Y-%m-%d}"}).content.decode()
    assert f'id="dag-{old.date:%Y-%m-%d}"' in page and "Nyere" in page


def test_assigned_shift_appears_in_personal_calendar_feed(make_resident: Callable) -> None:
    r = make_resident()
    shift = _shift(datetime.timedelta(days=3))
    services.enroll(shift, r)
    token = CalendarFeedToken.for_resident(r)
    body = Client().get(reverse("events_feed", args=[token.token])).content.decode()
    assert f"UID:koekkenvagt-{shift.pk}@gahk.dk" in body
