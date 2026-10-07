"""Køkkenvagter P3 step 2 -- away ranges (`Fravaer`) and the summer page's "Mit fravær"/"Væk denne uge"
sections, and step 3 -- summer shift claiming (`claim_vagt`), the grid, Køkkengruppen's unclaimed list and
generation from 1 May. Design: `docs/plans/2026-10-04-koekkenvagter-p3-design.md` §2/§3/§4/§6/§7. The
real-Postgres claim races live in test_koekkenvagter_concurrency.py.

Summer 2043: 1 Jul is a Wednesday (ISO week 27), 31 Jul a Friday, 1 Aug a Saturday (same week 31 as
31 Jul), and 31 Aug a Monday -- so the first and last weeks are both partial.
"""

import ast
import inspect
import re
import textwrap
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.db.models import Count
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.clock import clear_cache
from core.models import DevClock, Room
from koekken import services
from koekken.models import (
    Fravaer,
    Fridag,
    KoekkenPost,
    Periode,
    Vagt,
    VagtTildeling,
)
from koekken.services import (
    KoekkenAllocationError,
    add_fravaer,
    away_by_week,
    can_claim,
    claim_vagt,
    delete_fravaer,
    generate_vagter,
    mark_udfoert,
    offer_tildeling,
    periodes_to_generate,
    projected_balance_for,
    resident_fravaer,
    resolve_periode,
    summer_bounds,
    summer_grid,
    summer_link_visible,
    take_over,
    target_summer,
    todays_tildelinger,
    unclaimed_summer_vagter,
)
from residents.models import Residency, Resident, Role

pytestmark = pytest.mark.django_db

Y = 2043
TODAY = date(Y, 6, 1)  # before the summer: nothing is "already ended"
SUMMER = target_summer(TODAY)


@pytest.fixture
def clock(settings: object) -> Iterator[Callable[[date], None]]:
    settings.DEBUG = True  # type: ignore[attr-defined]

    def _set(d: date) -> None:
        DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": d})

    _set(TODAY)
    yield _set
    clear_cache()


@pytest.fixture
def anna(make_resident: Callable) -> Resident:
    return make_resident(email="anna@gahk.dk", first_name="Anna", last_name="Hansen")


@pytest.fixture
def bo(make_resident: Callable) -> Resident:
    return make_resident(email="bo@gahk.dk", first_name="Bo", last_name="Lund")


def _refuses(*args: object, **kwargs: object) -> str:
    n = Fravaer.objects.count()
    with pytest.raises(KoekkenAllocationError) as exc:
        add_fravaer(*args, **kwargs)  # type: ignore[arg-type]
    assert Fravaer.objects.count() == n
    return str(exc.value)


# ------------------------------------------------------------------------------------- model


def test_check_constraint_rejects_start_after_end(anna: Resident) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Fravaer.objects.create(resident=anna, start_date=date(Y, 7, 10), end_date=date(Y, 7, 9))
    Fravaer.objects.create(resident=anna, start_date=date(Y, 7, 10), end_date=date(Y, 7, 10))  # one day ok


# ------------------------------------------------------------------------------------- add_fravaer


def test_add_inside_cross_boundary_and_ongoing(anna: Resident, bo: Resident) -> None:
    inside = add_fravaer(anna, date(Y, 7, 10), date(Y, 7, 20), today=TODAY)
    assert (inside.start_date, inside.end_date) == (date(Y, 7, 10), date(Y, 7, 20))
    cross = add_fravaer(bo, date(Y, 6, 28), date(Y, 7, 3), today=TODAY)
    assert cross.pk
    ongoing = add_fravaer(anna, date(Y, 8, 1), date(Y, 8, 10), today=date(Y, 8, 5))
    assert ongoing.pk  # started before today, not yet ended
    wide = add_fravaer(bo, date(Y, 8, 20), date(Y, 9, 15), today=TODAY)
    assert wide.pk


def test_add_refusals_write_nothing(anna: Resident) -> None:
    assert _refuses(anna, date(Y, 7, 10), date(Y, 7, 9), today=TODAY) == (
        "Fra-datoen skal ligge før eller på til-datoen."
    )
    assert _refuses(anna, date(Y, 7, 1), date(Y, 7, 9), today=date(Y, 7, 10)) == "Fraværet er allerede slut."
    msg = f"Fravær kan kun registreres for sommerperioden {Y} (1. juli-31. august)."
    assert _refuses(anna, date(Y, 12, 20), date(Y + 1, 1, 5), today=TODAY) == msg
    assert _refuses(anna, date(Y, 6, 1), date(Y, 6, 30), today=TODAY) == msg
    assert _refuses(anna, date(Y, 9, 1), date(Y, 9, 5), today=TODAY) == msg


def test_add_refuses_range_in_non_target_summer(anna: Resident) -> None:
    """Regression: a range in another year's summer used to be accepted silently yet never displayed."""
    msg = f"Fravær kan kun registreres for sommerperioden {Y} (1. juli-31. august)."
    assert _refuses(anna, date(Y + 1, 7, 10), date(Y + 1, 7, 20), today=TODAY) == msg
    # after 31 August the target is next year's summer: this year's is now refused, next year's accepted
    after = date(Y, 9, 5)
    assert _refuses(anna, date(Y, 8, 20), date(Y, 9, 10), today=after).endswith(
        f"{Y + 1} (1. juli-31. august)."
    )
    assert add_fravaer(anna, date(Y + 1, 7, 10), date(Y + 1, 7, 20), today=after).pk


