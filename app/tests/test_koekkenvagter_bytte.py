"""Køkkenvagter Amendment 4, step 1 -- hand-off of vagter (offer / withdraw / take-over / whole-shift
take-over). Design: `docs/plans/2026-10-04-koekkenvagter-a4-design.md`. Builders are local on purpose.

Dates sit in Forår 2042 (March), far past the real clock. `clock(d)` sets DevClock (whose "now" is
midnight of that date), so a shift is "started" once the clock is on a later date.
"""

import re
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core import push
from core.clock import clear_cache
from core.models import DevClock, PushSubscription, Room
from koekken import access as koekken_access
from koekken import services
from koekken.models import (
    KoekkenPost,
    Periode,
    Praeference,
    Vagt,
    VagtBytte,
    VagtBytteForslag,
    VagtRegel,
    VagtTildeling,
)
from koekken.services import (
    KoekkenAllocationError,
    accept_trade,
    allocate_month,
    allocate_tier_a,
    allocate_tier_b,
    can_offer,
    can_take,
    can_take_whole,
    declare_fridag,
    decline_proposal,
    flag_tildeling,
    force_rerun_impact,
    generate_vagter,
    has_started,
    mark_udfoert,
    offer_tildeling,
    open_offers,
    post_obligation,
    projected_balance_for,
    propose_trade,
    reconcile_month,
    resolve_anmeldelse,
    resolve_periode,
    take_over,
    take_over_whole,
    withdraw_offer,
    withdraw_proposal,
)
from residents.models import Residency, Resident, Role

pytestmark = pytest.mark.django_db

T = VagtTildeling.Status
B = VagtBytte.Status
F = VagtBytteForslag.Status
_seq = iter(range(1, 100_000))

M1 = date(2042, 3, 12)  # Wednesday, morgen
M2 = date(2042, 3, 13)  # Thursday, morgen
AV = date(2042, 3, 11)  # Tuesday, aften (2 x 180)
TODAY = date(2042, 3, 1)


@pytest.fixture
def clock(settings: object) -> Iterator[Callable[[date], None]]:
    settings.DEBUG = True  # type: ignore[attr-defined]

    def _set(d: date) -> None:
        DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": d})

    _set(TODAY)
    yield _set
    clear_cache()


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch, settings: object) -> list:
    settings.VAPID_PUBLIC_KEY = "k"  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = "k"  # type: ignore[attr-defined]
    settings.VAPID_ADMIN_EMAIL = "drift@gahk.dk"  # type: ignore[attr-defined]
    sent: list = []

    def record(subscriptions: object, payload: dict) -> int:
        sent.append((sorted(s.user_id for s in subscriptions), payload))  # type: ignore[union-attr]
        return len(sent)

    monkeypatch.setattr(push, "_dispatch", record)
    monkeypatch.setattr(push, "_run_in_background", lambda fn: fn())
    return sent


def _room() -> Room:
    n = next(_seq)
    return Room.objects.create(legacy_index=n, number=n, floor="stuen", side="mod gaden")


def _place(resident: Resident, year: int = 2042, month: int = 3) -> None:
    Residency.objects.create(resident=resident, room=_room(), year=year, month=month)


def _vagt(d: date, kind: str, headcount: int, minutes: int) -> Vagt:
    return Vagt.objects.create(
        periode=resolve_periode(d), date=d, kind=kind, headcount=headcount, duration_minutes=minutes
    )


def _hold(vagt: Vagt, resident: Resident, status: str = T.TILDELT) -> VagtTildeling:
    return VagtTildeling.objects.create(vagt=vagt, resident=resident, status=status)


def _sub(resident: Resident) -> None:
    PushSubscription.objects.create(
        user=resident,
        endpoint=f"https://example.test/{resident.pk}",
        auth="a",
        p256dh="p",
        wants_koekken=True,
    )


class World:
    pass


@pytest.fixture
def w(make_resident: Callable, clock: Callable) -> World:
    w = World()
    names = ["a", "b", "c", "d", "e"]
    for n in names:
        r = make_resident(email=f"{n}@gahk.dk", first_name=f"Navn{n.upper()}", last_name="Test")
        _place(r)
        setattr(w, n, r)
    w.outsider = make_resident(email="x@gahk.dk", first_name="Udenfor")  # not on March's list
    w.m1 = _vagt(M1, VagtRegel.Kind.MORGEN, 1, 60)
    w.m2 = _vagt(M2, VagtRegel.Kind.MORGEN, 1, 60)
    w.av = _vagt(AV, VagtRegel.Kind.AFTEN, 2, 180)
    w.ra = _hold(w.m1, w.a)
    w.rb = _hold(w.m2, w.b)
    w.rc = _hold(w.av, w.c)
    w.rd = _hold(w.av, w.d)
    w.periode = w.m1.periode
    return w


def _refuses(fn: Callable, *args: object) -> str:
    with pytest.raises(KoekkenAllocationError) as exc:
        fn(*args)
    return str(exc.value)


# ----------------------------------------------------------------------------------------- offer


def test_offer_creates_open_offer_and_moves_nothing(w: World) -> None:
    bytte = offer_tildeling(w.ra, w.a)
    assert bytte.status == B.AABEN and bytte.tilbudt_af == w.a and bytte.tildeling_id == w.ra.pk
    w.ra.refresh_from_db()
    assert w.ra.resident == w.a and w.ra.status == T.TILDELT


