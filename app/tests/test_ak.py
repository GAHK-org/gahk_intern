"""AK monthly kryds deduction — officer-controlled per-calendar-month schedule + reconciliation.

The schedule (`AkMonthlyCharge`) has one row per calendar month (1..12), seeded on/2 by the migration;
the amount is the same every year. Applying a period charges that calendar month's amount to the
residents on that month's alumneliste.
"""

from collections.abc import Callable

import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import IntegrityError
from django.test import Client
from django.utils import timezone

from ak.models import AkAutoApply, AkEntry, AkMonthlyCharge
from ak.services import apply_monthly_charge, charge_period, ensure_previous_month_charged
from core.models import Room
from residents.models import Residency, Role, active_period, prev_period

_room_seq = iter(range(1, 10_000))


def _room() -> Room:
    n = next(_room_seq)
    return Room.objects.create(legacy_index=n, number=n, floor="stuen", side="mod gaden")


def _place(resident: object, year: int, month: int) -> Residency:
    return Residency.objects.create(resident=resident, room=_room(), year=year, month=month)


def _monthly(resident: object, year: int, month: int) -> AkEntry | None:
    return AkEntry.objects.filter(
        resident=resident, kind=AkEntry.Kind.MONTHLY, year=year, month=month
    ).first()


def _set_schedule(month: int, *, active: bool = True, krydser: int = 2) -> None:
    AkMonthlyCharge.objects.update_or_create(month=month, defaults={"active": active, "krydser": krydser})


@pytest.mark.django_db
def test_schedule_is_seeded_for_all_twelve_months() -> None:
    """The migration seeds a row per calendar month so the schedule is always complete."""
    assert AkMonthlyCharge.objects.count() == 12
    assert set(AkMonthlyCharge.objects.values_list("month", flat=True)) == set(range(1, 13))


@pytest.mark.django_db
def test_apply_charges_members_only_and_is_idempotent(make_resident: Callable) -> None:
    """A charged month deducts krydser from residents on that month's alumneliste; non-members are
    never charged (requirement #3). Re-applying does not double-charge."""
    y, m = active_period()
    _set_schedule(m, active=True, krydser=2)
    member = make_resident(email="member@gahk.dk")
    _place(member, y, m)
    outsider = make_resident(email="outsider@gahk.dk")  # no residency for (y, m)

    written, removed = apply_monthly_charge(y, m)
    assert (written, removed) == (1, 0)
    assert _monthly(member, y, m).delta == -2
    assert AkEntry.balance_for(member) == -2
    assert _monthly(outsider, y, m) is None  # not on the list → not charged

    apply_monthly_charge(y, m)  # idempotent
    assert AkEntry.objects.filter(resident=member, kind=AkEntry.Kind.MONTHLY, year=y, month=m).count() == 1
    assert AkEntry.balance_for(member) == -2


@pytest.mark.django_db
def test_amount_is_the_same_every_year(make_resident: Callable) -> None:
    """The calendar-month schedule applies the same amount across different years."""
    y, m = active_period()
    _set_schedule(m, active=True, krydser=4)
    r = make_resident(email="yoy@gahk.dk")
    _place(r, y, m)
    _place(r, y + 1, m)  # same calendar month, next year

    apply_monthly_charge(y, m)
    apply_monthly_charge(y + 1, m)
    assert _monthly(r, y, m).delta == -4
    assert _monthly(r, y + 1, m).delta == -4  # same amount year on year


@pytest.mark.django_db
def test_unique_monthly_per_period_enforced(make_resident: Callable) -> None:
    y, m = active_period()
    r = make_resident(email="dup@gahk.dk")
    AkEntry.objects.create(resident=r, delta=-2, kind=AkEntry.Kind.MONTHLY, year=y, month=m)
    with pytest.raises(IntegrityError):
        AkEntry.objects.create(resident=r, delta=-2, kind=AkEntry.Kind.MONTHLY, year=y, month=m)


@pytest.mark.django_db
def test_amount_change_disable_and_removal(make_resident: Callable) -> None:
    y, m = active_period()
    _set_schedule(m, active=True, krydser=2)
    r = make_resident(email="rr@gahk.dk")
    residency = _place(r, y, m)

    apply_monthly_charge(y, m)
    assert _monthly(r, y, m).delta == -2

    _set_schedule(m, active=True, krydser=3)  # change amount → updates the same entry
    apply_monthly_charge(y, m)
    assert _monthly(r, y, m).delta == -3
    assert AkEntry.objects.filter(resident=r, kind=AkEntry.Kind.MONTHLY, year=y, month=m).count() == 1

    _set_schedule(m, active=False)  # disabling a month removes its charges
    written, removed = apply_monthly_charge(y, m)
    assert (written, removed) == (0, 1)
    assert _monthly(r, y, m) is None

    _set_schedule(m, active=True, krydser=2)
    apply_monthly_charge(y, m)
    assert _monthly(r, y, m) is not None
    residency.delete()  # removed from the list → charge dropped on re-apply
    apply_monthly_charge(y, m)
    assert _monthly(r, y, m) is None