def test_add_refuses_overlap_with_own_range(anna: Resident, bo: Resident) -> None:
    add_fravaer(anna, date(Y, 7, 10), date(Y, 7, 20), today=TODAY)
    msg = _refuses(anna, date(Y, 7, 20), date(Y, 7, 25), today=TODAY)  # single shared day
    assert msg == "Overlapper dit fravær 10.\u201320. juli."
    assert _refuses(anna, date(Y, 7, 1), date(Y, 7, 10), today=TODAY).startswith("Overlapper")
    assert _refuses(anna, date(Y, 7, 12), date(Y, 7, 13), today=TODAY).startswith("Overlapper")
    assert _refuses(anna, date(Y, 7, 5), date(Y, 7, 30), today=TODAY).startswith("Overlapper")


def test_add_allows_adjacent_and_other_residents_overlap(anna: Resident, bo: Resident) -> None:
    add_fravaer(anna, date(Y, 7, 1), date(Y, 7, 10), today=TODAY)
    add_fravaer(anna, date(Y, 7, 11), date(Y, 7, 20), today=TODAY)  # adjacent, not an overlap
    add_fravaer(bo, date(Y, 7, 5), date(Y, 7, 15), today=TODAY)  # someone else's range
    assert Fravaer.objects.count() == 3


# ------------------------------------------------------------------------------------- delete


def test_delete_owner_ended_and_non_owner(anna: Resident, bo: Resident) -> None:
    f = add_fravaer(anna, date(Y, 7, 1), date(Y, 7, 10), today=TODAY)
    with pytest.raises(KoekkenAllocationError):
        delete_fravaer(f, bo, today=TODAY)
    with pytest.raises(KoekkenAllocationError) as exc:
        delete_fravaer(f, anna, today=date(Y, 7, 11))
    assert str(exc.value) == "Afsluttet fravær kan ikke slettes."
    assert Fravaer.objects.filter(pk=f.pk).exists()
    delete_fravaer(f, anna, today=date(Y, 7, 10))  # ends today: still deletable
    assert not Fravaer.objects.filter(pk=f.pk).exists()


def test_resident_fravaer_flags_and_scopes_to_target_summer(anna: Resident) -> None:
    a = add_fravaer(anna, date(Y, 7, 1), date(Y, 7, 10), today=TODAY)
    b = add_fravaer(anna, date(Y, 7, 20), date(Y, 7, 25), today=TODAY)
    Fravaer.objects.create(resident=anna, start_date=date(Y + 1, 7, 1), end_date=date(Y + 1, 7, 5))
    assert resident_fravaer(anna, today=date(Y, 7, 15)) == [(a, False), (b, True)]


# ------------------------------------------------------------------------------------- away_by_week


def _weeks(summer: Periode = SUMMER) -> dict[date, tuple[date, list[str]]]:
    return {first: (last, [r.full_name for r in rs]) for first, last, rs in away_by_week(summer)}


def test_weeks_cover_summer_with_clipped_edges(anna: Resident) -> None:
    rows = away_by_week(SUMMER)
    assert rows[0][:2] == (date(Y, 7, 1), date(Y, 7, 5))  # Wed-Sun
    assert rows[1][:2] == (date(Y, 7, 6), date(Y, 7, 12))
    assert rows[-1][:2] == (date(Y, 8, 31), date(Y, 8, 31))  # a lone Monday
    assert rows[0][0].isocalendar()[1] == 27 and len(rows) == 10
    add_fravaer(anna, date(Y, 8, 31), date(Y, 8, 31), today=TODAY)
    add_fravaer(anna, date(Y, 7, 1), date(Y, 7, 1), today=TODAY)
    w = _weeks()
    assert w[date(Y, 8, 31)][1] == ["Anna Hansen"] and w[date(Y, 7, 1)][1] == ["Anna Hansen"]
    assert w[date(Y, 7, 6)][1] == [] and w[date(Y, 8, 24)][1] == []


def test_weeks_month_boundary_same_week(anna: Resident, bo: Resident) -> None:
    add_fravaer(anna, date(Y, 7, 31), date(Y, 7, 31), today=TODAY)
    add_fravaer(bo, date(Y, 8, 1), date(Y, 8, 1), today=TODAY)
    w = _weeks()
    assert w[date(Y, 7, 27)] == (date(Y, 8, 2), ["Anna Hansen", "Bo Lund"])
    assert w[date(Y, 8, 3)][1] == [] and w[date(Y, 7, 20)][1] == []


def test_cross_boundary_range_only_in_summer_weeks(anna: Resident) -> None:
    add_fravaer(anna, date(Y, 6, 15), date(Y, 7, 7), today=TODAY)
    add_fravaer(anna, date(Y, 8, 28), date(Y, 9, 15), today=TODAY)
    named = {first for first, (_last, names) in _weeks().items() if names}
    assert named == {date(Y, 7, 1), date(Y, 7, 6), date(Y, 8, 24), date(Y, 8, 31)}
    assert min(first for first, *_ in away_by_week(SUMMER)) == date(Y, 7, 1)  # nothing in June