def test_offer_refusals(w: World, clock: Callable, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "egne" in _refuses(offer_tildeling, w.ra, w.b)  # not the holder

    done = _hold(_vagt(date(2042, 3, 20), VagtRegel.Kind.MORGEN, 1, 60), w.a, T.UDFOERT)
    assert _refuses(offer_tildeling, done, w.a)  # non-TILDELT

    # Flag history in ANY status, including dismissed back to TILDELT.
    flag_tildeling(w.rb, w.a, "x")
    resolve_anmeldelse(w.rb.anmeldelser.get(), upheld=False, resolved_by=w.c)
    w.rb.refresh_from_db()
    assert w.rb.status == T.TILDELT
    assert "anmeldelse" in _refuses(offer_tildeling, w.rb, w.b)
    assert not can_offer(w.rb, w.b)

    offer_tildeling(w.ra, w.a)
    assert "allerede tilbudt" in _refuses(offer_tildeling, w.ra, w.a)  # second open offer
    with pytest.raises(IntegrityError), transaction.atomic():
        VagtBytte.objects.create(tildeling=w.ra, tilbudt_af=w.a)  # the DB constraint itself

    # Started shift: boundary is the VagtRegel start time (morgen 06:00).
    tz_now = lambda h: timezone.make_aware(datetime(2042, 3, 13, h, 0))  # noqa: E731
    monkeypatch.setattr(services, "current_datetime", lambda: tz_now(5))
    assert not has_started(w.m2)
    monkeypatch.setattr(services, "current_datetime", lambda: tz_now(6))
    assert has_started(w.m2)
    other = _hold(_vagt(date(2042, 3, 13), VagtRegel.Kind.FROKOST, 1, 60), w.e)
    monkeypatch.setattr(services, "current_datetime", lambda: tz_now(13))
    assert "startet" in _refuses(offer_tildeling, other, w.e)


# ------------------------------------------------------------------------------------ take-over


def test_take_over_mechanics(w: World) -> None:
    bytte = offer_tildeling(w.ra, w.a)
    before_a, before_e = projected_balance_for(w.a), projected_balance_for(w.e)
    out = take_over(bytte, w.e)
    w.ra.refresh_from_db()
    bytte.refresh_from_db()
    assert out.pk == bytte.pk
    assert w.ra.resident == w.e and w.ra.status == T.TILDELT
    assert bytte.status == B.OVERTAGET and bytte.overtaget_af == w.e and bytte.closed_at is not None
    assert projected_balance_for(w.a) == before_a - 60
    assert projected_balance_for(w.e) == before_e + 60
    mark_udfoert(w.ra, at=timezone.make_aware(datetime(2042, 3, 12, 12, 0)))
    post = KoekkenPost.objects.get(kind=KoekkenPost.Kind.ARBEJDE)
    assert post.resident == w.e and post.delta_minutes == 60


def test_take_over_refusals(w: World, monkeypatch: pytest.MonkeyPatch, clock: Callable) -> None:
    bytte = offer_tildeling(w.ra, w.a)
    assert "egen" in _refuses(take_over, bytte, w.a)
    assert _refuses(take_over, bytte, w.outsider)  # outside the month's population
    moved = w.e
    Resident.objects.filter(pk=moved.pk).update(move_out_date=date(2042, 3, 5))
    moved.refresh_from_db()
    assert _refuses(take_over, bytte, moved)  # moved out before the shift date
    assert not may_hold_check(moved, w.m1)
    # Weekday-unavailable is deliberately NOT checked.
    Resident.objects.filter(pk=moved.pk).update(move_out_date=None)
    Praeference.objects.create(resident=w.e, periode=w.periode, weekday_unavailable=True)
    assert w.e.pk and can_take(bytte, Resident.objects.get(pk=w.e.pk))
    # Taker already on the vagt (the partner on a two-person shift): points at the whole-shift action.
    cb = offer_tildeling(w.rc, w.c)
    assert "Tag hele vagten" in _refuses(take_over, cb, w.d)
    assert not can_take(cb, w.d) and can_take_whole(cb, w.d)
    # Expired: cannot be taken, offerer stays the holder.
    clock(date(2042, 3, 13))
    assert "startet" in _refuses(take_over, bytte, w.b)
    w.ra.refresh_from_db()
    assert w.ra.resident == w.a


def may_hold_check(r: Resident, v: Vagt) -> bool:
    return services.may_hold(r, v)


def test_non_tildelt_or_stale_holder_lapses_offer(w: World) -> None:
    bytte = offer_tildeling(w.ra, w.a)
    VagtTildeling.objects.filter(pk=w.ra.pk).update(resident=w.b)  # offerer is no longer the holder
    assert "bortfaldet" in _refuses(take_over, bytte, w.e)
    bytte.refresh_from_db()
    assert bytte.status == B.BORTFALDET  # persisted despite the refusal
    assert not open_offers()


def test_stale_second_taker_gets_clean_refusal(w: World) -> None:
    first = offer_tildeling(w.ra, w.a)
    stale = VagtBytte.objects.get(pk=first.pk)
    take_over(first, w.e)
    assert _refuses(take_over, stale, w.b)
    w.ra.refresh_from_db()
    assert w.ra.resident == w.e and VagtBytte.objects.get(pk=first.pk).overtaget_af == w.e


def test_close_invalidated_offers_helper(w: World) -> None:
    keep = offer_tildeling(w.ra, w.a)
    other = VagtBytte.objects.create(tildeling=w.ra, tilbudt_af=w.a, status=B.TRUKKET)
    services._close_invalidated_offers([w.ra], keep=keep)
    other.refresh_from_db()
    assert other.status == B.TRUKKET  # only AABEN ones lapse
    keep.refresh_from_db()
    assert keep.status == B.AABEN
    services._close_invalidated_offers([w.ra])
    keep.refresh_from_db()
    assert keep.status == B.BORTFALDET


# ---------------------------------------------------------------------------------- whole shift


def test_whole_shift_mechanics(w: World) -> None:
    supply = lambda: sum(  # noqa: E731
        v.headcount * v.duration_minutes for v in Vagt.objects.filter(periode=w.periode)
    )
    before = supply()
    post_obligation(w.periode, 3)
    obl = lambda: sorted(  # noqa: E731
        KoekkenPost.objects.filter(kind=KoekkenPost.Kind.FORPLIGTELSE).values_list("delta_minutes", flat=True)
    )
    obl_before = obl()
    bytte = offer_tildeling(w.rc, w.c)
    take_over_whole(bytte, w.d)
    w.av.refresh_from_db()
    assert (w.av.headcount, w.av.duration_minutes) == (1, 360)
    assert not VagtTildeling.objects.filter(pk=w.rc.pk).exists()  # vacated row gone
    bytte = VagtBytte.objects.get(pk=bytte.pk)  # cascade trap: the offer survived
    assert bytte.status == B.OVERTAGET_HEL and bytte.tildeling_id == w.rd.pk and bytte.overtaget_af == w.d
    assert supply() == before
    post_obligation(w.periode, 3)
    assert obl() == obl_before
    # Credit 360, and an upheld flag reverses 360.
    when = timezone.make_aware(datetime(2042, 3, 11, 18, 0))
    mark_udfoert(w.rd, at=when)
    assert KoekkenPost.objects.get(kind=KoekkenPost.Kind.ARBEJDE).delta_minutes == 360
    flag_tildeling(w.rd, w.a)
    resolve_anmeldelse(w.rd.anmeldelser.get(), upheld=True, resolved_by=w.b)
    assert KoekkenPost.objects.get(kind=KoekkenPost.Kind.TILBAGEFOERSEL).delta_minutes == -360
    # generate_vagter does not reset it.
    generate_vagter(w.periode)
    w.av.refresh_from_db()
    assert (w.av.headcount, w.av.duration_minutes) == (1, 360)
    assert Vagt.objects.filter(date=AV, kind=VagtRegel.Kind.AFTEN).count() == 1


def test_whole_shift_refusals(w: World) -> None:
    # headcount 1 (hand-built overfull) and 3
    one = _vagt(date(2042, 3, 18), VagtRegel.Kind.MORGEN, 1, 60)
    _hold(one, w.a)
    _hold(one, w.b)
    assert "to pladser" in _refuses(
        take_over_whole, offer_tildeling(VagtTildeling.objects.get(vagt=one, resident=w.a), w.a), w.b
    )
    three = _vagt(date(2042, 3, 19), VagtRegel.Kind.AFTEN, 3, 120)
    rows = [_hold(three, r) for r in (w.a, w.b, w.e)]
    assert "to pladser" in _refuses(take_over_whole, offer_tildeling(rows[0], w.a), w.b)
    # taker holds no row on the vagt
    b1 = offer_tildeling(w.rc, w.c)
    assert "ikke en plads" in _refuses(take_over_whole, b1, w.e)
    # partner row not TILDELT
    VagtTildeling.objects.filter(pk=w.rd.pk).update(status=T.UDFOERT)
    assert "meldt udført" in _refuses(take_over_whole, b1, w.d)
    VagtTildeling.objects.filter(pk=w.rd.pk).update(status=T.TILDELT)
    # partner has own open offer
    offer_tildeling(w.rd, w.d)
    assert "tilbudt din plads" in _refuses(take_over_whole, b1, w.d)
    assert not can_take_whole(b1, w.d)
    w.av.refresh_from_db()
    assert (w.av.headcount, w.av.duration_minutes) == (2, 180)
    assert VagtTildeling.objects.filter(vagt=w.av).count() == 2


def test_collapsed_shift_counts_as_full(w: World, make_resident: Callable) -> None:
    take_over_whole(offer_tildeling(w.rc, w.c), w.d)
    w.av.refresh_from_db()
    assert w.av.tildelinger.count() >= w.av.headcount  # override_assign's own "fuld besætning" test
    manager = make_resident(email="mgr@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    c = Client()
    c.force_login(manager)
    resp = c.post("/intern/koekken/gruppe/override", {"vagt": w.av.pk, "resident": w.e.pk}, follow=True)
    assert "fuld besætning" in resp.content.decode()
    assert VagtTildeling.objects.filter(vagt=w.av).count() == 1


# ------------------------------------------------------------------------------ expiry, withdraw


def test_expired_offer_never_listed_or_takeable(w: World, clock: Callable) -> None:
    offer_tildeling(w.ra, w.a)
    assert len(open_offers()) == 1
    clock(date(2042, 3, 13))
    assert open_offers() == []
    assert VagtBytte.objects.get().status == B.AABEN  # stays AABEN in the database
    assert [] == _board(w.b)


def _board(resident: Resident) -> list:
    from django.test import RequestFactory

    from koekken.views import _bytte_context

    req = RequestFactory().get("/")
    req.user = resident
    req.session = {}
    import residents.permissions as perms

    orig = perms.current_resident
    try:
        import koekken.views as kv

        kv.current_resident = lambda request: resident  # type: ignore[assignment]
        return list(_bytte_context(req)["board"])  # type: ignore[call-overload]
    finally:
        kv.current_resident = orig  # type: ignore[assignment]


def test_withdraw(w: World) -> None:
    bytte = offer_tildeling(w.ra, w.a)
    assert _refuses(withdraw_offer, bytte, w.b)
    out = withdraw_offer(bytte, w.a)
    assert out.status == B.TRUKKET and out.closed_at
    again = offer_tildeling(w.ra, w.a)  # can be re-offered
    assert again.pk != bytte.pk and again.status == B.AABEN
    msg = _refuses(take_over, bytte, w.e)  # the withdrawn one cannot be taken
    assert "trukket tilbage" in msg and "først" not in msg  # not "someone else got there first"


# ------------------------------------------------------- flag history re-checked at take time


def _flagged_then_dismissed_offer(w: World, row: VagtTildeling, offerer: Resident) -> VagtBytte:
    """Offer `row`, flag it (same day, allowed), dismiss the flag: back to TILDELT. Flagging now lapses
    the offer, so it is put back to AABEN by hand: this simulates an offer left open by data from before
    that fix, which the take-time re-check must still refuse."""
    bytte = offer_tildeling(row, offerer)
    flag_tildeling(row, w.e, "x")
    resolve_anmeldelse(row.anmeldelser.get(), upheld=False, resolved_by=w.b)
    row.refresh_from_db()
    assert row.status == T.TILDELT
    VagtBytte.objects.filter(pk=bytte.pk).update(status=B.AABEN, closed_at=None)
    assert VagtBytte.objects.get(pk=bytte.pk).status == B.AABEN
    return bytte


def test_flagging_lapses_an_open_offer(w: World, login: Callable) -> None:
    bytte = offer_tildeling(w.ra, w.a)
    assert open_offers() == [bytte]
    flag_tildeling(w.ra, w.e, "x")
    bytte.refresh_from_db()
    assert bytte.status == B.BORTFALDET and bytte.closed_at is not None
    assert not open_offers()
    resolve_anmeldelse(w.ra.anmeldelser.get(), upheld=False, resolved_by=w.b)  # dismissal: still lapsed
    assert VagtBytte.objects.get(pk=bytte.pk).status == B.BORTFALDET and not open_offers()
    for resident in (w.a, w.b):
        html = _html(login(resident))
        assert f"bytte/{bytte.pk}/" not in html  # not on the board, nor on the offerer's own list


def test_flagged_then_dismissed_offer_is_not_takeable(w: World) -> None:
    bytte = _flagged_then_dismissed_offer(w, w.ra, w.a)
    assert not can_take(bytte, w.d)
    assert not open_offers()
    assert "anmeldelse" in _refuses(take_over, bytte, w.d)
    w.ra.refresh_from_db()
    assert w.ra.resident == w.a and w.ra.anmeldelser.count() == 1  # nothing moved, history intact
    assert VagtBytte.objects.get(pk=bytte.pk).status == B.BORTFALDET  # the stale offer is cleaned up


def test_flagged_then_dismissed_offer_is_not_takeable_whole(w: World) -> None:
    bytte = _flagged_then_dismissed_offer(w, w.rc, w.c)
    assert not can_take_whole(bytte, w.d)
    assert "anmeldelse" in _refuses(take_over_whole, bytte, w.d)
    w.av.refresh_from_db()
    assert (w.av.headcount, w.av.duration_minutes) == (2, 180)
    assert VagtTildeling.objects.filter(pk=w.rc.pk).exists() and w.rc.anmeldelser.count() == 1


def test_take_over_integrity_race_gives_clean_refusal(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The (vagt, resident) unique constraint is the backstop for a race the locks cannot see (a row
    inserted by someone else after the in-lock check): a clean refusal, and nothing partially written."""
    bytte = offer_tildeling(w.ra, w.a)
    real = services.may_hold

    def conflicting(resident: Resident, vagt: Vagt, **kw: object) -> bool:
        if resident.pk == w.e.pk:
            _hold(vagt, w.e)  # the concurrent insert, landing after the existence check
        return real(resident, vagt, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(services, "may_hold", conflicting)
    assert "allerede en plads" in _refuses(take_over, bytte, w.e)
    w.ra.refresh_from_db()
    assert w.ra.resident == w.a and w.ra.status == T.TILDELT
    assert VagtBytte.objects.get(pk=bytte.pk).status == B.AABEN
    assert not VagtBytte.objects.filter(status=B.OVERTAGET).exists()
    assert VagtTildeling.objects.filter(vagt=w.m1).count() == 1  # the whole attempt rolled back


# ------------------------------------------------------------------------------- notifications


def test_notifications(w: World, pushes: list, monkeypatch: pytest.MonkeyPatch) -> None:
    for r in (w.a, w.b, w.c, w.d):
        _sub(r)
    bytte = offer_tildeling(w.ra, w.a)
    withdraw_offer(bytte, w.a)
    assert pushes == []  # nothing on offer/withdraw
    take_over(offer_tildeling(w.ra, w.a), w.b)
    assert len(pushes) == 1 and pushes[0][0] == [w.a.pk]  # offerer only, never the actor
    assert f"{w.b.full_name} har overtaget din" in pushes[0][1]["body"]
    pushes.clear()
    take_over_whole(offer_tildeling(w.rc, w.c), w.d)
    assert pushes[0][0] == [w.c.pk]
    assert f"{w.d.full_name} har overtaget hele" in pushes[0][1]["body"]
    # Narrowed through allowed_subscribers: an offerer without access is not notified.
    pushes.clear()
    monkeypatch.setattr(koekken_access, "ACCESS_ROLES", (Role.KOKKENGRUPPE,))
    take_over(offer_tildeling(w.rb, w.b), w.e)
    assert all(recipients == [] for recipients, _payload in pushes)


def test_no_push_on_expiry(w: World, pushes: list, clock: Callable) -> None:
    _sub(w.a)
    offer_tildeling(w.ra, w.a)
    clock(date(2042, 3, 14))
    assert open_offers() == [] and pushes == []


# ---------------------------------------------------------------------------- Q2c force survival


def _allocated(make_resident: Callable, clock: Callable, n: int = 8) -> tuple[Periode, list[Resident]]:
    periode = resolve_periode(date(2042, 3, 15))
    generate_vagter(periode)
    people = []
    for i in range(n):
        r = make_resident(email=f"al{i}@gahk.dk", first_name=f"Al{i}", last_name="Test")
        _place(r)
        people.append(r)
    clock(TODAY)
    allocate_month(2042, 3)
    return periode, people


def _tier_a_row() -> VagtTildeling:
    return (
        VagtTildeling.objects.filter(vagt__kind=VagtRegel.Kind.MORGEN, vagt__date__gt=TODAY, status=T.TILDELT)
        .order_by("vagt__date", "pk")
        .first()
    )


def _aften_pair() -> Vagt:
    for v in Vagt.objects.filter(
        kind=VagtRegel.Kind.AFTEN, headcount=2, date__gt=TODAY, date__month=3
    ).order_by("date"):
        if v.date.weekday() < 5 and v.tildelinger.count() == 2:
            return v
    raise AssertionError("no staffed weekday aften")


def _taker(people: list[Resident], vagt: Vagt, not_these: tuple[int, ...] = ()) -> Resident:
    held = set(VagtTildeling.objects.filter(vagt=vagt).values_list("resident_id", flat=True))
    return next(p for p in people if p.pk not in held and p.pk not in not_these)


def _handoff(kind: str, people: list[Resident]) -> tuple[int, int, int]:
    if kind == "tier_a_take":
        row = _tier_a_row()
    elif kind == "aften_take":
        row = _aften_pair().tildelinger.order_by("pk").first()
    else:
        vagt = _aften_pair()
        r1, r2 = vagt.tildelinger.order_by("pk")
        bytte = offer_tildeling(r1, r1.resident)
        take_over_whole(bytte, r2.resident)
        return r2.pk, r2.resident_id, vagt.pk
    taker = _taker(people, row.vagt)
    take_over(offer_tildeling(row, row.resident), taker)
    return row.pk, taker.pk, row.vagt_id


CASES = [
    ("month", "tier_a_take"),
    ("month", "aften_take"),
    ("month", "whole"),
    ("tier_a", "tier_a_take"),
    ("tier_b", "aften_take"),
    ("tier_b", "whole"),
]


def _force(entry: str) -> None:
    if entry == "month":
        allocate_month(2042, 3, force=True)
    elif entry == "tier_a":
        allocate_tier_a(2042, 3, force=True)
    else:
        allocate_tier_b(2042, 3, force=True)


@pytest.mark.parametrize(("entry", "kind"), CASES)
def test_handoff_survives_force_rerun(
    entry: str, kind: str, make_resident: Callable, clock: Callable
) -> None:
    _periode, people = _allocated(make_resident, clock)
    pk, resident_id, vagt_id = _handoff(kind, people)
    _force(entry)
    row = VagtTildeling.objects.get(pk=pk)
    assert row.resident_id == resident_id and row.vagt_id == vagt_id and row.status == T.TILDELT
    assert VagtBytte.objects.get(tildeling=row, status__in=[B.OVERTAGET, B.OVERTAGET_HEL])
    for v in Vagt.objects.filter(date__month=3):
        assert v.tildelinger.count() <= v.headcount
    if kind == "whole":
        v = Vagt.objects.get(pk=vagt_id)
        assert (v.headcount, v.duration_minutes) == (1, 360) and v.tildelinger.count() == 1


def test_force_rerun_is_idempotent_with_handoff(make_resident: Callable, clock: Callable) -> None:
    _periode, people = _allocated(make_resident, clock)
    _handoff("tier_a_take", people)
    _handoff("whole", people)
    snap = lambda: set(  # noqa: E731
        VagtTildeling.objects.filter(vagt__date__month=3).values_list("vagt_id", "resident_id")
    )
    allocate_month(2042, 3, force=True)
    first = snap()
    allocate_month(2042, 3, force=True)
    assert snap() == first


def test_open_offer_on_plain_row_lapses_and_impact_counts(make_resident: Callable, clock: Callable) -> None:
    _periode, people = _allocated(make_resident, clock)
    _handoff("tier_a_take", people)
    plain = (
        VagtTildeling.objects.filter(
            vagt__kind=VagtRegel.Kind.FROKOST, vagt__date__gt=TODAY, status=T.TILDELT
        )
        .order_by("vagt__date")
        .first()
    )
    offer_tildeling(plain, plain.resident)
    assert force_rerun_impact(2042, 3) == (1, 1)
    allocate_month(2042, 3, force=True)
    assert not VagtBytte.objects.filter(status=B.AABEN).exists()
    assert force_rerun_impact(2042, 3) == (1, 0)


def test_small_population_cycling_with_handoff(make_resident: Callable, clock: Callable) -> None:
    _periode, people = _allocated(make_resident, clock, n=3)
    _handoff("whole", people)
    allocate_month(2042, 3, force=True)  # must not raise a unique-constraint error
    allocate_tier_b(2042, 3, force=True)


def test_command_force_output_and_non_force_unchanged(make_resident: Callable, clock: Callable) -> None:
    _periode, people = _allocated(make_resident, clock)
    _handoff("tier_a_take", people)
    out = StringIO()
    call_command("allocate_koekkenvagter", "2042", "3", "--force", stdout=out)
    assert "1 aftalte byttehandler bevaret, 0 åbne tilbud bortfaldet." in out.getvalue()
    out = StringIO()
    with pytest.raises(Exception, match="allerede"):
        call_command("allocate_koekkenvagter", "2042", "3", stdout=out)
    assert "aftalte byttehandler" not in out.getvalue()


def test_command_batch_force_reports_per_month(make_resident: Callable, clock: Callable) -> None:
    _periode, people = _allocated(make_resident, clock)
    for m in (2, 4):
        for p in people:
            _place(p, 2042, m)
    clock(TODAY)
    call_command("allocate_koekkenvagter", "2042", "3", "--batch", "--force", stdout=StringIO())
    _handoff("tier_a_take", people)
    out = StringIO()
    call_command("allocate_koekkenvagter", "2042", "3", "--batch", "--force", stdout=out)
    lines = [ln for ln in out.getvalue().splitlines() if "aftalte byttehandler" in ln]
    assert len(lines) == 3
    assert any(ln.startswith("2042-03") and "1 aftalte" in ln for ln in lines)


def test_allokering_view_message_has_both_numbers(make_resident: Callable, clock: Callable) -> None:
    _allocated_result = _allocated(make_resident, clock)
    manager = make_resident(email="mgr2@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    c = Client()
    c.force_login(manager)
    resp = c.post("/intern/koekken/gruppe/allokering", {"year": 2042, "month": 3, "force": "on"}, follow=True)
    assert "0 aftalte byttehandler bevaret, 0 åbne tilbud bortfaldet." in resp.content.decode()


# --------------------------------------------------------------------- reconciliation / fridag


def test_reconciliation_keeps_present_handoff_and_vacates_departed_taker(make_resident: Callable) -> None:
    year, month = 2042, 6
    present = make_resident(email="rp@gahk.dk")
    leaver = make_resident(email="rl@gahk.dk")
    other = make_resident(email="ro@gahk.dk")
    for r in (present, leaver, other):
        _place(r, year, 5)
    _vagt(date(2042, 6, 10), VagtRegel.Kind.MORGEN, 1, 60)
    _vagt(date(2042, 6, 11), VagtRegel.Kind.MORGEN, 1, 60)
    allocate_tier_a(year, month)  # projected from May's list
    first, second = list(VagtTildeling.objects.order_by("vagt__date"))

    def give(row: VagtTildeling, to: Resident) -> VagtTildeling:
        if row.resident_id != to.pk:
            take_over(offer_tildeling(row, row.resident), to)
        return VagtTildeling.objects.get(pk=row.pk)

    first_was_handed_off = first.resident_id != present.pk  # allocation may already have chosen `present`
    first, second = give(first, present), give(second, leaver)
    for r in (present, other):
        _place(r, year, month)  # the real list excludes `leaver`

    result = reconcile_month(year, month)

    assert leaver in result.vacated
    assert not VagtTildeling.objects.filter(pk=second.pk, resident=leaver).exists()
    kept = VagtTildeling.objects.get(pk=first.pk)  # present resident's hand-off untouched
    assert kept.resident == present and kept.vagt_id == first.vagt_id
    assert kept.byttetilbud.filter(status=B.OVERTAGET, overtaget_af=present).exists() == first_was_handed_off


def test_fridag_removes_offer_and_notifies_offerer_keeps_other_handoff(w: World, pushes: list) -> None:
    _sub(w.c)
    take_over(offer_tildeling(w.ra, w.a), w.e)
    bytte = offer_tildeling(w.rc, w.c)
    result = declare_fridag(AV, [VagtRegel.Kind.AFTEN], "Test")
    assert w.c in [r for r, _v in result.removed]
    assert [r.pk for r, _a, _m in result.notifications].count(w.c.pk) == 1
    assert not VagtBytte.objects.filter(pk=bytte.pk).exists()
    w.ra.refresh_from_db()
    assert w.ra.resident == w.e and VagtBytte.objects.filter(status=B.OVERTAGET).count() == 1


# ----------------------------------------------------------------------------------- views


@pytest.fixture
def login(w: World) -> Callable[[Resident], Client]:
    def _login(r: Resident) -> Client:
        c = Client()
        c.force_login(r)
        return c

    return _login


def _html(c: Client) -> str:
    return c.get("/intern/koekken/").content.decode()


def test_buttons_absent_where_predicate_false(w: World, login: Callable) -> None:
    base = "/intern/koekken/"
    ba = offer_tildeling(w.ra, w.a)
    bc = offer_tildeling(w.rc, w.c)
    # b: can take a's offer; the partner-only whole action is absent
    html = _html(login(w.b))
    assert f"{base}bytte/{ba.pk}/tag" in html and f"{base}bytte/{ba.pk}/tag-hele" not in html
    assert "Tag vagten" in html and f"{base}bytte/{bc.pk}/tag-hele" not in html
    # d (c's partner): whole only for c's offer, plain take absent
    html = _html(login(w.d))
    assert f"{base}bytte/{bc.pk}/tag-hele" in html and f'{base}bytte/{bc.pk}/tag"' not in html
    assert "Tag hele vagten (6 t)" in html
    # outsider: nothing takeable
    html = _html(login(w.outsider))
    assert f"{base}bytte/{ba.pk}/tag" not in html and "tag-hele" not in html
    # offerer: withdraw present, offer absent; own offer never on own board
    html = _html(login(w.a))
    assert f"{base}bytte/{ba.pk}/traek-tilbage" in html and f"{base}vagt/{w.ra.pk}/tilbyd" not in html
    assert f"{base}bytte/{ba.pk}/tag" not in html
    assert "Tilbudt — ingen har taget den endnu" in html
    # b can offer own row; flagged-history row has no offer button
    assert f"{base}vagt/{w.rb.pk}/tilbyd" in _html(login(w.b))
    flag_tildeling(w.rb, w.a)
    resolve_anmeldelse(w.rb.anmeldelser.get(), upheld=False, resolved_by=w.c)
    assert f"{base}vagt/{w.rb.pk}/tilbyd" not in _html(login(w.b))


def test_empty_state(w: World, login: Callable) -> None:
    assert "Ingen vagter til overtagelse lige nu." in _html(login(w.b))


def test_post_flow_and_stale_take_and_403(w: World, login: Callable) -> None:
    ca, cb, ce = login(w.a), login(w.b), login(w.e)
    resp = ca.post(f"/intern/koekken/vagt/{w.ra.pk}/tilbyd")
    assert resp.status_code == 200 and 'id="koekken-bytte"' in resp.content.decode()
    bytte = VagtBytte.objects.get(status=B.AABEN)
    assert cb.post(f"/intern/koekken/vagt/{w.ra.pk}/tilbyd").status_code == 403  # not their row
    assert cb.post(f"/intern/koekken/bytte/{bytte.pk}/traek-tilbage").status_code == 403
    assert cb.get(f"/intern/koekken/bytte/{bytte.pk}/tag").status_code == 405
    assert cb.post(f"/intern/koekken/bytte/{bytte.pk}/tag").status_code == 200
    stale = ce.post(f"/intern/koekken/bytte/{bytte.pk}/tag")
    assert stale.status_code == 200 and "ikke længere åbent" in stale.content.decode()
    w.ra.refresh_from_db()
    assert w.ra.resident == w.b
    # whole-shift via the view
    bc = offer_tildeling(w.rc, w.c)
    assert login(w.d).post(f"/intern/koekken/bytte/{bc.pk}/tag-hele").status_code == 200
    assert Vagt.objects.get(pk=w.av.pk).headcount == 1
    # withdraw via the view
    ca.post(f"/intern/koekken/vagt/{w.rb.pk}/tilbyd")  # a does not hold rb -> 403 already covered
    bb = offer_tildeling(VagtTildeling.objects.get(pk=w.rb.pk), w.b)
    assert cb.post(f"/intern/koekken/bytte/{bb.pk}/traek-tilbage").status_code == 200
    assert VagtBytte.objects.get(pk=bb.pk).status == B.TRUKKET


def test_partial_and_full_page_render_same_content(w: World, login: Callable) -> None:
    ca = login(w.a)
    partial = ca.post(f"/intern/koekken/vagt/{w.ra.pk}/tilbyd").content.decode()
    full = _html(ca)

    def norm(s: str) -> str:
        s = re.sub(r'name="csrfmiddlewaretoken" value="[^"]*"', "", s)
        return re.sub(r"\s+", " ", s).strip()

    p = norm(partial)
    start = norm(full).index('<div id="koekken-bytte">')
    assert norm(full)[start : start + len(p)] == p


def test_gruppe_lists_only_soon_unexpired_offers(
    w: World, login: Callable, make_resident: Callable, clock: Callable
) -> None:
    mgr = make_resident(email="mgr3@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    far = _hold(_vagt(date(2042, 3, 25), VagtRegel.Kind.MORGEN, 1, 60), w.d)
    old = _hold(_vagt(date(2042, 2, 27), VagtRegel.Kind.MORGEN, 1, 60), w.e)
    clock(date(2042, 2, 26))
    offer_tildeling(old, w.e)
    clock(TODAY)
    offer_tildeling(far, w.d)
    offer_tildeling(w.ra, w.a)
    html = login(mgr).get("/intern/koekken/gruppe/").content.decode()
    assert "Åbne byttetilbud" in html
    assert w.a.full_name in html and w.d.full_name not in html
    clock(date(2042, 3, 26))
    html = login(mgr).get("/intern/koekken/gruppe/").content.decode()
    assert "Ingen åbne tilbud." in html


def test_index_query_count_does_not_grow(w: World, login: Callable) -> None:
    c = login(w.b)

    def count() -> int:
        with CaptureQueriesContext(connection) as ctx:
            assert c.get("/intern/koekken/").status_code == 200
        return len(ctx)

    for m in (4, 5):
        _place(w.b, 2042, m)
        _place(w.a, 2042, m)
    # Baseline: one open offer in each of two months (one population lookup per month is by design).
    offer_tildeling(_hold(_vagt(date(2042, 3, 16), VagtRegel.Kind.FROKOST, 1, 60), w.a), w.a)
    offer_tildeling(_hold(_vagt(date(2042, 4, 13), VagtRegel.Kind.FROKOST, 1, 60), w.c), w.c)
    c.get("/intern/koekken/")  # warm caches
    small = count()
    for i in range(6):
        d = date(2042, 3, 17) + timedelta(days=i)
        _hold(_vagt(d, VagtRegel.Kind.MORGEN, 1, 60), w.b)
        offer_tildeling(_hold(_vagt(d, VagtRegel.Kind.FROKOST, 1, 60), w.a), w.a)
    for i in range(3):
        d = date(2042, 4, 14) + timedelta(days=i)
        offer_tildeling(_hold(_vagt(d, VagtRegel.Kind.MORGEN, 1, 60), w.c), w.c)
    assert count() == small


def test_no_unclaim_url_exists() -> None:
    text = (Path(__file__).resolve().parent.parent / "koekken" / "urls.py").read_text()
    assert "unclaim" not in text.lower() and "frafald" not in text.lower()
    for fragment in (
        "vagt/<int:pk>/tilbyd",
        "bytte/<int:pk>/traek-tilbage",
        "bytte/<int:pk>/tag",
        "bytte/<int:pk>/tag-hele",
    ):
        assert fragment in text


# ------------------------------------------------------------------------------------- demo


def test_demo_produces_handoffs() -> None:
    if resolve_periode(timezone.localdate()).kind == Periode.Kind.SOMMER:
        pytest.skip("demo skips the allocation scenarios in summer")
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    assert VagtBytte.objects.filter(status=B.AABEN).count() == 1
    assert VagtBytte.objects.filter(status=B.OVERTAGET).count() == 1
    # Step 2: one pending proposal on the open offer, one completed trade between two other residents.
    pending = VagtBytteForslag.objects.get(status=F.AABEN)
    assert pending.bytte.status == B.AABEN
    done = VagtBytteForslag.objects.get(status=F.ACCEPTERET)
    assert done.bytte.status == B.BYTTET and done.bytte.overtaget_af == done.foreslaaet_af
    assert done.bytte.tilbudt_af_id not in (pending.foreslaaet_af_id, pending.bytte.tilbudt_af_id)
    # Step 3: the open offer is shared, and Den Hurtige's own seeding (which runs after) leaves it alone.
    from core.clock import current_datetime

    open_offer = VagtBytte.objects.get(status=B.AABEN)
    assert open_offer.hurtig_post is not None
    assert (
        open_offer.hurtig_post.channel == "koekken" and open_offer.hurtig_post.author == open_offer.tilbudt_af
    )
    assert (
        open_offer.hurtig_post.expires_at > current_datetime()
        and "/intern/koekken/" in open_offer.hurtig_post.content
    )
    hel = VagtBytte.objects.get(status=B.OVERTAGET_HEL)
    assert (hel.tildeling.vagt.headcount, hel.tildeling.vagt.duration_minutes) == (1, 360)


# ======================================================================== Amendment 4, step 2: trading


def _ye(w: World) -> VagtTildeling:
    """e's own single 3 h row, on a later date than every `w` row: the unequal-trade partner."""
    return _hold(_vagt(date(2042, 3, 20), VagtRegel.Kind.AFTEN, 1, 180), w.e)


def _open_proposals() -> int:
    return VagtBytteForslag.objects.filter(status=F.AABEN).count()


def _state(*rows: VagtTildeling) -> list[tuple[int, int, str]]:
    return [
        (r.pk, r.resident_id, r.status) for r in VagtTildeling.objects.filter(pk__in=[r.pk for r in rows])
    ]


# ------------------------------------------------------------------------------------- propose


def test_propose_creates_open_proposal_and_moves_nothing(w: World) -> None:
    bytte = offer_tildeling(w.ra, w.a)
    before = _state(w.ra, w.rb)
    forslag = propose_trade(bytte, w.rb, w.b)
    assert forslag.status == F.AABEN and forslag.foreslaaet_af == w.b and forslag.modydelse_id == w.rb.pk
    assert forslag.bytte_id == bytte.pk and forslag.closed_at is None
    assert _state(w.ra, w.rb) == before


def test_propose_refusals(w: World, clock: Callable) -> None:
    bytte = offer_tildeling(w.ra, w.a)
    assert "egne" in _refuses(propose_trade, bytte, w.rb, w.e)  # not your row
    assert "eget tilbud" in _refuses(propose_trade, bytte, w.ra, w.a)  # the offerer's own offer

    done = _hold(_vagt(date(2042, 3, 20), VagtRegel.Kind.MORGEN, 1, 60), w.e, T.UDFOERT)
    assert "udført" in _refuses(propose_trade, bytte, done, w.e)  # not TILDELT

    flagged = _hold(_vagt(date(2042, 3, 21), VagtRegel.Kind.MORGEN, 1, 60), w.e)
    flag_tildeling(flagged, w.b, "x")
    resolve_anmeldelse(flagged.anmeldelser.get(), upheld=False, resolved_by=w.c)
    flagged.refresh_from_db()
    assert flagged.status == T.TILDELT
    assert "anmeldelse" in _refuses(propose_trade, bytte, flagged, w.e)  # flag history, any status

    own = _hold(_vagt(date(2042, 3, 22), VagtRegel.Kind.MORGEN, 1, 60), w.e)
    offer_tildeling(own, w.e)
    assert "Træk dit eget tilbud" in _refuses(propose_trade, bytte, own, w.e)

    # Y started: an earlier shift of e's, with the clock past it but before X.
    early = _hold(_vagt(date(2042, 3, 10), VagtRegel.Kind.MORGEN, 1, 60), w.e)
    clock(date(2042, 3, 11))
    assert "startet" in _refuses(propose_trade, bytte, early, w.e)
    assert _open_proposals() == 0


def test_propose_refuses_same_vagt_places_and_offerer_holds_y(w: World) -> None:
    # Both places of a two-person shift: d holds X's Vagt, so trading rd for rc would change nothing.
    bc = offer_tildeling(w.rc, w.c)
    assert "allerede en plads" in _refuses(propose_trade, bc, w.rd, w.d)
    assert not services.proposable_rows(w.d, bc)
    # The offerer already holds a place on Y's Vagt.
    other = _vagt(date(2042, 3, 18), VagtRegel.Kind.AFTEN, 2, 180)
    _hold(other, w.a)
    ye = _hold(other, w.e)
    ba = offer_tildeling(w.ra, w.a)
    assert "allerede en plads" in _refuses(propose_trade, ba, ye, w.e)
    assert ye not in services.proposable_rows(w.e, ba)


def test_propose_refuses_when_may_hold_fails(w: World) -> None:
    ba = offer_tildeling(w.ra, w.a)
    y_out = _hold(_vagt(date(2042, 3, 24), VagtRegel.Kind.MORGEN, 1, 60), w.outsider)
    assert "beboerlisten" in _refuses(propose_trade, ba, y_out, w.outsider)  # proposer may not hold X
    ye = _ye(w)
    Resident.objects.filter(pk=w.a.pk).update(move_out_date=date(2042, 3, 15))  # before Y, after X
    w.a.refresh_from_db()
    msg = _refuses(propose_trade, ba, ye, w.e)
    assert "fraflyttet" in msg and w.a.full_name in msg  # the offerer may not hold Y


def test_propose_duplicate_and_stale_offer(w: World, clock: Callable) -> None:
    ba = offer_tildeling(w.ra, w.a)
    propose_trade(ba, w.rb, w.b)
    assert "allerede foreslået" in _refuses(propose_trade, ba, w.rb, w.b)
    assert _open_proposals() == 1
    with pytest.raises(IntegrityError), transaction.atomic():
        VagtBytteForslag.objects.create(bytte=ba, modydelse=w.rb, foreslaaet_af=w.b)  # the DB constraint
    # A different Y by the same person is fine, and a closed one does not block a new one.
    ye = _ye(w)
    propose_trade(ba, ye, w.e)
    withdraw_offer(ba, w.a)
    assert _open_proposals() == 0
    assert "ikke længere åbent" in _refuses(propose_trade, ba, w.rb, w.b)  # withdrawn offer
    # X's offerer no longer holds it: the offer lapses, persisted, and the call refuses.
    bb = offer_tildeling(w.rb, w.b)
    VagtTildeling.objects.filter(pk=w.rb.pk).update(resident=w.e)
    assert "bortfaldet" in _refuses(propose_trade, bb, w.ra, w.a)
    assert VagtBytte.objects.get(pk=bb.pk).status == B.BORTFALDET
    # Expired offer: refused, not persisted (lazy expiry).
    bc = offer_tildeling(w.ra, w.a)
    clock(date(2042, 3, 13))
    assert "startet" in _refuses(propose_trade, bc, w.rb, w.b)
    assert VagtBytte.objects.get(pk=bc.pk).status == B.AABEN


def test_propose_refuses_flag_history_on_x_and_lapses_offer(w: World) -> None:
    bytte = _flagged_then_dismissed_offer(w, w.ra, w.a)
    assert not services.proposable_rows(w.b, bytte)
    assert "anmeldelse" in _refuses(propose_trade, bytte, w.rb, w.b)
    assert VagtBytte.objects.get(pk=bytte.pk).status == B.BORTFALDET
    assert _open_proposals() == 0


# -------------------------------------------------------------------------------------- accept


def test_accept_mechanics_unequal_trade(w: World) -> None:
    ye = _ye(w)  # 3 h for a's 1 h
    bytte = offer_tildeling(w.ra, w.a)
    forslag = propose_trade(bytte, ye, w.e)
    a_before, e_before = projected_balance_for(w.a), projected_balance_for(w.e)
    out = accept_trade(forslag, w.a)
    assert out.pk == forslag.pk and out.status == F.ACCEPTERET and out.closed_at is not None
    w.ra.refresh_from_db()
    ye.refresh_from_db()
    assert (w.ra.resident, ye.resident) == (w.e, w.a)  # pks unchanged, residents swapped
    assert w.ra.status == ye.status == T.TILDELT
    bytte.refresh_from_db()
    assert bytte.status == B.BYTTET and bytte.overtaget_af == w.e and bytte.closed_at is not None
    assert projected_balance_for(w.a) == a_before + 120  # gave 1 h, got 3 h
    assert projected_balance_for(w.e) == e_before - 120
    mark_udfoert(w.ra, at=timezone.make_aware(datetime(2042, 3, 12, 12, 0)))
    mark_udfoert(ye, at=timezone.make_aware(datetime(2042, 3, 20, 19, 0)))
    credits = {
        p.resident_id: p.delta_minutes for p in KoekkenPost.objects.filter(kind=KoekkenPost.Kind.ARBEJDE)
    }
    assert credits == {w.e.pk: 60, w.a.pk: 180}  # each earns the row they now hold
    # Both sides are completed hand-offs.
    assert VagtTildeling.objects.filter(services.handed_off_tildeling_filter()).count() == 2


def test_accept_lapses_everything_the_swap_invalidated(w: World) -> None:
    ye = _ye(w)
    third = _hold(_vagt(date(2042, 3, 25), VagtRegel.Kind.MORGEN, 1, 60), w.d)
    ba = offer_tildeling(w.ra, w.a)
    f_main = propose_trade(ba, w.rb, w.b)
    f_other = propose_trade(ba, ye, w.e)  # a second proposal on the same offer
    f_elsewhere = propose_trade(ba, third, w.d)
    # b then offers rb (Y's own offer), and e proposes on THAT offer; a proposal naming rb as its
    # modydelse on yet another offer also exists.
    other_offer = offer_tildeling(_hold(_vagt(date(2042, 3, 26), VagtRegel.Kind.MORGEN, 1, 60), w.c), w.c)
    f_uses_y = propose_trade(other_offer, w.rb, w.b)
    bb = offer_tildeling(w.rb, w.b)
    f_on_yoffer = VagtBytteForslag.objects.create(bytte=bb, modydelse=ye, foreslaaet_af=w.e)
    # (f_on_yoffer is created directly: e's ye is already proposed elsewhere, which is allowed.)
    accept_trade(f_main, w.a)
    for f in (f_other, f_elsewhere, f_uses_y, f_on_yoffer):
        f.refresh_from_db()
        assert f.status == F.BORTFALDET and f.closed_at is not None, f
    assert VagtBytte.objects.get(pk=bb.pk).status == B.BORTFALDET  # Y's own offer lapsed
    assert VagtBytte.objects.get(pk=other_offer.pk).status == B.AABEN  # unrelated offer untouched
    assert VagtBytteForslag.objects.get(pk=f_main.pk).status == F.ACCEPTERET


def test_accept_integrity_race_on_second_row_leaves_both_unchanged(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ye = _ye(w)
    ba = offer_tildeling(w.ra, w.a)
    forslag = propose_trade(ba, ye, w.e)
    real = services.may_hold

    def conflicting(resident: Resident, vagt: Vagt, **kw: object) -> bool:
        if resident.pk == w.a.pk and vagt.pk == ye.vagt_id:
            _hold(vagt, w.a)  # a concurrent insert landing on Y's Vagt AFTER the existence checks
        return real(resident, vagt, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(services, "may_hold", conflicting)
    assert "plads på den anden vagt" in _refuses(accept_trade, forslag, w.a)
    w.ra.refresh_from_db()
    ye.refresh_from_db()
    assert (w.ra.resident, ye.resident) == (w.a, w.e)  # the first row's update was rolled back too
    assert VagtBytte.objects.get(pk=ba.pk).status == B.AABEN
    assert VagtBytteForslag.objects.get(pk=forslag.pk).status == F.BORTFALDET  # can never succeed now
    assert not VagtBytte.objects.filter(status=B.BYTTET).exists()


def test_accept_refusals(w: World, clock: Callable) -> None:
    ye = _ye(w)
    ba = offer_tildeling(w.ra, w.a)
    f1 = propose_trade(ba, w.rb, w.b)
    assert "tilbyderen" in _refuses(accept_trade, f1, w.b)  # not the offerer
    assert "tilbyderen" in _refuses(accept_trade, f1, w.e)
    assert VagtBytteForslag.objects.get(pk=f1.pk).status == F.AABEN
    accept_trade(f1, w.a)
    assert "ikke længere åbent" in _refuses(accept_trade, f1, w.a)  # already closed (stale object)
    # Y moved since proposing: lapse is persisted, then refused.
    bb = offer_tildeling(VagtTildeling.objects.get(pk=w.rb.pk), w.a)  # a now holds rb
    f2 = propose_trade(bb, ye, w.e)
    VagtTildeling.objects.filter(pk=ye.pk).update(resident=w.d)
    assert not services.can_accept(
        VagtBytteForslag.objects.select_related(
            "bytte__tildeling__vagt", "modydelse__vagt", "foreslaaet_af"
        ).get(pk=f2.pk),
        w.a,
    )
    assert "bortfaldet" in _refuses(accept_trade, f2, w.a)
    assert VagtBytteForslag.objects.get(pk=f2.pk).status == F.BORTFALDET
    assert VagtBytte.objects.get(pk=bb.pk).status == B.AABEN  # X's side was fine: the offer stays open


def test_accept_refuses_when_y_started_or_flagged_or_x_flagged(w: World, clock: Callable) -> None:
    early = _hold(_vagt(date(2042, 3, 10), VagtRegel.Kind.MORGEN, 1, 60), w.e)
    ba = offer_tildeling(w.ra, w.a)
    f = propose_trade(ba, early, w.e)
    clock(date(2042, 3, 11))  # Y has started, X has not
    assert "startet" in _refuses(accept_trade, f, w.a)
    assert VagtBytteForslag.objects.get(pk=f.pk).status == F.BORTFALDET
    clock(TODAY)
    # Y flagged since proposing. Flagging lapses the proposal at once; reopen it by hand to simulate a
    # row left open by data from before that rule, which the accept-time re-check must still refuse.
    y2 = _hold(_vagt(date(2042, 3, 21), VagtRegel.Kind.MORGEN, 1, 60), w.e)
    f2 = propose_trade(ba, y2, w.e)
    flag_tildeling(y2, w.b, "x")
    assert VagtBytteForslag.objects.get(pk=f2.pk).status == F.BORTFALDET
    resolve_anmeldelse(y2.anmeldelser.get(), upheld=False, resolved_by=w.c)  # back to TILDELT, history kept
    VagtBytteForslag.objects.filter(pk=f2.pk).update(status=F.AABEN, closed_at=None)
    assert "anmeldelse" in _refuses(accept_trade, f2, w.a)
    assert VagtBytteForslag.objects.get(pk=f2.pk).status == F.BORTFALDET
    # X flagged (dismissed back): the OFFER lapses too.
    w.ra.refresh_from_db()
    y3 = _hold(_vagt(date(2042, 3, 22), VagtRegel.Kind.MORGEN, 1, 60), w.e)
    f3 = propose_trade(ba, y3, w.e)
    flag_tildeling(w.ra, w.b, "x")
    resolve_anmeldelse(w.ra.anmeldelser.get(), upheld=False, resolved_by=w.c)
    VagtBytte.objects.filter(pk=ba.pk).update(status=B.AABEN, closed_at=None)
    VagtBytteForslag.objects.filter(pk=f3.pk).update(status=F.AABEN, closed_at=None)
    assert "anmeldelse" in _refuses(accept_trade, f3, w.a)
    assert VagtBytte.objects.get(pk=ba.pk).status == B.BORTFALDET
    assert VagtBytteForslag.objects.get(pk=f3.pk).status == F.BORTFALDET
    w.ra.refresh_from_db()
    assert w.ra.resident == w.a


def test_accept_after_x_was_taken_meanwhile(w: World) -> None:
    ba = offer_tildeling(w.ra, w.a)
    f = propose_trade(ba, w.rb, w.b)
    stale = VagtBytteForslag.objects.get(pk=f.pk)
    take_over(ba, w.e)  # someone else took X first: the proposal lapsed
    assert VagtBytteForslag.objects.get(pk=f.pk).status == F.BORTFALDET
    assert _refuses(accept_trade, stale, w.a)
    w.ra.refresh_from_db()
    w.rb.refresh_from_db()
    assert (w.ra.resident, w.rb.resident) == (w.e, w.b)


def test_accept_allows_y_that_gained_an_offer_and_lapses_it(w: World) -> None:
    ba = offer_tildeling(w.ra, w.a)
    f = propose_trade(ba, w.rb, w.b)
    bb = offer_tildeling(w.rb, w.b)  # Y gains an open offer AFTER the proposal was made
    accept_trade(f, w.a)
    assert VagtBytte.objects.get(pk=bb.pk).status == B.BORTFALDET
    w.rb.refresh_from_db()
    assert w.rb.resident == w.a


def test_collapsed_shift_trades_like_any_other(w: World) -> None:
    take_over_whole(offer_tildeling(w.rc, w.c), w.d)  # av is now (1, 360), held by d via rd
    ba = offer_tildeling(w.ra, w.a)
    f = propose_trade(ba, VagtTildeling.objects.get(pk=w.rd.pk), w.d)  # 1 h for 6 h
    a_before = projected_balance_for(w.a)
    accept_trade(f, w.a)
    assert projected_balance_for(w.a) == a_before + 300
    w.rd.refresh_from_db()
    assert w.rd.resident == w.a


# ---------------------------------------------------------------------------- decline / withdraw


def test_decline_and_withdraw(w: World, clock: Callable) -> None:
    ba = offer_tildeling(w.ra, w.a)
    f = propose_trade(ba, w.rb, w.b)
    assert "tilbyderen" in _refuses(decline_proposal, f, w.b)
    assert "egne" in _refuses(withdraw_proposal, f, w.a)
    assert _refuses(withdraw_proposal, f, w.e)
    out = decline_proposal(f, w.a)
    assert out.status == F.AFVIST and out.closed_at
    assert "ikke længere åbent" in _refuses(withdraw_proposal, f, w.b)
    assert "ikke længere åbent" in _refuses(decline_proposal, f, w.a)
    f2 = propose_trade(ba, w.rb, w.b)  # a declined one does not block a fresh proposal
    out = withdraw_proposal(f2, w.b)
    assert out.status == F.TRUKKET and out.closed_at
    # Declining stays possible after the offer expired (cleanup).
    f3 = propose_trade(ba, w.rb, w.b)
    clock(date(2042, 3, 13))
    assert decline_proposal(f3, w.a).status == F.AFVIST
    assert w.ra.resident_id == w.a.pk and w.rb.resident_id == w.b.pk


def test_predicates(w: World) -> None:
    ye = _ye(w)
    ba = offer_tildeling(w.ra, w.a)
    f = propose_trade(ba, ye, w.e)
    f = VagtBytteForslag.objects.select_related(
        "bytte__tildeling__vagt", "modydelse__vagt", "foreslaaet_af"
    ).get(pk=f.pk)
    assert services.can_accept(f, w.a) and services.can_decline(f, w.a)
    assert not services.can_accept(f, w.e) and not services.can_decline(f, w.e)
    assert services.can_withdraw_proposal(f, w.e) and not services.can_withdraw_proposal(f, w.a)
    assert services.forslag_is_live(f)
    assert w.rb in services.proposable_rows(w.b, ba) and ye not in services.proposable_rows(w.a, ba)
    assert ye not in services.proposable_rows(w.e, ba)  # already proposed
    assert services.proposable_rows(w.a, ba) == []  # the offerer
    assert services.proposable_rows(w.outsider, ba) == []


# ------------------------------------------------------------------------- step 1 extensions


def test_withdraw_offer_lapses_proposals(w: World) -> None:
    ba = offer_tildeling(w.ra, w.a)
    f = propose_trade(ba, w.rb, w.b)
    withdraw_offer(ba, w.a)
    f.refresh_from_db()
    assert f.status == F.BORTFALDET and f.closed_at is not None


def test_take_over_lapses_the_offers_and_xs_proposals(w: World) -> None:
    ye = _ye(w)
    ba = offer_tildeling(w.ra, w.a)
    on_offer = propose_trade(ba, w.rb, w.b)
    # a proposes ra (as Y) on someone else's offer BEFORE offering it -- here ra is already offered, so
    # build that state by hand: the proposal exists, then the row gained its own offer.
    be = offer_tildeling(ye, w.e)
    uses_x = VagtBytteForslag.objects.create(bytte=be, modydelse=w.ra, foreslaaet_af=w.a)
    take_over(ba, w.d)
    for f in (on_offer, uses_x):
        f.refresh_from_db()
        assert f.status == F.BORTFALDET, f
    assert VagtBytte.objects.get(pk=be.pk).status == B.AABEN  # the unrelated offer itself survives


def test_whole_shift_take_over_lapses_and_cascades(w: World) -> None:
    ye = _ye(w)
    third = _hold(_vagt(date(2042, 3, 25), VagtRegel.Kind.MORGEN, 1, 60), w.a)
    be = offer_tildeling(ye, w.e)
    uses_vacated = propose_trade(be, w.rc, w.c)  # c's row is the one that will be deleted
    uses_partner = propose_trade(be, w.rd, w.d)  # d's row survives, as a whole shift
    bc = offer_tildeling(w.rc, w.c)  # c can still offer rc after proposing it
    on_offer = propose_trade(bc, third, w.a)
    take_over_whole(bc, w.d)
    assert not VagtBytteForslag.objects.filter(pk=uses_vacated.pk).exists()  # cascaded with its row
    for f in (uses_partner, on_offer):
        f.refresh_from_db()
        assert f.status == F.BORTFALDET, f
    assert VagtBytte.objects.get(pk=bc.pk).status == B.OVERTAGET_HEL


def test_flagging_lapses_offer_proposals_and_proposals_using_the_row(w: World) -> None:
    ye = _ye(w)
    ba = offer_tildeling(w.ra, w.a)
    on_offer = propose_trade(ba, ye, w.e)
    be = offer_tildeling(w.rb, w.b)
    uses_flagged = propose_trade(be, w.rd, w.d)
    flag_tildeling(w.rd, w.a, "x")  # flagging Y lapses every proposal that uses it ...
    uses_flagged.refresh_from_db()
    on_offer.refresh_from_db()
    assert uses_flagged.status == F.BORTFALDET and on_offer.status == F.AABEN
    flag_tildeling(w.ra, w.b, "x")  # ... and flagging X lapses the offer and its proposals
    on_offer.refresh_from_db()
    assert on_offer.status == F.BORTFALDET
    assert VagtBytte.objects.get(pk=ba.pk).status == B.BORTFALDET
    assert VagtBytte.objects.get(pk=be.pk).status == B.AABEN


# ------------------------------------------------------------------------ Q2c force survival


def _trade_pair(kind: str) -> tuple[VagtTildeling, VagtTildeling]:
    kinds = [VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST] if kind == "a" else [VagtRegel.Kind.AFTEN]
    rows = list(
        VagtTildeling.objects.filter(
            vagt__kind__in=kinds, vagt__date__gt=TODAY, vagt__date__month=3, status=T.TILDELT
        )
        .select_related("vagt")
        .order_by("vagt__date", "pk")
    )
    for x in rows:
        for y in rows:
            if y.vagt_id == x.vagt_id or y.resident_id == x.resident_id:
                continue
            holders = set(
                VagtTildeling.objects.filter(vagt__in=[x.vagt_id, y.vagt_id]).values_list(
                    "vagt_id", "resident_id"
                )
            )
            if (x.vagt_id, y.resident_id) in holders or (y.vagt_id, x.resident_id) in holders:
                continue
            return x, y
    raise AssertionError("no tradable pair")


def _trade(kind: str) -> tuple[int, int, int, int]:
    x, y = _trade_pair(kind)
    xr, yr = x.resident, y.resident
    accept_trade(propose_trade(offer_tildeling(x, xr), y, yr), xr)
    return x.pk, y.pk, yr.pk, xr.pk  # x now with yr, y now with xr


TRADE_CASES = [("month", "a"), ("month", "aften"), ("tier_a", "a"), ("tier_b", "aften")]


@pytest.mark.parametrize(("entry", "kind"), TRADE_CASES)
def test_both_sides_of_a_trade_survive_force_rerun(
    entry: str, kind: str, make_resident: Callable, clock: Callable
) -> None:
    _periode, _people = _allocated(make_resident, clock)
    x_pk, y_pk, x_holder, y_holder = _trade(kind)
    assert force_rerun_impact(2042, 3) == (2, 0)  # BOTH sides count as kept
    _force(entry)
    x, y = VagtTildeling.objects.get(pk=x_pk), VagtTildeling.objects.get(pk=y_pk)
    assert (x.resident_id, y.resident_id) == (x_holder, y_holder)
    assert x.status == y.status == T.TILDELT
    assert VagtBytte.objects.get(tildeling=x).status == B.BYTTET
    assert VagtBytteForslag.objects.get(modydelse=y).status == F.ACCEPTERET
    for v in Vagt.objects.filter(date__month=3):
        assert v.tildelinger.count() <= v.headcount
    assert force_rerun_impact(2042, 3) == (2, 0)


def test_force_rerun_deletes_plain_rows_with_proposals_on_either_side(
    make_resident: Callable, clock: Callable
) -> None:
    """Un-traded rows with an open offer and proposals on either side are replaced by a force re-run: the
    cascade through offers and proposals must not trip a foreign key, and nothing dangles afterwards."""
    _periode, _people = _allocated(make_resident, clock)
    x, y = _trade_pair("a")
    z = next(
        r
        for r in VagtTildeling.objects.filter(vagt__date__gt=TODAY, status=T.TILDELT)
        if r.pk not in (x.pk, y.pk)
    )
    bx = offer_tildeling(x, x.resident)
    propose_trade(bx, y, y.resident)  # X side (offer + proposal) and Y side (modydelse)
    bz = offer_tildeling(z, z.resident)
    assert bz.pk
    allocate_month(2042, 3, force=True)
    assert not VagtBytte.objects.filter(status=B.AABEN).exists()
    assert not VagtBytteForslag.objects.exists()
    for v in Vagt.objects.filter(date__month=3):
        assert v.tildelinger.count() <= v.headcount


def test_force_rerun_with_trade_is_idempotent(make_resident: Callable, clock: Callable) -> None:
    _periode, _people = _allocated(make_resident, clock)
    _trade("a")
    snap = lambda: set(  # noqa: E731
        VagtTildeling.objects.filter(vagt__date__month=3).values_list("vagt_id", "resident_id")
    )
    allocate_month(2042, 3, force=True)
    first = snap()
    allocate_month(2042, 3, force=True)
    assert snap() == first
    assert force_rerun_impact(2042, 3) == (2, 0)


# ------------------------------------------------------------------------------------ deleters


def test_override_remove_deletes_rows_with_proposals_on_either_side(
    w: World, login: Callable, make_resident: Callable
) -> None:
    ye = _ye(w)
    mgr = make_resident(email="mgr-t@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    cm = login(mgr)
    ba = offer_tildeling(w.ra, w.a)
    on_x = propose_trade(ba, ye, w.e)
    assert cm.post(f"/intern/koekken/gruppe/override/{w.ra.pk}/fjern").status_code == 302  # X side
    assert (
        not VagtBytteForslag.objects.filter(pk=on_x.pk).exists()
        and not VagtBytte.objects.filter(pk=ba.pk).exists()
    )
    bb = offer_tildeling(w.rb, w.b)
    as_y = propose_trade(bb, ye, w.e)
    assert cm.post(f"/intern/koekken/gruppe/override/{ye.pk}/fjern").status_code == 302  # Y side
    assert not VagtBytteForslag.objects.filter(pk=as_y.pk).exists()
    assert VagtBytte.objects.get(pk=bb.pk).status == B.AABEN  # the offer survives its proposal


def test_reconcile_deletes_rows_with_proposals_on_either_side(w: World) -> None:
    ye = _ye(w)
    ba = offer_tildeling(w.ra, w.a)
    on_x = propose_trade(ba, ye, w.e)
    bb = offer_tildeling(w.rb, w.b)
    yd = _hold(_vagt(date(2042, 3, 27), VagtRegel.Kind.MORGEN, 1, 60), w.d)  # reconcile vacates tier A only
    as_y = propose_trade(bb, yd, w.d)
    Residency.objects.filter(resident__in=[w.a, w.d], year=2042, month=3).delete()  # a and d left
    result = reconcile_month(2042, 3)
    assert w.a in result.vacated and w.d in result.vacated
    assert not VagtTildeling.objects.filter(pk__in=[w.ra.pk, yd.pk]).exists()
    assert not VagtBytteForslag.objects.filter(pk__in=[on_x.pk, as_y.pk]).exists()
    assert VagtBytte.objects.get(pk=bb.pk).status == B.AABEN


def test_fridag_removes_proposals_and_notifies_the_proposer(w: World, pushes: list) -> None:
    for r in (w.a, w.c, w.d):
        _sub(r)
    ba = offer_tildeling(w.ra, w.a)
    f = propose_trade(ba, w.rc, w.c)  # Y sits on the aften shift that gets a fridag
    pushes.clear()
    result = declare_fridag(AV, [VagtRegel.Kind.AFTEN], "Test")
    assert w.c in [r for r, _v in result.removed]
    assert w.c.pk in [r.pk for r, _a, _m in result.notifications]  # the proposer is told too
    assert not VagtBytteForslag.objects.filter(pk=f.pk).exists()
    assert VagtBytte.objects.get(pk=ba.pk).status == B.AABEN
    # And the X side: a fridag on the offered row's day removes offer and proposals together.
    f2 = propose_trade(ba, w.rb, w.b)
    declare_fridag(M1, [VagtRegel.Kind.MORGEN], "Test")
    assert (
        not VagtBytte.objects.filter(pk=ba.pk).exists()
        and not VagtBytteForslag.objects.filter(pk=f2.pk).exists()
    )


# --------------------------------------------------------------------------------- notifications


def test_trade_notifications(
    w: World, pushes: list, monkeypatch: pytest.MonkeyPatch, clock: Callable
) -> None:
    for r in (w.a, w.b, w.e):
        _sub(r)
    ba = offer_tildeling(w.ra, w.a)
    f = propose_trade(ba, w.rb, w.b)
    assert len(pushes) == 1 and pushes[0][0] == [w.a.pk]  # the offerer only, never the proposer
    assert pushes[0][1]["body"] == (
        f"{w.b.full_name} foreslår at bytte din {w.m1} med {w.m2} — svar i app'en."
    )
    pushes.clear()
    accept_trade(f, w.a)
    assert len(pushes) == 1 and pushes[0][0] == [w.b.pk]  # the proposer only
    assert pushes[0][1]["body"] == (
        f"{w.a.full_name} har accepteret byttet: du har nu {w.m1} i stedet for {w.m2}."
    )
    pushes.clear()
    # Nothing on decline / withdraw / lapse / expiry.
    ye = _ye(w)
    bb = offer_tildeling(VagtTildeling.objects.get(pk=w.rb.pk), w.a)
    pushes.clear()
    f2 = propose_trade(bb, ye, w.e)
    pushes.clear()
    decline_proposal(f2, w.a)
    f3 = propose_trade(bb, ye, w.e)
    pushes.clear()
    withdraw_proposal(f3, w.e)
    f4 = propose_trade(bb, ye, w.e)
    pushes.clear()
    withdraw_offer(bb, w.a)  # lapses f4
    clock(date(2042, 3, 30))
    assert pushes == [] and VagtBytteForslag.objects.get(pk=f4.pk).status == F.BORTFALDET
    # Narrowed through allowed_subscribers: an offerer without access is not notified.
    clock(TODAY)
    monkeypatch.setattr(koekken_access, "ACCESS_ROLES", (Role.KOKKENGRUPPE,))
    bc = offer_tildeling(w.rc, w.c)
    pushes.clear()
    propose_trade(bc, VagtTildeling.objects.get(pk=ye.pk), w.e)
    assert all(recipients == [] for recipients, _payload in pushes)


# ------------------------------------------------------------------------------------- views


def _proposal_urls(html: str, name: str) -> bool:
    return f"/intern/koekken/{name}" in html


def test_trade_ui_buttons_follow_predicates(w: World, login: Callable) -> None:
    ye = _ye(w)
    ba = offer_tildeling(w.ra, w.a)
    html = _html(login(w.b))
    assert f"/intern/koekken/bytte/{ba.pk}/foreslaa" in html and "Foreslå bytte" in html
    sel = re.search(r'<select name="modydelse">(.*?)</select>', html, re.DOTALL)
    assert sel and f'value="{w.rb.pk}"' in sel.group(1) and f'value="{w.ra.pk}"' not in sel.group(1)
    # the offerer never sees the form; an outsider neither; d (who has rows) sees only valid options
    assert "foreslaa" not in _html(login(w.a))
    assert "foreslaa" not in _html(login(w.outsider))
    # e's select lists only ye (not another resident's row, not an offered row)
    html = _html(login(w.e))
    sel = re.search(r'<select name="modydelse">(.*?)</select>', html, re.DOTALL)
    assert sel and sel.group(1).count("<option") == 1 and f'value="{ye.pk}"' in sel.group(1)
    # a resident whose only candidate row has an open offer of its own gets no form
    offer_tildeling(ye, w.e)
    assert f"bytte/{ba.pk}/foreslaa" not in _html(login(w.e))
    # b proposes: the incoming list shows for a, with accept/decline URLs; b sees withdraw, not accept
    f = propose_trade(ba, w.rb, w.b)
    html = _html(login(w.a))
    assert "Forslag til dig" in html and "Acceptér bytte" in html and "Afvis" in html
    assert f"forslag/{f.pk}/accepter" in html and f"forslag/{f.pk}/afvis" in html
    assert f"Du får {w.m2} i stedet for {w.m1}. Det kan ikke fortrydes." in html
    assert f"{w.b.full_name} tilbyder {w.m2} (1 t)" in re.sub(r"\s+", " ", html)
    html = _html(login(w.b))
    assert f"forslag/{f.pk}/traek-tilbage" in html and "Træk forslag tilbage" in html
    assert f"forslag/{f.pk}/accepter" not in html and f"bytte/{ba.pk}/foreslaa" not in html
    # stale proposal (Y moved): the offerer still sees it, with "Afvis" only (cleanup), never "Acceptér";
    # the old proposer sees nothing
    VagtTildeling.objects.filter(pk=w.rb.pk).update(resident=w.e)
    stale_html = _html(login(w.a))
    assert f"forslag/{f.pk}/afvis" in stale_html and f"forslag/{f.pk}/accepter" not in stale_html
    assert f"forslag/{f.pk}/" not in _html(login(w.b))


def test_trade_post_flow_403_and_stale(w: World, login: Callable) -> None:
    base = "/intern/koekken"
    ba = offer_tildeling(w.ra, w.a)
    ca, cb, ce = login(w.a), login(w.b), login(w.e)
    assert cb.get(f"{base}/bytte/{ba.pk}/foreslaa").status_code == 405
    assert cb.post(f"{base}/bytte/{ba.pk}/foreslaa", {"modydelse": w.ra.pk}).status_code == 403  # not b's row
    assert (
        cb.post(f"{base}/bytte/{ba.pk}/foreslaa", {"modydelse": "x"}).status_code == 200
    )  # form error, inside
    resp = cb.post(f"{base}/bytte/{ba.pk}/foreslaa", {"modydelse": w.rb.pk})
    assert resp.status_code == 200 and 'id="koekken-bytte"' in resp.content.decode()
    f = VagtBytteForslag.objects.get()
    dup = cb.post(f"{base}/bytte/{ba.pk}/foreslaa", {"modydelse": w.rb.pk})
    assert dup.status_code == 200 and "allerede foreslået" in dup.content.decode()
    # wrong actors -> 403
    assert cb.post(f"{base}/forslag/{f.pk}/accepter").status_code == 403
    assert cb.post(f"{base}/forslag/{f.pk}/afvis").status_code == 403
    assert ca.post(f"{base}/forslag/{f.pk}/traek-tilbage").status_code == 403
    assert ce.post(f"{base}/forslag/{f.pk}/accepter").status_code == 403
    assert ca.get(f"{base}/forslag/{f.pk}/accepter").status_code == 405
    # accept via the view; second accept is a stale 200 with the error inside the partial
    assert ca.post(f"{base}/forslag/{f.pk}/accepter").status_code == 200
    w.ra.refresh_from_db()
    assert w.ra.resident == w.b
    stale = ca.post(f"{base}/forslag/{f.pk}/accepter")
    assert stale.status_code == 200 and "ikke længere åbent" in stale.content.decode()
    # withdraw / decline via the views
    bb = offer_tildeling(VagtTildeling.objects.get(pk=w.rb.pk), w.a)
    f2 = propose_trade(bb, VagtTildeling.objects.get(pk=w.ra.pk), w.b)
    assert cb.post(f"{base}/forslag/{f2.pk}/traek-tilbage").status_code == 200
    assert VagtBytteForslag.objects.get(pk=f2.pk).status == F.TRUKKET
    f3 = propose_trade(bb, VagtTildeling.objects.get(pk=w.ra.pk), w.b)
    assert ca.post(f"{base}/forslag/{f3.pk}/afvis").status_code == 200
    assert VagtBytteForslag.objects.get(pk=f3.pk).status == F.AFVIST
    # a proposal form open while the offer is taken meanwhile: a clean error in the partial
    bc = offer_tildeling(w.rc, w.c)
    take_over(bc, w.e)
    late = login(w.b).post(f"{base}/bytte/{bc.pk}/foreslaa", {"modydelse": w.ra.pk})
    assert late.status_code in (200, 403)


def test_foreslaa_with_a_row_that_moved_since_render_is_an_in_partial_error_not_403(
    w: World, login: Callable
) -> None:
    """Y was b's when the page rendered but was traded/taken away since: staleness, not authorization."""
    base = "/intern/koekken"
    ba = offer_tildeling(w.ra, w.a)
    bb = offer_tildeling(w.rb, w.b)
    take_over(bb, w.e)  # b hands rb to e after b's page was rendered
    resp = login(w.b).post(f"{base}/bytte/{ba.pk}/foreslaa", {"modydelse": w.rb.pk})
    assert resp.status_code == 200 and "ikke længere din" in resp.content.decode()
    # ... while a row that never was b's stays a 403
    assert login(w.b).post(f"{base}/bytte/{ba.pk}/foreslaa", {"modydelse": w.ra.pk}).status_code == 403
    assert not VagtBytteForslag.objects.exists()


def test_trade_partial_and_full_page_render_same_content(w: World, login: Callable) -> None:
    ba = offer_tildeling(w.ra, w.a)
    ye = _ye(w)
    propose_trade(ba, ye, w.e)
    ca = login(w.a)
    partial = ca.post(f"/intern/koekken/vagt/{w.rb.pk}/tilbyd").status_code  # a does not hold rb: 403
    assert partial == 403
    f = VagtBytteForslag.objects.get()
    partial_html = ca.post(f"/intern/koekken/forslag/{f.pk}/afvis").content.decode()
    full = _html(ca)

    def norm(x: str) -> str:
        x = re.sub(r'name="csrfmiddlewaretoken" value="[^"]*"', "", x)
        return re.sub(r"\s+", " ", x).strip()

    p = norm(partial_html)
    start = norm(full).index('<div id="koekken-bytte">')
    assert norm(full)[start : start + len(p)] == p


def test_byttet_med_labels_on_both_sides(w: World, login: Callable) -> None:
    ye = _ye(w)
    ba = offer_tildeling(w.ra, w.a)
    accept_trade(propose_trade(ba, ye, w.e), w.a)
    html_e = re.sub(r"\s+", " ", _html(login(w.e)))  # e now holds ra: the offered side
    assert f"byttet med {w.a.full_name}" in html_e
    html_a = re.sub(r"\s+", " ", _html(login(w.a)))  # a now holds ye: the proposed side
    assert f"byttet med {w.e.full_name}" in html_a


def test_index_query_count_does_not_grow_with_proposals(w: World, login: Callable) -> None:
    c = login(w.b)
    for m in (4, 5):
        _place(w.b, 2042, m)
        _place(w.a, 2042, m)

    def count() -> int:
        with CaptureQueriesContext(connection) as ctx:
            assert c.get("/intern/koekken/").status_code == 200
        return len(ctx)

    ba = offer_tildeling(w.ra, w.a)
    offer_tildeling(_hold(_vagt(date(2042, 4, 13), VagtRegel.Kind.FROKOST, 1, 60), w.c), w.c)
    propose_trade(ba, w.rb, w.b)
    # Baseline: one incoming proposal too (its batched pair lookup is one query, issued once any exists).
    base_in = _hold(_vagt(date(2042, 3, 9), VagtRegel.Kind.MORGEN, 1, 60), w.b)
    base_e = _hold(_vagt(date(2042, 3, 9), VagtRegel.Kind.FROKOST, 1, 60), w.e)
    propose_trade(offer_tildeling(base_in, w.b), base_e, w.e)
    c.get("/intern/koekken/")
    small = count()
    for i in range(6):
        d = date(2042, 3, 17) + timedelta(days=i)
        mine = _hold(_vagt(d, VagtRegel.Kind.MORGEN, 1, 60), w.b)
        theirs = _hold(_vagt(d + timedelta(days=30), VagtRegel.Kind.FROKOST, 1, 60), w.a)
        bo = offer_tildeling(theirs, w.a)
        propose_trade(bo, mine, w.b)
        incoming = _hold(_vagt(d, VagtRegel.Kind.AFTEN, 1, 60), w.e)
        mine_offered = _hold(_vagt(d, VagtRegel.Kind.FROKOST, 1, 60), w.b)
        mo = offer_tildeling(mine_offered, w.b)
        propose_trade(mo, incoming, w.e)
    assert count() == small


# ========================================================== Amendment 4, step 3: Den Hurtige post

LINK = "http://testserver/intern/koekken/"


def _share(row: VagtTildeling, by: Resident, link: str | None = LINK) -> VagtBytte:
    return offer_tildeling(row, by, hurtig_link=link)


def _archived(offer: VagtBytte) -> bool:
    """The offer's post still exists (archived, never deleted) and is expired as of now."""
    from core.clock import current_datetime
    from den_hurtige.models import QuickPost

    offer.refresh_from_db()
    assert offer.hurtig_post_id is not None
    post = QuickPost.objects.get(pk=offer.hurtig_post_id)
    return post.expires_at <= current_datetime()


def test_offer_with_link_posts_in_the_koekken_channel(w: World, clock: Callable) -> None:
    from core.clock import current_datetime
    from den_hurtige.models import QuickPost

    offer = _share(w.ra, w.a)
    post = QuickPost.objects.get()
    assert offer.hurtig_post_id == post.pk and VagtBytte.objects.get(pk=offer.pk).hurtig_post == post
    assert (post.channel, post.author) == ("koekken", w.a)
    assert post.content == (
        "Jeg kan ikke tage min morgenvagt onsdag 12. marts. Kan du? "
        "Tag den under Køkkenvagter: http://testserver/intern/koekken/"
    )
    # Shift is on 12 March, "now" is 1 March: now + 2 døgn is the earlier of the two.
    assert post.expires_at == current_datetime() + timedelta(minutes=2880)


def test_offer_for_a_shift_within_two_days_expires_at_the_shift_start(w: World, clock: Callable) -> None:
    from den_hurtige.models import QuickPost

    clock(date(2042, 3, 11))  # midnight the day before the morning shift
    _share(w.ra, w.a)
    assert QuickPost.objects.get().expires_at == timezone.make_aware(datetime(2042, 3, 12, 6, 0))


def test_offer_without_link_posts_nothing(w: World) -> None:
    from den_hurtige.models import QuickPost

    assert _share(w.ra, w.a, link=None).hurtig_post_id is None
    assert not QuickPost.objects.exists()


def test_post_content_stays_well_below_the_limit_with_a_long_absolute_url(w: World) -> None:
    from den_hurtige.services import MAX_CONTENT_CHARS

    long_link = "https://" + "a" * 100 + ".example.dk/intern/koekken/"
    offer = _share(w.rc, w.c, link=long_link)  # an aftenvagt: the longest kind name
    assert offer.hurtig_post is not None and len(offer.hurtig_post.content) < MAX_CONTENT_CHARS // 2


def test_post_text_renders_the_url_as_a_link(w: World) -> None:
    from core.links import linkify

    offer = _share(w.ra, w.a)
    assert offer.hurtig_post is not None
    html = linkify(offer.hurtig_post.content)
    assert 'href="http://testserver/intern/koekken/"' in html


def test_posting_failure_never_blocks_the_offer(
    w: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from den_hurtige import services as hurtig
    from den_hurtige.models import QuickPost

    def boom(*args: object, **kwargs: object) -> None:
        raise ValueError("nej")

    monkeypatch.setattr(hurtig, "publish_post", boom)
    with caplog.at_level("WARNING", logger="koekken.services"):
        offer = _share(w.ra, w.a)
    assert VagtBytte.objects.get(pk=offer.pk).status == B.AABEN and offer.hurtig_post_id is None
    assert not QuickPost.objects.exists()
    assert any("Den Hurtige" in r.getMessage() for r in caplog.records)


def test_a_database_failure_while_posting_rolls_back_only_the_post(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from den_hurtige import services as hurtig
    from den_hurtige.models import QuickPost

    real = hurtig.publish_post

    def create_then_fail(*args: object, **kwargs: object) -> None:
        real(*args, **kwargs)  # type: ignore[arg-type]
        raise IntegrityError("boom")

    monkeypatch.setattr(hurtig, "publish_post", create_then_fail)
    offer = _share(w.ra, w.a)
    assert VagtBytte.objects.get(pk=offer.pk).status == B.AABEN
    assert not QuickPost.objects.exists()  # the half-made post went with the savepoint


def _tilbyd(c: Client, row: VagtTildeling, **fields: str) -> str:
    resp = c.post(f"/intern/koekken/vagt/{row.pk}/tilbyd", fields)
    assert resp.status_code == 200
    return resp.content.decode()


def test_view_ticked_box_posts_with_an_absolute_link(w: World, login: Callable) -> None:
    from den_hurtige.models import QuickPost

    html = _html(login(w.a))
    assert 'name="del_i_den_hurtige"' in html and "Del i Den Hurtige" in html
    _tilbyd(login(w.a), w.ra, del_i_den_hurtige="1")
    post = QuickPost.objects.get()
    assert post.channel == "koekken" and post.content.endswith("http://testserver/intern/koekken/")
    assert VagtBytte.objects.get().hurtig_post == post


def test_view_unticked_box_posts_nothing(w: World, login: Callable) -> None:
    from den_hurtige.models import QuickPost

    _tilbyd(login(w.a), w.ra)
    assert not QuickPost.objects.exists() and VagtBytte.objects.get().hurtig_post_id is None


def test_view_with_den_hurtige_inaccessible_hides_the_box_and_ignores_a_forged_field(
    w: World, login: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    from den_hurtige import access as hurtig_access
    from den_hurtige.models import QuickPost

    monkeypatch.setattr(hurtig_access, "ACCESS_ROLES", (Role.ADMINISTRATOR,))
    assert "del_i_den_hurtige" not in _html(login(w.a))
    html = _tilbyd(login(w.a), w.ra, del_i_den_hurtige="1")  # forged
    assert "del_i_den_hurtige" not in html
    assert VagtBytte.objects.get().status == B.AABEN and not QuickPost.objects.exists()


def test_view_with_the_channel_restricted_hides_the_box(
    w: World, login: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    from den_hurtige import channels as hurtig_channels
    from den_hurtige.models import QuickPost

    restricted = dataclasses.replace(hurtig_channels.BY_SLUG["koekken"], roles=(Role.ADMINISTRATOR,))
    monkeypatch.setitem(hurtig_channels.BY_SLUG, "koekken", restricted)
    assert "del_i_den_hurtige" not in _html(login(w.a))
    _tilbyd(login(w.a), w.ra, del_i_den_hurtige="1")
    assert not QuickPost.objects.exists()


def test_open_offer_row_is_marked_as_shared(w: World, login: Callable) -> None:
    _share(w.ra, w.a)
    assert "delt i Den Hurtige" in _html(login(w.a))


# ------------------------------------------------------------------------------ archive on close


def _w_withdraw(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    o = _share(w.ra, w.a)
    withdraw_offer(o, w.a)
    return [o]


def _w_take(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    o = _share(w.ra, w.a)
    take_over(o, w.e)
    return [o]


def _w_take_whole(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    o = _share(w.rc, w.c)
    take_over_whole(o, w.d)
    return [o]


def _w_accept(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    o = _share(w.ra, w.a)
    accept_trade(propose_trade(o, w.rb, w.b), w.a)
    return [o]


def _w_accept_closes_ys_offer(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    """`_close_invalidated_offers`: Y's own offer, made after the proposal, lapses with the swap."""
    o = _share(w.ra, w.a)
    forslag = propose_trade(o, w.rb, w.b)
    y_offer = _share(w.rb, w.b)
    accept_trade(forslag, w.a)
    assert VagtBytte.objects.get(pk=y_offer.pk).status == B.BORTFALDET
    return [o, y_offer]


def _w_take_closes_other_offers(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    """`_close_invalidated_offers` called directly: an open offer on a touched row lapses."""
    o = _share(w.ra, w.a)
    with transaction.atomic():
        services._close_invalidated_offers([w.ra])
    assert VagtBytte.objects.get(pk=o.pk).status == B.BORTFALDET
    return [o]


def _w_stale_propose(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    o = _share(w.ra, w.a)
    VagtTildeling.objects.filter(pk=w.ra.pk).update(resident=w.e)  # the offerer no longer holds the row
    with pytest.raises(KoekkenAllocationError):
        propose_trade(o, w.rb, w.b)
    assert VagtBytte.objects.get(pk=o.pk).status == B.BORTFALDET
    return [o]


def _w_stale_take(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    o = _share(w.ra, w.a)
    VagtTildeling.objects.filter(pk=w.ra.pk).update(resident=w.e)
    with pytest.raises(KoekkenAllocationError):
        take_over(o, w.d)
    assert VagtBytte.objects.get(pk=o.pk).status == B.BORTFALDET
    return [o]


def _w_stale_accept(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    o = _share(w.ra, w.a)
    forslag = propose_trade(o, w.rb, w.b)
    VagtTildeling.objects.filter(pk=w.ra.pk).update(resident=w.e)
    with pytest.raises(KoekkenAllocationError):
        accept_trade(forslag, w.a)
    assert VagtBytte.objects.get(pk=o.pk).status == B.BORTFALDET
    return [o]


def _w_flag(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    o = _share(w.ra, w.a)
    flag_tildeling(w.ra, w.e, "x")
    return [o]


def _w_override_remove(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    o = _share(w.ra, w.a)
    mgr = make_resident(email="mgr-hurtig@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    assert login(mgr).post(f"/intern/koekken/gruppe/override/{w.ra.pk}/fjern").status_code == 302
    assert not VagtBytte.objects.filter(pk=o.pk).exists()
    return [o]


def _w_reconcile(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    o = _share(w.ra, w.a)
    Residency.objects.filter(resident=w.a, year=2042, month=3).delete()
    assert w.a in reconcile_month(2042, 3).vacated
    assert not VagtBytte.objects.filter(pk=o.pk).exists()
    return [o]


def _w_fridag(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    o = _share(w.rc, w.c)
    declare_fridag(AV, [VagtRegel.Kind.AFTEN], "Test")
    assert not VagtBytte.objects.filter(pk=o.pk).exists()
    return [o]


def _w_whole_shift_vacated_row_delete(w: World, login: Callable, make_resident: Callable) -> list[VagtBytte]:
    """The whole-shift collapse deletes the vacated row and cascades into every OTHER offer ever made on it.
    A closed offer's post is already archived, so revive it by hand to prove the receiver does it."""
    from core.clock import current_datetime
    from den_hurtige.models import QuickPost

    old = _share(w.rc, w.c)
    withdraw_offer(old, w.c)

    QuickPost.objects.filter(pk=old.hurtig_post_id).update(expires_at=current_datetime() + timedelta(days=1))
    current = _share(w.rc, w.c)
    take_over_whole(current, w.d)
    assert not VagtBytte.objects.filter(pk=old.pk).exists()
    return [old, current]


CLOSE_PATHS = [
    _w_withdraw,
    _w_take,
    _w_take_whole,
    _w_accept,
    _w_accept_closes_ys_offer,
    _w_take_closes_other_offers,
    _w_stale_propose,
    _w_stale_take,
    _w_stale_accept,
    _w_flag,
    _w_override_remove,
    _w_reconcile,
    _w_fridag,
    _w_whole_shift_vacated_row_delete,
]


@pytest.mark.parametrize("path", CLOSE_PATHS, ids=lambda f: f.__name__.removeprefix("_w_"))
def test_every_close_path_archives_the_post(
    path: Callable, w: World, login: Callable, make_resident: Callable
) -> None:
    from core.clock import current_datetime
    from den_hurtige.models import QuickPost

    path(w, login, make_resident)
    posts = list(QuickPost.objects.filter(channel="koekken"))
    assert posts
    for post in posts:  # archived, never deleted
        assert post.expires_at <= current_datetime(), path.__name__
        assert post.deleted_at is None


def test_force_rerun_cascade_archives_the_post(make_resident: Callable, clock: Callable) -> None:
    from core.clock import current_datetime
    from den_hurtige.models import QuickPost

    _periode, _people = _allocated(make_resident, clock)
    row = _tier_a_row()
    o = _share(row, row.resident)
    post_pk = o.hurtig_post_id
    allocate_month(2042, 3, force=True)
    assert not VagtBytte.objects.filter(pk=o.pk).exists()
    assert QuickPost.objects.get(pk=post_pk).expires_at <= current_datetime()


def test_archiving_skips_an_expired_post_but_archives_an_unexpired_soft_deleted_one(w: World) -> None:
    from core.clock import current_datetime
    from den_hurtige.models import QuickPost

    o = _share(w.ra, w.a)
    past = current_datetime() - timedelta(days=1)
    QuickPost.objects.filter(pk=o.hurtig_post_id).update(expires_at=past)
    withdraw_offer(o, w.a)
    assert QuickPost.objects.get(pk=o.hurtig_post_id).expires_at == past  # already archived: untouched

    o2 = _share(w.ra, w.a)
    now = current_datetime()
    future = now + timedelta(days=1)
    QuickPost.objects.filter(pk=o2.hurtig_post_id).update(deleted_at=now, expires_at=future)
    withdraw_offer(o2, w.a)
    # a tombstone that has not expired is archived along with its offer (harmless)
    assert QuickPost.objects.get(pk=o2.hurtig_post_id).expires_at <= current_datetime()


def test_author_hard_deleting_within_grace_nulls_the_link_and_a_later_close_is_harmless(w: World) -> None:
    from den_hurtige.models import QuickPost

    o = _share(w.ra, w.a)
    QuickPost.objects.get().delete()
    o.refresh_from_db()
    assert o.hurtig_post_id is None
    withdraw_offer(o, w.a)  # nothing to archive, nothing breaks
    take_over(_share(w.ra, w.a), w.e)
    assert VagtBytte.objects.filter(status=B.OVERTAGET).count() == 1


def test_reoffering_after_a_withdrawal_makes_a_new_post(w: World) -> None:
    from den_hurtige.models import QuickPost

    first = _share(w.ra, w.a)
    withdraw_offer(first, w.a)
    second = _share(w.ra, w.a)
    assert first.hurtig_post_id != second.hurtig_post_id and QuickPost.objects.count() == 2
    assert _archived(first) and not _archived(second)


# ------------------------------------------------------------------------------------- pushes


def test_sharing_pushes_once_through_den_hurtige_only(w: World, pushes: list) -> None:
    for r in (w.a, w.b, w.c, w.d):
        PushSubscription.objects.create(
            user=r,
            endpoint=f"https://example.test/hurtig/{r.pk}",
            auth="a",
            p256dh="p",
            wants_den_hurtige=True,
            wants_koekken=True,
        )
    from den_hurtige.models import ChannelMute

    ChannelMute.objects.create(resident=w.d, channel="koekken")
    _share(w.ra, w.a)
    assert len(pushes) == 1  # one Den Hurtige push; nothing on the koekken topic
    recipients, payload = pushes[0]
    assert recipients == sorted([w.b.pk, w.c.pk])  # author excluded, muter excluded
    assert payload["url"] == "/intern/den-hurtige/koekken/"
    assert payload["head"] == w.a.full_name


def test_a_rolled_back_offer_sends_no_push(
    w: World,
    monkeypatch: pytest.MonkeyPatch,
    settings: object,
    django_capture_on_commit_callbacks: Callable,
) -> None:
    from den_hurtige.models import QuickPost

    settings.VAPID_PUBLIC_KEY = settings.VAPID_PRIVATE_KEY = "k"  # type: ignore[attr-defined]
    settings.VAPID_ADMIN_EMAIL = "drift@gahk.dk"  # type: ignore[attr-defined]
    PushSubscription.objects.create(
        user=w.b, endpoint="https://example.test/b", auth="a", p256dh="p", wants_den_hurtige=True
    )
    with django_capture_on_commit_callbacks() as rolled_back:
        with pytest.raises(RuntimeError), transaction.atomic():
            _share(w.ra, w.a)
            raise RuntimeError
    assert rolled_back == [] and not QuickPost.objects.exists()
    with django_capture_on_commit_callbacks() as committed:
        _share(w.ra, w.a)
    assert len(committed) == 1  # control: a committed offer does queue the push
