"""Køkkenvagter P3 step 2 -- away ranges (`Fravaer`) and the summer page's "Mit fravær"/"Væk denne uge"
sections. Design: `docs/plans/2026-10-04-koekkenvagter-p3-design.md` §2/§3/§7. Step 3 (the shift grid)
extends this file.

Summer 2043: 1 Jul is a Wednesday (ISO week 27), 31 Jul a Friday, 1 Aug a Saturday (same week 31 as
31 Jul), and 31 Aug a Monday -- so the first and last weeks are both partial.
"""

import re
from collections.abc import Callable, Iterator
from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.clock import clear_cache
from core.models import DevClock
from koekken.models import Fravaer, Periode
from koekken.services import (
    KoekkenAllocationError,
    add_fravaer,
    away_by_week,
    delete_fravaer,
    resident_fravaer,
    summer_bounds,
    summer_link_visible,
    target_summer,
)
from residents.models import Resident

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