def test_two_ranges_in_one_week_listed_once(anna: Resident) -> None:
    add_fravaer(anna, date(Y, 7, 6), date(Y, 7, 7), today=TODAY)
    add_fravaer(anna, date(Y, 7, 9), date(Y, 7, 10), today=TODAY)
    assert _weeks()[date(Y, 7, 6)][1] == ["Anna Hansen"]


def test_moved_out_resident_hidden_after_move_out(anna: Resident, bo: Resident) -> None:
    add_fravaer(anna, date(Y, 7, 1), date(Y, 8, 31), today=TODAY)
    add_fravaer(bo, date(Y, 7, 1), date(Y, 8, 31), today=TODAY)
    Resident.objects.filter(pk=anna.pk).update(move_out_date=date(Y, 7, 15))
    w = _weeks()
    assert w[date(Y, 7, 13)][1] == ["Anna Hansen", "Bo Lund"]  # moves out inside this week
    assert w[date(Y, 7, 20)][1] == ["Bo Lund"]
    assert Fravaer.objects.filter(resident=anna).exists()  # rows are kept


def test_away_by_week_query_count_constant(make_resident: Callable) -> None:
    def count() -> int:
        with CaptureQueriesContext(connection) as ctx:
            away_by_week(SUMMER)
        return len(ctx)

    r = make_resident(email="q0@gahk.dk")
    add_fravaer(r, date(Y, 7, 1), date(Y, 7, 3), today=TODAY)
    one = count()
    for i in range(1, 8):
        r = make_resident(email=f"q{i}@gahk.dk")
        add_fravaer(r, date(Y, 7, 1 + i), date(Y, 7, 10 + i), today=TODAY)
    assert one == 1 and count() == 1


# ------------------------------------------------------------------------------------- target / link


def test_summer_bounds() -> None:
    assert summer_bounds(Y) == (date(Y, 7, 1), date(Y, 8, 31))


@pytest.mark.parametrize(
    ("today", "visible", "year"),
    [
        (date(Y, 4, 30), False, Y),
        (date(Y, 5, 1), True, Y),
        (date(Y, 8, 31), True, Y),
        (date(Y, 9, 1), False, Y + 1),
        (date(Y, 12, 31), False, Y + 1),
    ],
)
def test_target_summer_and_link_window(today: date, visible: bool, year: int) -> None:
    assert summer_link_visible(today) is visible
    assert target_summer(today).year == year


def test_target_summer_writes_nothing() -> None:
    before = Periode.objects.count()
    target_summer(date(Y, 5, 1))
    summer_link_visible(date(Y, 5, 1))
    assert Periode.objects.count() == before


# ------------------------------------------------------------------------------------- views

BASE = "/intern/koekken/"


def _login(r: Resident) -> Client:
    c = Client()
    c.force_login(r)
    return c


def _norm(html: str) -> str:
    html = re.sub(r'name="csrfmiddlewaretoken" value="[^"]+"', "", html)
    return re.sub(r"\s+", " ", html).strip()


def test_page_requires_login_and_renders_for_resident(anna: Resident, clock: Callable) -> None:
    assert Client().get(f"{BASE}sommer/").status_code in (301, 302, 403)
    resp = _login(anna).get(f"{BASE}sommer/")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert f"Sommer {Y}" in html and "Mit fravær" in html and "Væk denne uge" in html
    assert "Du har ikke registreret noget fravær i sommer." in html and "Ingen registreret" in html
    assert 'hx-target="#koekken-fravaer"' in html


def test_get_does_not_create_periode(anna: Resident, clock: Callable) -> None:
    Periode.objects.all().delete()
    _login(anna).get(f"{BASE}sommer/")
    _login(anna).get(BASE)
    assert not Periode.objects.filter(kind=Periode.Kind.SOMMER).exists()


def test_add_and_delete_via_htmx(anna: Resident, bo: Resident, clock: Callable) -> None:
    c = _login(anna)
    resp = c.post(f"{BASE}sommer/fravaer", {"fra": f"{Y}-07-06", "til": f"{Y}-07-12"}, HTTP_HX_REQUEST="true")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert '<div id="koekken-fravaer">' in html and "<html" not in html
    assert "mandag 6. juli – søndag 12. juli" in html
    f = Fravaer.objects.get(resident=anna)
    assert "Anna Hansen" in html and f"sommer/fravaer/{f.pk}/slet" in html
    resp = c.post(f"{BASE}sommer/fravaer/{f.pk}/slet", HTTP_HX_REQUEST="true")
    assert resp.status_code == 200 and not Fravaer.objects.exists()
    assert "Du har ikke registreret noget fravær i sommer." in resp.content.decode()


def test_errors_render_inside_partial(anna: Resident, clock: Callable) -> None:
    c = _login(anna)
    resp = c.post(f"{BASE}sommer/fravaer", {"fra": f"{Y}-07-12", "til": f"{Y}-07-06"})
    html = resp.content.decode()
    assert resp.status_code == 200 and "Fra-datoen skal ligge før eller på til-datoen." in html
    assert "<html" not in html
    resp = c.post(f"{BASE}sommer/fravaer", {"fra": "ikke-en-dato", "til": ""})
    html = resp.content.decode()
    assert resp.status_code == 200 and "Fra-datoen er ikke en gyldig dato." in html
    assert "Udfyld til-datoen." in html and not Fravaer.objects.exists()
    resp = c.post(f"{BASE}sommer/fravaer", {"fra": f"{Y}-12-01", "til": f"{Y}-12-05"})
    assert "kun registreres for sommerperioden" in resp.content.decode()