@pytest.mark.django_db
def test_save_persists_the_schedule_and_books_nothing(make_resident: Callable) -> None:
    """Saving the schedule persists all 12 months but charges nothing: the month in progress is charged
    once it is over, and months already charged are never touched."""
    y, m = active_period()
    py, pm = prev_period((y, m))
    officer = make_resident(email="off@gahk.dk", roles=[Role.AK])
    now_member = make_resident(email="nowm@gahk.dk")
    _place(now_member, y, m)
    past_member = make_resident(email="pastm@gahk.dk")
    _place(past_member, py, pm)

    payload = {f"active_{mo}": "1" for mo in range(1, 13)}
    payload.update({f"krydser_{mo}": "2" for mo in range(1, 13)})
    payload[f"krydser_{m}"] = "3"

    c = Client()
    c.force_login(officer)
    resp = c.post("/intern/ak/admin/gem-maaneder", payload)
    assert resp.status_code == 302

    assert AkMonthlyCharge.objects.get(month=m).krydser == 3  # schedule persisted
    assert _monthly(now_member, y, m) is None  # month in progress: charged when it ends
    assert _monthly(past_member, py, pm) is None  # past month untouched


@pytest.mark.django_db
def test_overview_and_save_role_gated(make_resident: Callable) -> None:
    plain = make_resident(email="plain-ak@gahk.dk")
    officer = make_resident(email="off2@gahk.dk", roles=[Role.AK])

    plain_c = Client()
    plain_c.force_login(plain)
    assert plain_c.get("/intern/ak/admin").status_code == 403
    assert plain_c.post("/intern/ak/admin/gem-maaneder").status_code == 403

    off_c = Client()
    off_c.force_login(officer)
    assert off_c.get("/intern/ak/admin").status_code == 200
    assert off_c.get("/intern/ak/admin/gem-maaneder").status_code == 405  # POST only


@pytest.mark.django_db
def test_overview_shows_twelve_month_schedule(make_resident: Callable) -> None:
    officer = make_resident(email="off3@gahk.dk", roles=[Role.AK])
    c = Client()
    c.force_login(officer)
    html = c.get("/intern/ak/admin").content.decode()
    for mo in range(1, 13):
        assert f'name="active_{mo}"' in html
    assert "Januar" in html and "December" in html
    assert "{%" not in html and "{#" not in html  # no template leak


@pytest.mark.django_db
def test_charge_period_is_the_month_before_today() -> None:
    today = timezone.localdate()
    assert charge_period() == prev_period((today.year, today.month))


@pytest.mark.django_db
def test_lazy_charge_books_last_month_once(make_resident: Callable) -> None:
    """Last month is charged lazily and only once — the marker stops it re-running every request."""
    y, m = charge_period()
    _set_schedule(m, active=True, krydser=2)
    member = make_resident(email="lazy@gahk.dk")
    _place(member, y, m)

    ensure_previous_month_charged()
    assert _monthly(member, y, m).delta == -2
    state = AkAutoApply.get()
    assert (state.year, state.month) == (y, m)

    ensure_previous_month_charged()  # marker already set → no-op
    assert AkEntry.objects.filter(resident=member, kind=AkEntry.Kind.MONTHLY, year=y, month=m).count() == 1


@pytest.mark.django_db
def test_lazy_charge_does_not_touch_the_month_in_progress(make_resident: Callable) -> None:
    y, m = active_period()
    _set_schedule(m, active=True, krydser=2)
    member = make_resident(email="inprogress@gahk.dk")
    _place(member, y, m)

    ensure_previous_month_charged()
    assert _monthly(member, y, m) is None


@pytest.mark.django_db
def test_lazy_charge_runs_when_a_new_month_has_started(make_resident: Callable) -> None:
    """When the marker is older than last month (a new month started), it charges."""
    y, m = charge_period()
    _set_schedule(m, active=True, krydser=2)
    member = make_resident(email="adv@gahk.dk")
    _place(member, y, m)
    state = AkAutoApply.get()
    state.year, state.month = prev_period((y, m))  # marker stuck on the month before
    state.save()

    ensure_previous_month_charged()
    assert _monthly(member, y, m).delta == -2


