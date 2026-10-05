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
    VagtRegel,
    VagtTildeling,
)
from koekken.services import (
    KoekkenAllocationError,
    allocate_month,
    allocate_tier_a,
    allocate_tier_b,
    can_offer,
    can_take,
    can_take_whole,
    declare_fridag,
    flag_tildeling,
    force_rerun_impact,
    generate_vagter,
    has_started,
    mark_udfoert,
    offer_tildeling,
    open_offers,
    post_obligation,
    projected_balance_for,
    reconcile_month,
    resolve_anmeldelse,
    resolve_periode,
    take_over,
    take_over_whole,
    withdraw_offer,
)
from residents.models import Residency, Resident, Role

pytestmark = pytest.mark.django_db

T = VagtTildeling.Status
B = VagtBytte.Status
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
    """Offer `row`, flag it (same day, allowed), dismiss the flag: back to TILDELT, offer still AABEN."""
    bytte = offer_tildeling(row, offerer)
    flag_tildeling(row, w.e, "x")
    resolve_anmeldelse(row.anmeldelser.get(), upheld=False, resolved_by=w.b)
    row.refresh_from_db()
    assert row.status == T.TILDELT
    assert VagtBytte.objects.get(pk=bytte.pk).status == B.AABEN
    return bytte


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
    hel = VagtBytte.objects.get(status=B.OVERTAGET_HEL)
    assert (hel.tildeling.vagt.headcount, hel.tildeling.vagt.duration_minutes) == (1, 360)