def test_add_view_overlap_message(anna: Resident, clock: Callable) -> None:
    c = _login(anna)
    c.post(f"{BASE}sommer/fravaer", {"fra": f"{Y}-07-06", "til": f"{Y}-07-12"})
    resp = c.post(f"{BASE}sommer/fravaer", {"fra": f"{Y}-07-12", "til": f"{Y}-07-14"})
    assert "Overlapper dit fravær" in resp.content.decode() and Fravaer.objects.count() == 1


def test_non_owner_delete_is_403_and_ended_has_no_button(
    anna: Resident, bo: Resident, clock: Callable
) -> None:
    ended = add_fravaer(anna, date(Y, 7, 1), date(Y, 7, 3), today=TODAY)
    live = add_fravaer(anna, date(Y, 7, 20), date(Y, 7, 25), today=TODAY)
    assert _login(bo).post(f"{BASE}sommer/fravaer/{live.pk}/slet").status_code == 403
    assert Fravaer.objects.filter(pk=live.pk).exists()
    clock(date(Y, 7, 10))
    html = _login(anna).get(f"{BASE}sommer/").content.decode()
    assert f"sommer/fravaer/{live.pk}/slet" in html
    assert f"sommer/fravaer/{ended.pk}/slet" not in html
    # a replayed POST for the ended range is refused inside the partial, nothing deleted
    resp = _login(anna).post(f"{BASE}sommer/fravaer/{ended.pk}/slet")
    assert resp.status_code == 200 and "Afsluttet fravær kan ikke slettes." in resp.content.decode()
    assert Fravaer.objects.filter(pk=ended.pk).exists()
    assert _login(anna).get(f"{BASE}sommer/fravaer/{ended.pk}/slet").status_code == 405


def test_others_visible_in_weekly_list_not_in_mine(anna: Resident, bo: Resident, clock: Callable) -> None:
    add_fravaer(bo, date(Y, 7, 6), date(Y, 7, 12), today=TODAY)
    html = _login(anna).get(f"{BASE}sommer/").content.decode()
    mine, _, weekly = html.partition("Væk denne uge")
    assert "Du har ikke registreret noget fravær i sommer." in mine and "Bo Lund" not in mine
    assert "Uge 28 (6.–12. juli): Bo Lund" in _norm(re.sub(r"<[^>]+>", "", weekly)).replace(" :", ":")


def test_full_page_and_partial_render_identical_content(
    anna: Resident, bo: Resident, clock: Callable
) -> None:
    add_fravaer(bo, date(Y, 7, 6), date(Y, 7, 12), today=TODAY)
    c = _login(anna)
    partial = c.post(f"{BASE}sommer/fravaer", {"fra": f"{Y}-07-20", "til": f"{Y}-07-25"}).content.decode()
    page = c.get(f"{BASE}sommer/").content.decode()
    assert "<html" not in partial and "<html" in page
    assert _norm(partial) in _norm(page)


def test_index_link_only_inside_window(anna: Resident, clock: Callable) -> None:
    c = _login(anna)
    for d, shown in [
        (date(Y, 4, 30), False),
        (date(Y, 5, 1), True),
        (date(Y, 8, 31), True),
        (date(Y, 9, 1), False),
    ]:
        clock(d)
        html = c.get(BASE).content.decode()
        assert (f'href="{BASE}sommer/"' in html) is shown, d
        if shown:
            assert f"Sommer {Y}" in html


# ------------------------------------------------------------------------------------- demo


def test_demo_produces_two_away_ranges(clock: Callable) -> None:
    for d in (date(Y, 4, 10), date(Y, 7, 15)):  # outside and inside SOMMER
        clock(d)
        call_command("seed_demo", "--fresh", "--force", "--residents", "12", stdout=StringIO(), verbosity=0)
        rows = list(Fravaer.objects.all())
        assert len(rows) == 2 and len({f.resident_id for f in rows}) == 2, d
        assert all(f.start_date.year == Y and f.start_date.month in (7, 8) for f in rows)


# =============================================================================================
# P3 step 3 -- claiming
# =============================================================================================

T = VagtTildeling.Status
WED_AFTEN_DAY = date(Y, 7, 1)  # Wednesday: aftenvagt with two places
SAT_DAY = date(Y, 7, 4)  # Saturday


def _stay(r: Resident, n: int, months: tuple[int, ...] = (7, 8)) -> None:
    room = Room.objects.create(legacy_index=500 + n, number=500 + n, floor="stuen", side="mod gaden")
    for month in months:
        Residency.objects.create(resident=r, room=room, year=Y, month=month)


@pytest.fixture
def cat(make_resident: Callable) -> Resident:
    return make_resident(email="cat@gahk.dk", first_name="Cat", last_name="Dam")


@pytest.fixture
def summer(anna: Resident, bo: Resident, cat: Resident, clock: Callable) -> Periode:
    """Summer 2043 generated, with anna, bo and cat living in July and August."""
    for i, r in enumerate((anna, bo, cat)):
        _stay(r, i)
    periode = resolve_periode(date(Y, 7, 1))
    generate_vagter(periode)
    return periode


def _vagt(day: date, kind: str) -> Vagt:
    return Vagt.objects.get(date=day, kind=kind)