@pytest.mark.django_db
def test_a_resident_removed_during_the_month_is_not_charged(make_resident: Callable) -> None:
    """The bug this design fixes: the month's list starts as a copy of the one before, and someone who
    moved out is removed partway through. Charging from the final list means they never owe it."""
    y, m = charge_period()
    _set_schedule(m, active=True, krydser=2)
    stays = make_resident(email="stays@gahk.dk")
    _place(stays, y, m)
    left = make_resident(email="left@gahk.dk")
    _place(left, y, m)
    Residency.objects.filter(resident=left, year=y, month=m).delete()  # removed before the month ended

    ensure_previous_month_charged()
    assert _monthly(stays, y, m).delta == -2
    assert _monthly(left, y, m) is None


@pytest.mark.django_db
def test_ak_page_visit_triggers_lazy_charge(make_resident: Callable) -> None:
    """Loading an internal AK page charges last month even if the scheduled command never runs."""
    y, m = charge_period()
    _set_schedule(m, active=True, krydser=2)
    member = make_resident(email="visit@gahk.dk")
    _place(member, y, m)
    assert _monthly(member, y, m) is None  # nothing booked yet

    c = Client()
    c.force_login(member)
    c.get("/intern/ak/")  # my_ak → ensure_previous_month_charged
    assert _monthly(member, y, m).delta == -2


@pytest.mark.django_db
def test_management_command_charges_last_month(make_resident: Callable) -> None:
    y, m = charge_period()
    _set_schedule(m, active=True, krydser=2)
    r = make_resident(email="cmd@gahk.dk")
    _place(r, y, m)
    call_command("ak_monthly_assessment")
    assert _monthly(r, y, m).delta == -2
    assert (AkAutoApply.get().year, AkAutoApply.get().month) == (y, m)
    call_command("ak_monthly_assessment")  # idempotent
    assert AkEntry.objects.filter(resident=r, kind=AkEntry.Kind.MONTHLY, year=y, month=m).count() == 1


def test_ak_charge_and_oelkaelder_statements_run_on_the_first() -> None:
    """Both settle the previous month, so both are scheduled for the 1st."""
    schedule = settings.CELERY_BEAT_SCHEDULE
    for name in ("apply-ak-monthly-assessment", "email-oelkaelder-monthly-statements"):
        assert schedule[name]["schedule"].day_of_month == {1}, name


@pytest.mark.django_db
@pytest.mark.parametrize("delta", ["0", "abc", "2,5", ""])
def test_officer_add_entry_rejects_zero_or_non_numeric_out_loud(make_resident: Callable, delta: str) -> None:
    """add_entry used to drop these silently and redirect as if saved, so the officer believed the
    resident had been credited."""
    officer = make_resident(email="add-off@gahk.dk", roles=[Role.AK])
    r = make_resident(email="add-r@gahk.dk")
    c = Client()
    c.force_login(officer)

    resp = c.post(f"/intern/ak/log/{r.pk}/add", {"delta": delta, "reason": "x"}, follow=True)

    assert [m.level_tag for m in resp.context["messages"]] == ["error"]
    assert not AkEntry.objects.filter(resident=r).exists()


@pytest.mark.django_db
def test_officer_add_entry_confirms_success(make_resident: Callable) -> None:
    officer = make_resident(email="add-off2@gahk.dk", roles=[Role.AK])
    r = make_resident(email="add-r2@gahk.dk")
    c = Client()
    c.force_login(officer)

    resp = c.post(f"/intern/ak/log/{r.pk}/add", {"delta": "+2", "reason": "Havearbejde"}, follow=True)

    assert [m.level_tag for m in resp.context["messages"]] == ["success"]
    assert AkEntry.balance_for(r) == 2


@pytest.mark.django_db
def test_missing_schedule_row_means_the_same_on_page_load_as_in_the_command(make_resident: Callable) -> None:
    """The command created a default (on, 2) row for a month with none; the page-load backstop instead
    took "missing" as "off" and deleted every charge for the period. Both now create the default."""
    y, m = charge_period()
    member = make_resident(email="missing-row@gahk.dk")
    _place(member, y, m)
    AkMonthlyCharge.objects.filter(month=m).delete()

    ensure_previous_month_charged()

    charge = AkMonthlyCharge.objects.get(month=m)
    assert (charge.active, charge.krydser) == (True, 2)
    assert _monthly(member, y, m).delta == -2


@pytest.mark.django_db
def test_officer_delete_asks_for_confirmation(make_resident: Callable) -> None:
    officer = make_resident(email="del-off@gahk.dk", roles=[Role.AK])
    r = make_resident(email="del-r@gahk.dk")
    AkEntry.objects.create(resident=r, delta=3, kind=AkEntry.Kind.LABOUR, reason="Maling")
    c = Client()
    c.force_login(officer)

    html = c.get(f"/intern/ak/log/{r.pk}").content.decode()

    form = html.split('action="/intern/ak/log/')[1].split(">")[0]  # the add form comes first
    delete_form = html.split("/delete/")[1].split(">")[0]
    assert "confirm(" not in form
    assert "onsubmit=\"return confirm('Slet +3 kryds" in delete_form