def _refused(resident: Resident, vagt: Vagt, text: str) -> None:
    n = VagtTildeling.objects.count()
    with pytest.raises(KoekkenAllocationError) as exc:
        claim_vagt(resident, vagt)
    assert text in str(exc.value)
    assert VagtTildeling.objects.count() == n


def test_claim_succeeds_and_second_place_of_aften(summer: Periode, anna: Resident, bo: Resident) -> None:
    morgen = _vagt(WED_AFTEN_DAY, "morgen")
    row = claim_vagt(anna, morgen)
    assert (row.vagt, row.resident, row.status) == (morgen, anna, T.TILDELT)
    aften = _vagt(WED_AFTEN_DAY, "aften")
    assert aften.headcount == 2
    claim_vagt(anna, aften)
    claim_vagt(bo, aften)
    assert aften.tildelinger.count() == 2


def test_weekday_unavailable_and_away_do_not_block_claiming(summer: Periode, anna: Resident) -> None:
    from koekken.models import Praeference

    Praeference.objects.create(
        resident=anna, periode=resolve_periode(date(Y, 2, 1)), weekday_unavailable=True
    )
    add_fravaer(anna, date(Y, 7, 1), date(Y, 7, 5), today=TODAY)
    assert claim_vagt(anna, _vagt(WED_AFTEN_DAY, "frokost")).pk


def test_claim_sends_no_notification(summer: Periode, anna: Resident) -> None:
    with mock.patch("koekken.services._notify") as notify, mock.patch("core.push.send") as send:
        claim_vagt(anna, _vagt(WED_AFTEN_DAY, "morgen"))
    notify.assert_not_called()
    send.assert_not_called()


def test_claim_refuses_non_summer_shift(summer: Periode, anna: Resident) -> None:
    other = resolve_periode(date(Y, 9, 1))
    vagt = Vagt.objects.create(
        periode=other, date=date(Y, 9, 2), kind="morgen", headcount=1, duration_minutes=60
    )
    _refused(anna, vagt, "Kun sommervagter kan tages.")


def test_claim_refuses_started_shift(summer: Periode, anna: Resident) -> None:
    vagt = _vagt(WED_AFTEN_DAY, "morgen")  # 06:00
    started = timezone.make_aware(datetime(Y, 7, 1, 6, 0))
    with pytest.raises(KoekkenAllocationError):
        services._insert_tildeling_locked(
            vagt.pk,
            anna,
            check=lambda v, n, h: services._claim_refusal(v, anna, taken_count=n, held=h, at=started),
            duplicate_message="dup",
        )
    assert not VagtTildeling.objects.exists()
    assert can_claim(vagt, anna, at=started - timedelta(minutes=1))
    assert not can_claim(vagt, anna, at=started)


def test_claim_refuses_full_shift_including_udfoert_row(
    summer: Periode, anna: Resident, bo: Resident, cat: Resident
) -> None:
    vagt = _vagt(WED_AFTEN_DAY, "morgen")  # one place
    VagtTildeling.objects.create(vagt=vagt, resident=bo, status=T.UDFOERT)
    _refused(anna, vagt, "ingen ledige pladser")
    aften = _vagt(WED_AFTEN_DAY, "aften")
    claim_vagt(anna, aften)
    claim_vagt(bo, aften)
    _refused(cat, aften, "ingen ledige pladser")


def test_claim_stale_post_on_now_full_held_shift_says_already_held(
    summer: Periode, anna: Resident, bo: Resident
) -> None:
    aften = _vagt(WED_AFTEN_DAY, "aften")
    claim_vagt(anna, aften)
    VagtTildeling.objects.create(vagt=aften, resident=bo, status=T.TILDELT)  # now full, anna still holds
    _refused(anna, aften, "Du har allerede denne vagt.")


def test_claim_refuses_already_held(summer: Periode, anna: Resident) -> None:
    aften = _vagt(WED_AFTEN_DAY, "aften")
    claim_vagt(anna, aften)
    _refused(anna, aften, "Du har allerede denne vagt.")


def test_claim_refuses_outside_population_and_moved_out(
    summer: Periode, make_resident: Callable, anna: Resident
) -> None:
    outsider = make_resident(email="out@gahk.dk", first_name="Ud")
    _refused(outsider, _vagt(WED_AFTEN_DAY, "morgen"), "kan ikke tage")
    anna.move_out_date = date(Y, 7, 10)
    anna.save()
    _refused(anna, _vagt(date(Y, 7, 11), "morgen"), "kan ikke tage")
    assert claim_vagt(anna, _vagt(date(Y, 7, 10), "morgen")).pk


def test_claim_refuses_when_vagt_gone(summer: Periode, anna: Resident) -> None:
    vagt = _vagt(WED_AFTEN_DAY, "morgen")
    vagt.delete()
    with pytest.raises(KoekkenAllocationError, match="findes ikke længere"):
        claim_vagt(anna, vagt)


def test_forced_integrity_error_is_a_clean_refusal(summer: Periode, anna: Resident) -> None:
    vagt = _vagt(WED_AFTEN_DAY, "aften")
    real_create = VagtTildeling.objects.create

    def racing_create(**kw: object) -> VagtTildeling:
        real_create(**kw)  # type: ignore[arg-type]  # the "other transaction's" row lands first ...
        return real_create(**kw)  # type: ignore[arg-type]  # ... and ours violates (vagt, resident)

    with mock.patch.object(VagtTildeling.objects, "create", side_effect=racing_create):
        with pytest.raises(KoekkenAllocationError, match=r"Du har allerede denne vagt\."):
            claim_vagt(anna, vagt)
    assert VagtTildeling.objects.count() == 0  # the savepoint rolled back; no partial state


def test_claim_reuses_may_hold(summer: Periode, anna: Resident) -> None:
    vagt = _vagt(WED_AFTEN_DAY, "morgen")
    assert can_claim(vagt, anna)
    with mock.patch("koekken.services.may_hold", return_value=False) as m:
        _refused(anna, vagt, "kan ikke tage")
    assert m.called


def test_claim_code_has_no_move_out_check_of_its_own() -> None:
    for fn in (
        services._claim_refusal,
        services.can_claim,
        services._insert_tildeling_locked,
        services.claim_vagt,
        services.summer_grid,
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }
        assert "move_out_date" not in names, fn.__name__
    assert "may_hold" in inspect.getsource(services._claim_refusal)


def test_no_unclaim_url() -> None:
    urls = (Path(__file__).parent.parent / "koekken" / "urls.py").read_text()
    claim_paths = re.findall(r'path\("(sommer/vagt/[^"]*)"', urls)
    assert claim_paths == ["sommer/vagt/<int:pk>/tag"]
    for word in ("unclaim", "fortryd", "opgiv", "afmeld"):
        assert word not in urls


# ------------------------------------------------------------------------- claimed rows are normal rows


def test_claimed_row_on_tablet_marked_done_and_in_balance(summer: Periode, anna: Resident) -> None:
    vagt = _vagt(WED_AFTEN_DAY, "aften")
    before = projected_balance_for(anna)
    row = claim_vagt(anna, vagt)
    assert projected_balance_for(anna) != before
    assert row in list(todays_tildelinger(today=WED_AFTEN_DAY))
    mark_udfoert(row, at=timezone.make_aware(datetime(Y, 7, 1, 18, 0)))
    row.refresh_from_db()
    assert row.status == T.UDFOERT
    assert KoekkenPost.objects.filter(resident=anna, kind=KoekkenPost.Kind.ARBEJDE).exists()


def test_claimed_summer_row_can_be_offered_and_taken_over(
    summer: Periode, anna: Resident, bo: Resident
) -> None:
    row = claim_vagt(anna, _vagt(date(Y, 7, 2), "frokost"))
    bytte = offer_tildeling(row, anna)
    take_over(bytte, bo)
    row.refresh_from_db()
    assert row.resident == bo


# ------------------------------------------------------------------------------------------- the grid


def _grid_html(r: Resident) -> str:
    return _login(r).get(f"{BASE}sommer/").content.decode()


def test_grid_empty_before_generation(anna: Resident, clock: Callable) -> None:
    assert summer_grid(SUMMER, anna) == []
    html = _grid_html(anna)
    assert "Sommerens vagter åbner 1. maj." in html
    assert 'id="koekken-sommervagter"' in html and "Tag vagt" not in html


def test_grid_button_only_where_claimable(summer: Periode, anna: Resident, bo: Resident) -> None:
    morgen = _vagt(WED_AFTEN_DAY, "morgen")
    aften = _vagt(WED_AFTEN_DAY, "aften")
    claim_vagt(bo, morgen)  # now full
    claim_vagt(anna, aften)  # held by the viewer
    html = _grid_html(anna)
    assert f"/sommer/vagt/{morgen.pk}/tag" not in html
    assert f"/sommer/vagt/{aften.pk}/tag" not in html
    other = _vagt(WED_AFTEN_DAY, "frokost")
    assert f"/sommer/vagt/{other.pk}/tag" in html
    assert "Vagten er bindende" in html and 'hx-target="#koekken-sommervagter"' in html
    text = _norm(re.sub(r"<[^>]+>", " ", html))
    assert "Anna Hansen (din)" in text and "Bo Lund" in text
    assert "1 ledig" in text and "0 ledige" in text


def test_grid_started_shift_has_no_button(summer: Periode, anna: Resident) -> None:
    started = timezone.make_aware(datetime(Y, 7, 1, 7, 0))
    weeks = summer_grid(summer, anna, at=started)
    shifts = {(v.date, v.kind): ok for _f, _l, days in weeks for _d, ss in days for v, _n, _fr, ok in ss}
    assert shifts[(WED_AFTEN_DAY, "morgen")] is False
    assert shifts[(WED_AFTEN_DAY, "frokost")] is True


def test_grid_weeks_clipped_and_away_listed_once(summer: Periode, anna: Resident, bo: Resident) -> None:
    weeks = summer_grid(summer, anna)
    assert (weeks[0][0], weeks[0][1]) == (date(Y, 7, 1), date(Y, 7, 5))
    assert (weeks[-1][0], weeks[-1][1]) == (date(Y, 8, 31), date(Y, 8, 31))
    add_fravaer(bo, date(Y, 7, 6), date(Y, 7, 12), today=TODAY)
    html = _grid_html(anna)
    assert html.count("Bo Lund") == 1  # listed once, in the "Væk denne uge" card, not again in the grid


def test_grid_query_count_is_constant(summer: Periode, anna: Resident, bo: Resident) -> None:
    def count() -> int:
        with CaptureQueriesContext(connection) as ctx:
            summer_grid(summer, anna)
        return len(ctx)

    base = count()
    for v in list(Vagt.objects.filter(date__lte=date(Y, 7, 20))):
        claim_vagt(bo, v)
    assert count() == base
    assert base < 15


def test_get_with_generated_summer_writes_nothing(summer: Periode, anna: Resident) -> None:
    n_periode, n_rows = Periode.objects.count(), VagtTildeling.objects.count()
    _grid_html(anna)
    assert (Periode.objects.count(), VagtTildeling.objects.count()) == (n_periode, n_rows)


def test_tag_view_claims_and_partial_matches_page(summer: Periode, anna: Resident, bo: Resident) -> None:
    c = _login(anna)
    vagt = _vagt(WED_AFTEN_DAY, "frokost")
    resp = c.post(f"{BASE}sommer/vagt/{vagt.pk}/tag")
    partial = resp.content.decode()
    assert resp.status_code == 200 and "<html" not in partial
    assert VagtTildeling.objects.filter(vagt=vagt, resident=anna).exists()
    page = c.get(f"{BASE}sommer/").content.decode()
    assert _norm(partial) in _norm(page)


def test_stale_claim_on_full_shift_returns_200_with_error(
    summer: Periode, anna: Resident, bo: Resident
) -> None:
    vagt = _vagt(WED_AFTEN_DAY, "morgen")
    claim_vagt(bo, vagt)
    resp = _login(anna).post(f"{BASE}sommer/vagt/{vagt.pk}/tag")
    assert resp.status_code == 200
    assert "ingen ledige pladser" in resp.content.decode()
    assert not VagtTildeling.objects.filter(vagt=vagt, resident=anna).exists()


def test_tag_view_requires_post_and_login(summer: Periode, anna: Resident) -> None:
    vagt = _vagt(WED_AFTEN_DAY, "morgen")
    assert _login(anna).get(f"{BASE}sommer/vagt/{vagt.pk}/tag").status_code == 405
    assert Client().post(f"{BASE}sommer/vagt/{vagt.pk}/tag").status_code in (301, 302, 403)


# ----------------------------------------------------------------------- Køkkengruppen's list


def test_unclaimed_list(summer: Periode, anna: Resident, bo: Resident, clock: Callable) -> None:
    clock(date(Y, 7, 1))
    claim_vagt(anna, _vagt(WED_AFTEN_DAY, "aften"))  # one of two places left
    claim_vagt(bo, _vagt(WED_AFTEN_DAY, "frokost"))  # full -> not listed
    VagtTildeling.objects.create(
        vagt=_vagt(WED_AFTEN_DAY, "morgen"), resident=bo, status=T.UDFOERT
    )  # full too
    at = timezone.make_aware(datetime(Y, 7, 1, 5, 0))  # nothing has started yet today
    keys = {(v.date, v.kind): free for v, free in unclaimed_summer_vagter(at=at)}
    assert (WED_AFTEN_DAY, "morgen") not in keys and (WED_AFTEN_DAY, "frokost") not in keys
    assert keys[(WED_AFTEN_DAY, "aften")] == 1
    assert keys[(date(Y, 7, 2), "aften")] == 2
    assert max(d for d, _k in keys) == date(Y, 7, 15) and date(Y, 7, 16) not in {d for d, _k in keys}
    order = ["morgen", "frokost", "aften"]
    ordered = [(v.date, order.index(v.kind)) for v, _f in unclaimed_summer_vagter(at=at)]
    assert ordered == sorted(ordered)
    # a started shift drops out
    later = timezone.make_aware(datetime(Y, 7, 1, 13, 0))
    assert (WED_AFTEN_DAY, "morgen") not in {(v.date, v.kind) for v, _f in unclaimed_summer_vagter(at=later)}


def test_unclaimed_list_ignores_non_summer(summer: Periode, clock: Callable) -> None:
    clock(date(Y, 9, 1))
    Vagt.objects.create(
        periode=resolve_periode(date(Y, 9, 1)),
        date=date(Y, 9, 3),
        kind="morgen",
        headcount=1,
        duration_minutes=60,
    )
    assert unclaimed_summer_vagter() == []


def test_gruppe_page_lists_unclaimed_and_empty_state(
    summer: Periode, make_resident: Callable, clock: Callable
) -> None:
    clock(date(Y, 7, 1))  # role validity is dated: create each manager after setting the clock
    manager = make_resident(email="mgr@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    html = _login(manager).get(f"{BASE}gruppe/").content.decode()
    assert "Ledige sommervagter" in html and "Ingen ledige sommervagter." not in html
    clock(date(Y, 9, 20))
    manager = make_resident(email="mgr3@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    html = _login(manager).get(f"{BASE}gruppe/").content.decode()
    assert "Ingen ledige sommervagter." in html


# ------------------------------------------------------------------------------- override_assign


def test_override_assign_messages_unchanged_and_uses_locked_insert(
    summer: Periode, make_resident: Callable, anna: Resident, bo: Resident, cat: Resident
) -> None:
    manager = make_resident(email="mgr2@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    c = _login(manager)
    vagt = _vagt(WED_AFTEN_DAY, "morgen")

    def post(r: Resident) -> str:
        resp = c.post(f"{BASE}gruppe/override", {"vagt": vagt.pk, "resident": r.pk}, follow=True)
        return " | ".join(str(m) for m in resp.context["messages"])

    assert post(anna) == f"{anna.full_name} tildelt {vagt}."
    assert post(bo) == f"{vagt} har allerede fuld besætning."
    aften = _vagt(WED_AFTEN_DAY, "aften")
    vagt = aften
    assert post(anna).endswith("tildelt " + str(aften) + ".")
    assert post(anna) == f"{anna.full_name} er allerede tildelt {aften}."


def test_override_assign_duplicate_race_uses_manager_wording(
    summer: Periode, make_resident: Callable, anna: Resident
) -> None:
    """The (vagt, resident) unique-constraint race (a concurrent take_over landing the same resident) is
    forced by making the insert itself fail; the manager sees the override wording, not the resident's."""
    manager = make_resident(email="mgr3@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    c = _login(manager)
    aften = _vagt(WED_AFTEN_DAY, "aften")
    with mock.patch.object(VagtTildeling.objects, "create", side_effect=IntegrityError("dup")):
        resp = c.post(f"{BASE}gruppe/override", {"vagt": aften.pk, "resident": anna.pk}, follow=True)
    assert [str(m) for m in resp.context["messages"]] == [f"{anna.full_name} er allerede tildelt {aften}."]


# ---------------------------------------------------------------------------------------- generation


def _kinds(ps: list[Periode]) -> list[tuple[str, int]]:
    return [(str(p.kind), p.year) for p in ps]


@pytest.mark.parametrize(
    "today,expected",
    [
        (date(Y, 4, 30), [("foraar", Y)]),
        (date(Y, 5, 1), [("foraar", Y), ("sommer", Y)]),
        (date(Y, 6, 15), [("foraar", Y), ("sommer", Y)]),
        (date(Y, 7, 1), [("sommer", Y)]),
        (date(Y, 9, 1), [("efteraar", Y)]),
        (date(Y + 1, 1, 15), [("efteraar", Y)]),
    ],
)
def test_periodes_to_generate(today: date, expected: list[tuple[str, int]], db: None) -> None:
    assert _kinds(periodes_to_generate(today)) == expected


def test_periodes_to_generate_writes_no_stray_summer_before_deadline(db: None) -> None:
    periodes_to_generate(date(Y, 4, 30))
    assert not Periode.objects.filter(kind=Periode.Kind.SOMMER).exists()


def test_generate_command_opens_summer_on_1_may_and_is_idempotent(clock: Callable) -> None:
    clock(date(Y, 4, 30))
    call_command("generate_koekkenvagter", stdout=StringIO())
    assert not Vagt.objects.filter(periode__kind=Periode.Kind.SOMMER).exists()
    clock(date(Y, 5, 1))
    out = StringIO()
    call_command("generate_koekkenvagter", stdout=out)
    assert Vagt.objects.filter(periode__kind=Periode.Kind.SOMMER).count() > 100
    assert "sommer" in out.getvalue().lower()
    n = Vagt.objects.count()
    clock(date(Y, 6, 10))
    call_command("generate_koekkenvagter", stdout=StringIO())
    assert Vagt.objects.count() == n


def test_generate_command_with_date_is_unchanged(clock: Callable) -> None:
    clock(date(Y, 5, 1))
    call_command("generate_koekkenvagter", "--date", f"{Y}-03-10", stdout=StringIO())
    assert not Vagt.objects.filter(periode__kind=Periode.Kind.SOMMER).exists()
    assert Vagt.objects.filter(date=date(Y, 3, 10)).exists()


# ------------------------------------------------------------------------------------------ fridag


def test_fridag_command_turns_commit_integrity_error_into_rerun_message(
    summer: Periode, anna: Resident, clock: Callable
) -> None:
    claim_vagt(anna, _vagt(SAT_DAY, "morgen"))
    real = services.declare_fridag

    def declare_then_fail(*a: object, **kw: object) -> None:
        real(*a, **kw)  # type: ignore[arg-type]  # does its writes, then the "commit" is rejected by the deferred FK
        raise IntegrityError("deferred FK")

    with (
        mock.patch("koekken.management.commands.declare_koekken_fridag.declare_fridag", declare_then_fail),
        mock.patch("koekken.management.commands.declare_koekken_fridag.send") as send,
        pytest.raises(CommandError, match="Kør kommandoen igen"),
    ):
        call_command("declare_koekken_fridag", SAT_DAY.isoformat(), stdout=StringIO())
    send.assert_not_called()
    assert not Fridag.objects.exists()
    assert Vagt.objects.filter(date=SAT_DAY).exists()
    assert VagtTildeling.objects.filter(resident=anna).count() == 1


# ----------------------------------------------------------------------------------------- demo


def test_demo_produces_summer_claims_and_an_unclaimed_shift(clock: Callable) -> None:
    for d in (date(Y, 4, 10), date(Y, 7, 15)):
        clock(d)
        call_command("seed_demo", "--fresh", "--force", "--residents", "12", stdout=StringIO(), verbosity=0)
        mine = VagtTildeling.objects.filter(vagt__periode__kind=Periode.Kind.SOMMER)
        assert mine.count() >= 3, d
        assert Vagt.objects.filter(periode__kind=Periode.Kind.SOMMER).count() > mine.count(), d
        full_aften = mine.filter(vagt__kind="aften").values("vagt").annotate(n=Count("id")).filter(n=2)
        assert full_aften.exists(), d
        taken = {t.vagt_id for t in mine}
        assert Vagt.objects.filter(periode__kind=Periode.Kind.SOMMER).exclude(pk__in=taken).exists(), d
