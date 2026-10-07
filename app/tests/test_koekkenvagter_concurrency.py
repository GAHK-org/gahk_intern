"""Køkkenvagter Amendment 4 -- the force re-run versus a concurrent take-over, against REAL row locks.

These need a real Postgres and real threads (`transaction=True`: each thread has its own connection and
the locks are only meaningful across committed transactions). `serialized_rollback` restores what the
end-of-test flush truncates (migration-seeded `VagtRegel`), and `_reseed` guards the rest. If a
`--reuse-db` database is ever left corrupted by a transactional test, run once with `--create-db`.
The session-level restore of the serialized post-migrate state is a global fixture in tests/conftest.py.

The hazard (reviewer's reproduction): Django splits a cascading `.delete()` into an unlocked SELECT
(where the hand-off exclusion is evaluated) and separate DELETEs by pk list. A take-over committing
between the two was deleted anyway. `_delete_replaceable_tildelinger` now locks the candidate rows first,
so a concurrent take-over must BLOCK until the re-run finishes.
"""

import re
import threading
from collections.abc import Callable, Iterator
from datetime import date, time
from typing import Any

import pytest
from django.db import connection, transaction
from django.test import Client

from core.clock import clear_cache
from core.models import DevClock, Room
from koekken.models import Vagt, VagtBytte, VagtBytteForslag, VagtRegel, VagtTildeling
from koekken.services import (
    KoekkenAllocationError,
    _delete_replaceable_tildelinger,
    accept_trade,
    allocate_month,
    claim_vagt,
    declare_fridag,
    flag_tildeling,
    offer_tildeling,
    propose_trade,
    reconcile_month,
    resolve_periode,
    take_over,
    take_over_whole,
    withdraw_offer,
)
from residents.models import Residency, Resident, Role

pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

T = VagtTildeling.Status
B = VagtBytte.Status
F = VagtBytteForslag.Status
YEAR, MONTH = 2042, 3
DAYS = [date(2042, 3, 10), date(2042, 3, 11)]  # Monday, Tuesday: morgen, 1 x 60
JOIN = 15.0  # seconds; a hung thread (deadlock) fails the test instead of hanging the suite


def _reseed() -> None:
    for kind, weekend, minutes, headcount, start in [
        ("morgen", False, 60, 1, time(6, 0)),
        ("morgen", True, 60, 1, time(6, 0)),
        ("frokost", False, 60, 1, time(12, 0)),
        ("frokost", True, 60, 1, time(12, 0)),
        ("aften", False, 180, 2, time(17, 0)),
        ("aften", True, 120, 1, time(17, 0)),
    ]:
        VagtRegel.objects.get_or_create(
            kind=kind,
            weekend=weekend,
            defaults={"duration_minutes": minutes, "headcount": headcount, "start_time": start},
        )


@pytest.fixture
def world(settings: object, make_resident: Callable[..., Resident]) -> Iterator[dict[str, Any]]:
    settings.DEBUG = True  # type: ignore[attr-defined]
    _reseed()
    DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": date(2042, 3, 1)})
    clear_cache()
    people = [make_resident(email=f"{n}@gahk.dk", first_name=n.upper()) for n in "abcd"]
    for i, r in enumerate(people):
        room = Room.objects.create(legacy_index=100 + i, number=100 + i, floor="stuen", side="mod gaden")
        Residency.objects.create(resident=r, room=room, year=YEAR, month=MONTH)
    for d in DAYS:
        Vagt.objects.create(
            periode=resolve_periode(d), date=d, kind=VagtRegel.Kind.MORGEN, headcount=1, duration_minutes=60
        )
    allocate_month(YEAR, MONTH)
    row = VagtTildeling.objects.order_by("pk").first()
    assert row is not None
    taker = next(r for r in people if r.pk != row.resident_id)
    yield {"row": row, "offerer": row.resident, "taker": taker}
    clear_cache()


def _run_in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["result"] = fn()
        except BaseException as exc:  # the test inspects whatever came out
            outcome["error"] = exc
        finally:
            connection.close()  # this thread's own connection

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


def _paused_rerun(
    pause_before: "re.Pattern[str]",
) -> tuple[threading.Event, threading.Event, threading.Thread]:
    """Run `allocate_month(force=True)` in a thread, paused just before the first statement matching
    `pause_before`. Returns `(paused, resume, thread)`."""
    paused, resume = threading.Event(), threading.Event()
    state = {"done": False}

    def wrapper(execute: Callable, sql: str, params: object, many: bool, context: object) -> object:
        if not state["done"] and pause_before.search(sql):
            state["done"] = True
            paused.set()
            assert resume.wait(JOIN), "test never resumed the paused re-run"
        return execute(sql, params, many, context)

    def rerun() -> None:
        with connection.execute_wrapper(wrapper):
            allocate_month(YEAR, MONTH, force=True)

    thread, _outcome = _run_in_thread(rerun)
    thread.outcome = _outcome  # type: ignore[attr-defined]
    return paused, resume, thread


def test_take_over_blocks_while_rerun_holds_the_rows_then_gets_a_clean_refusal(world: dict[str, Any]) -> None:
    """Re-run paused AFTER its candidate rows are locked, right before its first DELETE (the window in
    which the unlocked SELECT used to be stale). The take-over must not get through: it blocks on the
    row lock, then -- the plain row was legitimately replaced -- refuses cleanly. No lost hand-off, no
    deadlock, no unhandled error."""
    bytte = offer_tildeling(world["row"], world["offerer"])
    paused, resume, rerun = _paused_rerun(re.compile(r'^DELETE FROM "koekken_'))
    assert paused.wait(JOIN)

    taker_thread, taker = _run_in_thread(
        lambda: take_over(VagtBytte.objects.get(pk=bytte.pk), world["taker"])
    )
    taker_thread.join(1.5)
    assert taker_thread.is_alive(), "take_over was not blocked by the re-run's row locks (race is open)"
    assert VagtTildeling.objects.filter(pk=world["row"].pk).exists()  # re-run has not committed yet

    resume.set()
    rerun.join(JOIN)
    taker_thread.join(JOIN)
    assert not rerun.is_alive() and not taker_thread.is_alive(), "deadlock"
    assert "error" not in rerun.outcome  # type: ignore[attr-defined]
    assert isinstance(taker.get("error"), KoekkenAllocationError), taker  # clean Danish refusal
    assert "findes ikke" in str(taker["error"])
    # Nothing was handed off, so nothing may claim it was.
    assert not VagtBytte.objects.filter(status__in=[B.OVERTAGET, B.OVERTAGET_HEL]).exists()


def test_handoff_committed_before_the_rerun_locks_survives_with_its_offer(world: dict[str, Any]) -> None:
    """Re-run paused BEFORE it takes its locks; a take-over completes in the gap. The re-run's lock
    query and the exclusion that follows must see the committed hand-off: the row survives, moved to
    the taker, with its `OVERTAGET` offer intact."""
    bytte = offer_tildeling(world["row"], world["offerer"])
    paused, resume, rerun = _paused_rerun(re.compile(r"FOR UPDATE"))
    assert paused.wait(JOIN)

    taker_thread, taker = _run_in_thread(
        lambda: take_over(VagtBytte.objects.get(pk=bytte.pk), world["taker"])
    )
    taker_thread.join(JOIN)
    assert not taker_thread.is_alive() and "error" not in taker, taker  # no lock held yet: it just commits

    resume.set()
    rerun.join(JOIN)
    assert not rerun.is_alive() and "error" not in rerun.outcome  # type: ignore[attr-defined]
    survivor = VagtTildeling.objects.get(pk=world["row"].pk)
    assert survivor.resident == world["taker"]
    offer = VagtBytte.objects.get(pk=bytte.pk)
    assert offer.status == B.OVERTAGET and offer.overtaget_af == world["taker"]


def _paused_take_over(
    bytte: VagtBytte, taker: Resident, pause_after: "re.Pattern[str]"
) -> tuple[threading.Event, threading.Event, threading.Thread, dict[str, Any]]:
    """Run `take_over` in a thread, paused just AFTER the first statement matching `pause_after` has run
    (so the transaction is open and holds whatever that statement locked). Returns
    `(paused, resume, thread, outcome)`."""
    paused, resume = threading.Event(), threading.Event()
    state = {"done": False}

    def wrapper(execute: Callable, sql: str, params: object, many: bool, context: object) -> object:
        result = execute(sql, params, many, context)
        if not state["done"] and pause_after.search(sql):
            state["done"] = True
            paused.set()
            assert resume.wait(JOIN), "test never resumed the paused take_over"
        return result

    def run() -> VagtBytte:
        with connection.execute_wrapper(wrapper):
            return take_over(VagtBytte.objects.get(pk=bytte.pk), taker)

    thread, outcome = _run_in_thread(run)
    return paused, resume, thread, outcome


def test_rerun_blocks_on_an_uncommitted_take_over_then_excludes_the_handed_off_row(
    world: dict[str, Any],
) -> None:
    """The in-flight interleaving: a take-over has already UPDATEd the row (uncommitted) when the re-run's
    row-lock pass starts. The re-run must block on that row, and once the take-over commits, re-evaluate
    and exclude the now-handed-off row: the hand-off survives."""
    bytte = offer_tildeling(world["row"], world["offerer"])
    paused, resume, taker_thread, taker = _paused_take_over(
        bytte, world["taker"], re.compile(r'^UPDATE "koekken_vagttildeling"')
    )
    assert paused.wait(JOIN)

    _paused, _resume, rerun = _paused_rerun(re.compile(r"(?!)"))  # a pattern that never matches: no pause
    rerun.join(1.5)
    assert rerun.is_alive(), "the re-run was not blocked by the uncommitted take-over's row lock"

    resume.set()
    taker_thread.join(JOIN)
    rerun.join(JOIN)
    assert not taker_thread.is_alive() and not rerun.is_alive(), "deadlock"
    assert "error" not in taker, taker
    assert "error" not in rerun.outcome  # type: ignore[attr-defined]
    survivor = VagtTildeling.objects.get(pk=world["row"].pk)
    assert survivor.resident == world["taker"]
    offer = VagtBytte.objects.get(pk=bytte.pk)
    assert offer.status == B.OVERTAGET and offer.overtaget_af == world["taker"]


def test_override_remove_and_a_concurrent_take_over_do_not_deadlock(
    world: dict[str, Any], make_resident: Callable[..., Resident]
) -> None:
    """`override_remove` used to delete (cascading into the offer) before locking the row, the reverse of
    `take_over`'s order. With the take-over holding the row lock, the removal must now BLOCK on the row
    (not take the offer lock first), and run cleanly once the take-over commits."""
    manager = make_resident(email="manager@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    bytte = offer_tildeling(world["row"], world["offerer"])
    client = Client()
    client.force_login(manager)
    paused, resume, taker_thread, taker = _paused_take_over(bytte, world["taker"], re.compile(r"FOR UPDATE"))
    assert paused.wait(JOIN)  # take_over holds the row lock, has not yet asked for the offer

    remover, removal = _run_in_thread(
        lambda: client.post(f"/intern/koekken/gruppe/override/{world['row'].pk}/fjern")
    )
    remover.join(1.5)
    assert remover.is_alive(), "override_remove was not blocked by the take-over's row lock"

    resume.set()
    taker_thread.join(JOIN)
    remover.join(JOIN)
    assert not taker_thread.is_alive() and not remover.is_alive(), "deadlock"
    assert "error" not in taker, taker
    assert "error" not in removal, removal
    assert removal["result"].status_code == 302
    # The take-over won the race and the (still TILDELT) row was then removed: nothing half-done.
    assert not VagtTildeling.objects.filter(pk=world["row"].pk).exists()
    assert not VagtBytte.objects.filter(pk=bytte.pk).exists()  # cascaded with its row


# ======================================================================= Amendment 4, step 2: trading
#
# The lock order is rows (ascending pk) -> Vagt -> offers -> proposals. These tests drive real threads
# against real row locks: each pauses one side while it HOLDS its locks, starts the other side, proves it
# is blocked, resumes, and checks the end state and that nothing deadlocked.


@pytest.fixture
def tw(settings: object, make_resident: Callable[..., Resident]) -> Iterator[dict[str, Any]]:
    """Hand-built world (no allocator): a, b, c, d on March and April's lists. `yl` is b's April row and has
    the LOWEST pk; then March rows `ra` (a), `rc` (c), `rb` (b), `rd` (d), each on its own morgen vagt."""
    settings.DEBUG = True  # type: ignore[attr-defined]
    _reseed()
    DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": date(2042, 3, 1)})
    clear_cache()
    people = {n: make_resident(email=f"{n}@gahk.dk", first_name=n.upper()) for n in "abcd"}
    for i, r in enumerate(people.values()):
        room = Room.objects.create(legacy_index=200 + i, number=200 + i, floor="stuen", side="mod gaden")
        for month in (3, 4):
            Residency.objects.create(resident=r, room=room, year=YEAR, month=month)

    def row(d: date, who: str) -> VagtTildeling:
        vagt = Vagt.objects.create(
            periode=resolve_periode(d), date=d, kind=VagtRegel.Kind.MORGEN, headcount=1, duration_minutes=60
        )
        return VagtTildeling.objects.create(vagt=vagt, resident=people[who], status=T.TILDELT)

    world: dict[str, Any] = {"people": people}
    world["yl"] = row(date(2042, 4, 7), "b")
    world["ra"] = row(date(2042, 3, 10), "a")
    world["rc"] = row(date(2042, 3, 11), "c")
    world["rb"] = row(date(2042, 3, 12), "b")
    world["rd"] = row(date(2042, 3, 13), "d")
    yield world
    clear_cache()


def _paused(
    fn: Callable[[], Any], pattern: "re.Pattern[str]", *, nth: int = 1, after: bool = True
) -> tuple[threading.Event, threading.Event, threading.Thread, dict[str, Any]]:
    """Run `fn` in a thread, paused at the `nth` statement matching `pattern` (just after it has run when
    `after`, so the locks it takes are held; just before it otherwise). `(paused, resume, thread, outcome)`."""
    paused, resume = threading.Event(), threading.Event()
    seen = {"n": 0}

    def wrapper(execute: Callable, sql: str, params: object, many: bool, context: object) -> object:
        hit = False
        if pattern.search(sql):
            seen["n"] += 1
            hit = seen["n"] == nth
        if hit and not after:
            paused.set()
            assert resume.wait(JOIN), "test never resumed the paused call"
        result = execute(sql, params, many, context)
        if hit and after:
            paused.set()
            assert resume.wait(JOIN), "test never resumed the paused call"
        return result

    def run() -> object:
        with connection.execute_wrapper(wrapper):
            return fn()

    thread, outcome = _run_in_thread(run)
    return paused, resume, thread, outcome


FOR_UPDATE = re.compile(r"FOR UPDATE")


def _propose(world: dict[str, Any], x: str, who: str, y: str) -> tuple[VagtBytte, VagtBytteForslag]:
    row = world[x]
    bytte = VagtBytte.objects.filter(tildeling=row, status=B.AABEN).first() or offer_tildeling(
        row, row.resident
    )
    return bytte, propose_trade(bytte, world[y], world["people"][who])


def _joined(*threads: threading.Thread) -> None:
    for t in threads:
        t.join(JOIN)
    assert not any(t.is_alive() for t in threads), "deadlock"


def test_propose_blocks_on_force_rerun_then_gets_a_clean_refusal(tw: dict[str, Any]) -> None:
    """Re-run paused AFTER locking the month's rows (right before its first DELETE). A `propose_trade` on
    an X in that month must BLOCK on X's row lock (it takes Y first, which is free) instead of inserting a
    proposal beneath the deleter; after the re-run commits it gets a clean refusal, and nothing dangles."""
    bytte = offer_tildeling(tw["ra"], tw["people"]["a"])
    paused, resume, rerun = _paused_rerun(re.compile(r'^DELETE FROM "koekken_'))
    assert paused.wait(JOIN)

    proposer, outcome = _run_in_thread(lambda: propose_trade(bytte, tw["yl"], tw["people"]["b"]))
    proposer.join(1.5)
    assert proposer.is_alive(), "propose_trade was not blocked by the re-run's row locks (race is open)"
    assert not VagtBytteForslag.objects.exists()

    resume.set()
    _joined(rerun, proposer)
    assert "error" not in rerun.outcome  # type: ignore[attr-defined]
    assert isinstance(outcome.get("error"), KoekkenAllocationError), outcome
    assert "findes ikke" in str(outcome["error"])
    assert not VagtBytteForslag.objects.exists()


def test_rerun_blocks_on_accept_then_both_sides_survive(tw: dict[str, Any]) -> None:
    """accept_trade paused after taking its locks; a force re-run of the month must block; once the swap
    commits, the re-run's exclusion sees BOTH rows as completed hand-offs (X via the BYTTET offer, Y via the
    ACCEPTERET proposal) and deletes neither."""
    a, b = tw["people"]["a"], tw["people"]["b"]
    _bytte, forslag = _propose(tw, "ra", "b", "rb")
    paused, resume, acceptor, accepted = _paused(lambda: accept_trade(forslag, a), FOR_UPDATE)
    assert paused.wait(JOIN)

    _p, _r, rerun = _paused_rerun(re.compile(r"(?!)"))  # never pauses
    rerun.join(1.5)
    assert rerun.is_alive(), "the re-run was not blocked by the accept's row locks"

    resume.set()
    _joined(acceptor, rerun)
    assert "error" not in accepted, accepted
    assert "error" not in rerun.outcome  # type: ignore[attr-defined]
    ra, rb = VagtTildeling.objects.get(pk=tw["ra"].pk), VagtTildeling.objects.get(pk=tw["rb"].pk)
    assert (ra.resident, rb.resident) == (b, a)
    assert VagtBytte.objects.get(tildeling=ra).status == B.BYTTET
    assert VagtBytteForslag.objects.get(modydelse=rb).status == F.ACCEPTERET


def test_accept_with_lower_pk_y_in_another_month_does_not_deadlock_with_the_rerun(tw: dict[str, Any]) -> None:
    """The re-run holds X's lock (March). accept_trade wants Y (April, LOWER pk) then X. It takes Y, blocks
    on X, and -- the re-run never asks for Y -- there is no cycle. Once the re-run has replaced X the accept
    gets a clean refusal."""
    a = tw["people"]["a"]
    _bytte, forslag = _propose(tw, "ra", "b", "yl")
    assert tw["yl"].pk < tw["ra"].pk
    paused, resume, rerun = _paused_rerun(re.compile(r'^DELETE FROM "koekken_'))
    assert paused.wait(JOIN)

    acceptor, accepted = _run_in_thread(lambda: accept_trade(forslag, a))
    acceptor.join(1.5)
    assert acceptor.is_alive(), "accept_trade was not blocked by the re-run's lock on X"

    resume.set()
    _joined(rerun, acceptor)
    assert "error" not in rerun.outcome  # type: ignore[attr-defined]
    assert isinstance(accepted.get("error"), KoekkenAllocationError), accepted
    assert VagtTildeling.objects.get(pk=tw["yl"].pk).resident == tw["people"]["b"]  # Y never moved


@pytest.mark.parametrize("natural", [False, True])
def test_two_accepts_sharing_y_exactly_one_wins(tw: dict[str, Any], natural: bool) -> None:
    """Two offerers each hold a proposal naming b's row Y. Their locks {X1, Y} and {X2, Y} overlap and are
    taken in ascending pk order, so they serialise: exactly one swap happens, the other proposal is
    lapsed, and both calls return."""
    a, c = tw["people"]["a"], tw["people"]["c"]
    _b1, f1 = _propose(tw, "ra", "b", "rb")
    _b2, f2 = _propose(tw, "rc", "b", "rb")
    if natural:
        barrier = threading.Barrier(2)

        def go(f: VagtBytteForslag, who: Resident) -> VagtBytteForslag:
            barrier.wait(JOIN)
            return accept_trade(f, who)

        t1, o1 = _run_in_thread(lambda: go(f1, a))
        t2, o2 = _run_in_thread(lambda: go(f2, c))
        _joined(t1, t2)
    else:
        paused, resume, t1, o1 = _paused(lambda: accept_trade(f1, a), FOR_UPDATE)
        assert paused.wait(JOIN)
        t2, o2 = _run_in_thread(lambda: accept_trade(f2, c))
        t2.join(1.5)
        assert t2.is_alive(), "the second accept was not blocked on the shared Y"
        resume.set()
        _joined(t1, t2)
    wins = [o for o in (o1, o2) if "result" in o]
    losses = [o for o in (o1, o2) if "error" in o]
    assert len(wins) == 1 and len(losses) == 1, (o1, o2)
    assert isinstance(losses[0]["error"], KoekkenAllocationError), losses
    statuses = sorted(VagtBytteForslag.objects.values_list("status", flat=True))
    assert statuses == sorted([F.ACCEPTERET, F.BORTFALDET])
    assert VagtBytte.objects.filter(status=B.BYTTET).count() == 1
    holders = sorted(
        VagtTildeling.objects.filter(pk=tw["rb"].pk).values_list("resident__first_name", flat=True)
    )
    assert holders in (["A"], ["C"])  # b's Y went to exactly one of the offerers


@pytest.mark.parametrize("accept_first", [True, False])
def test_accept_versus_take_over_of_ys_own_offer(tw: dict[str, Any], accept_first: bool) -> None:
    """Y has an open offer of its own (made after the proposal). accept_trade and take_over of THAT offer
    both lock Y first: one wins, the other gets a clean refusal, nobody deadlocks."""
    a, d = tw["people"]["a"], tw["people"]["d"]
    _bytte, forslag = _propose(tw, "ra", "b", "rb")
    y_offer = offer_tildeling(tw["rb"], tw["people"]["b"])
    accept = lambda: accept_trade(forslag, a)  # noqa: E731
    take = lambda: take_over(y_offer, d)  # noqa: E731
    first, second = (accept, take) if accept_first else (take, accept)
    paused, resume, t1, o1 = _paused(first, FOR_UPDATE)
    assert paused.wait(JOIN)
    t2, o2 = _run_in_thread(second)
    t2.join(1.5)
    assert t2.is_alive(), "the second call was not blocked by the first one's row locks"
    resume.set()
    _joined(t1, t2)
    assert "result" in o1, o1
    assert isinstance(o2.get("error"), KoekkenAllocationError), o2
    rb, ra = VagtTildeling.objects.get(pk=tw["rb"].pk), VagtTildeling.objects.get(pk=tw["ra"].pk)
    if accept_first:
        assert (ra.resident, rb.resident) == (tw["people"]["b"], a)
        assert VagtBytte.objects.get(pk=y_offer.pk).status == B.BORTFALDET
    else:
        assert (ra.resident, rb.resident) == (a, d)
        assert VagtBytteForslag.objects.get(pk=forslag.pk).status == F.BORTFALDET


@pytest.mark.parametrize("accept_first", [True, False])
def test_accept_versus_override_remove_of_y(
    tw: dict[str, Any], make_resident: Callable[..., Resident], accept_first: bool
) -> None:
    manager = make_resident(email="manager@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    client = Client()
    client.force_login(manager)
    a = tw["people"]["a"]
    _bytte, forslag = _propose(tw, "ra", "b", "rb")
    accept = lambda: accept_trade(forslag, a)  # noqa: E731
    remove = lambda: client.post(f"/intern/koekken/gruppe/override/{tw['rb'].pk}/fjern")  # noqa: E731
    first, second = (accept, remove) if accept_first else (remove, accept)
    paused, resume, t1, o1 = _paused(first, FOR_UPDATE)
    assert paused.wait(JOIN)
    t2, o2 = _run_in_thread(second)
    t2.join(1.5)
    assert t2.is_alive(), "the second call was not blocked by the first one's row locks"
    resume.set()
    _joined(t1, t2)
    assert "error" not in o1, o1
    assert not VagtTildeling.objects.filter(pk=tw["rb"].pk).exists()  # Y was removed either way
    ra = VagtTildeling.objects.get(pk=tw["ra"].pk)
    if accept_first:  # the swap committed, THEN the (now a's) row was removed
        assert "error" not in o2 and ra.resident == tw["people"]["b"]
        assert VagtBytte.objects.get(tildeling=ra).status == B.BYTTET
    else:  # the removal won; the accept refuses cleanly and X did not move
        assert isinstance(o2.get("error"), KoekkenAllocationError), o2
        assert ra.resident == a and VagtBytte.objects.get(tildeling=ra).status == B.AABEN
    assert not VagtBytteForslag.objects.filter(status=F.AABEN).exists()


@pytest.mark.parametrize("accept_first", [True, False])
def test_accept_versus_withdraw_of_the_same_offer(tw: dict[str, Any], accept_first: bool) -> None:
    a = tw["people"]["a"]
    bytte, forslag = _propose(tw, "ra", "b", "rb")
    accept = lambda: accept_trade(forslag, a)  # noqa: E731
    withdraw = lambda: withdraw_offer(bytte, a)  # noqa: E731
    first, second = (accept, withdraw) if accept_first else (withdraw, accept)
    paused, resume, t1, o1 = _paused(first, FOR_UPDATE)
    assert paused.wait(JOIN)
    t2, o2 = _run_in_thread(second)
    t2.join(1.5)
    assert t2.is_alive(), "the second call was not blocked by the first one's row locks"
    resume.set()
    _joined(t1, t2)
    assert "result" in o1, o1
    assert isinstance(o2.get("error"), KoekkenAllocationError), o2
    final = VagtBytte.objects.get(pk=bytte.pk).status
    assert final == (B.BYTTET if accept_first else B.TRUKKET)
    assert VagtBytteForslag.objects.get(pk=forslag.pk).status == (
        F.ACCEPTERET if accept_first else F.BORTFALDET
    )


def test_accept_and_withdraw_of_another_offer_both_closing_the_same_proposal(tw: dict[str, Any]) -> None:
    """accept_trade (offer 1, Y = rb) lapses every other proposal naming rb, among them F, which sits on
    offer 2. withdraw_offer(offer 2) lapses F as well. Both must reach F through lock level 4 in ascending
    order: the withdraw waits for the accept, then finds F already closed. No deadlock."""
    a, c = tw["people"]["a"], tw["people"]["c"]
    _b1, f_main = _propose(tw, "ra", "b", "rb")
    b2, f_shared = _propose(tw, "rc", "b", "rb")
    # Pause the accept after its LAST up-front lock (tildelinger, offers, proposals = 3 statements).
    paused, resume, t1, o1 = _paused(lambda: accept_trade(f_main, a), FOR_UPDATE, nth=3)
    assert paused.wait(JOIN)
    t2, o2 = _run_in_thread(lambda: withdraw_offer(b2, c))
    t2.join(1.5)
    assert t2.is_alive(), "withdraw_offer was not blocked (it must wait for the proposal lock)"
    resume.set()
    _joined(t1, t2)
    assert "result" in o1 and "result" in o2, (o1, o2)
    assert VagtBytteForslag.objects.get(pk=f_shared.pk).status == F.BORTFALDET
    assert VagtBytte.objects.get(pk=b2.pk).status == B.TRUKKET
    assert VagtBytteForslag.objects.get(pk=f_main.pk).status == F.ACCEPTERET


# ============================================ step 2, round 3: a level's full set locked in ONE ascending pass
#
# Reviewer's reproductions. Every proposal-touching writer must lock its WHOLE level-4 set in one ascending
# statement, exactly like `accept_trade`. The crossing setup (pks P_b < Q < P_a):
#   a offers ra (B2); c proposes rc for B2 (P_b); c ALSO offers rc (B); b proposes rb for B2 (Q) and for B (P_a).
# T1 touches rc, B, P_a and P_b; T2 = accept_trade(Q) locks {P_b, Q, P_a}. Two separate lock statements in T1
# (first P_a, then P_b) cross T2's single ascending pass: T1 holds P_a, wants P_b; T2 holds P_b, wants P_a.

FORSLAG_LOCK = re.compile(r'^SELECT .*FROM "koekken_vagtbytteforslag".* FOR UPDATE')
FORSLAG_DELETE = re.compile(r'^DELETE FROM "koekken_vagtbytteforslag"')


def _crossing_setup(tw: dict[str, Any]) -> VagtBytteForslag:
    """Build the crossing proposals above; returns Q (the one `accept_trade` is called on)."""
    _propose(tw, "ra", "c", "rc")  # B2 and P_b
    offer_tildeling(tw["rc"], tw["people"]["c"])  # B: rc is also c's modydelse on B2 (allowed)
    b2 = VagtBytte.objects.get(tildeling=tw["ra"], status=B.AABEN)
    q = propose_trade(b2, tw["rb"], tw["people"]["b"])
    bc = VagtBytte.objects.get(tildeling=tw["rc"], status=B.AABEN)
    propose_trade(bc, tw["rb"], tw["people"]["b"])  # P_a
    return q


def _crossing_run(
    tw: dict[str, Any], t1: Callable[[], Any], pattern: "re.Pattern[str]", q: VagtBytteForslag
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pause T1 right after the first statement matching `pattern` (holding whatever it locked so far), run
    `accept_trade(q)` against it, resume, and prove neither side deadlocked and the accept went through."""
    a = tw["people"]["a"]
    paused, resume, thread1, out1 = _paused(t1, pattern)
    assert paused.wait(JOIN)
    thread2, out2 = _run_in_thread(lambda: accept_trade(q, a))
    thread2.join(1.5)  # give an out-of-order T1 time to let T2 get into the crossing position
    resume.set()
    _joined(thread1, thread2)
    assert "error" not in out1, out1
    assert "result" in out2, out2
    assert VagtBytteForslag.objects.get(pk=q.pk).status == F.ACCEPTERET
    assert VagtTildeling.objects.get(pk=tw["ra"].pk).resident == tw["people"]["b"]
    return out1, out2


def test_take_over_vs_accept_trade_crossing_proposals_do_not_deadlock(tw: dict[str, Any]) -> None:
    q = _crossing_setup(tw)
    bc = VagtBytte.objects.get(tildeling=tw["rc"], status=B.AABEN)
    _crossing_run(tw, lambda: take_over(bc, tw["people"]["d"]), FORSLAG_LOCK, q)
    assert VagtTildeling.objects.get(pk=tw["rc"].pk).resident == tw["people"]["d"]
    assert not VagtBytteForslag.objects.filter(status=F.AABEN).exists()


def test_flag_tildeling_vs_accept_trade_crossing_proposals_do_not_deadlock(tw: dict[str, Any]) -> None:
    q = _crossing_setup(tw)
    _crossing_run(tw, lambda: flag_tildeling(tw["rc"], tw["people"]["d"], "test"), FORSLAG_LOCK, q)
    assert VagtTildeling.objects.get(pk=tw["rc"].pk).status == T.ANMELDT
    assert not VagtBytteForslag.objects.filter(status=F.AABEN).exists()


def test_take_over_whole_vs_accept_trade_crossing_proposals_do_not_deadlock(tw: dict[str, Any]) -> None:
    """Whole-shift variant: c offers its aften place (B), d -- the partner -- has proposed its own aften row
    for B2 (p2 < Q < p1). `take_over_whole` used to lock the offer's proposals (p1), then those using the
    partner row (p2), then those using the vacated row, in three statements."""
    c, d = tw["people"]["c"], tw["people"]["d"]
    aften = Vagt.objects.create(
        periode=resolve_periode(date(2042, 3, 14)),
        date=date(2042, 3, 14),
        kind=VagtRegel.Kind.AFTEN,
        headcount=2,
        duration_minutes=180,
    )
    c_row = VagtTildeling.objects.create(vagt=aften, resident=c, status=T.TILDELT)
    d_row = VagtTildeling.objects.create(vagt=aften, resident=d, status=T.TILDELT)
    b2 = offer_tildeling(tw["ra"], tw["people"]["a"])
    propose_trade(b2, d_row, d)  # p2
    q = propose_trade(b2, tw["rb"], tw["people"]["b"])
    bc = offer_tildeling(c_row, c)
    p1 = propose_trade(bc, tw["rb"], tw["people"]["b"])
    _crossing_run(tw, lambda: take_over_whole(bc, d), FORSLAG_LOCK, q)
    assert VagtBytte.objects.get(pk=bc.pk).status == B.OVERTAGET_HEL
    assert VagtBytteForslag.objects.get(pk=p1.pk).status == F.BORTFALDET
    assert not VagtBytteForslag.objects.filter(status=F.AABEN).exists()


def test_override_remove_vs_accept_trade_crossing_proposals_do_not_deadlock(
    tw: dict[str, Any], make_resident: Callable[..., Resident]
) -> None:
    """The cascade fast-deletes proposals in two statements (by offer, by `modydelse`); unlocked it crossed
    `accept_trade`'s single ascending pass. Paused after the first proposal DELETE."""
    manager = make_resident(email="manager@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    client = Client()
    client.force_login(manager)
    q = _crossing_setup(tw)
    _crossing_run(
        tw, lambda: client.post(f"/intern/koekken/gruppe/override/{tw['rc'].pk}/fjern"), FORSLAG_DELETE, q
    )
    assert not VagtTildeling.objects.filter(pk=tw["rc"].pk).exists()
    assert not VagtBytteForslag.objects.filter(status=F.AABEN).exists()


def test_reconcile_month_vs_accept_trade_crossing_proposals_do_not_deadlock(tw: dict[str, Any]) -> None:
    """c is dropped from March's real Residency list, so `reconcile_month` vacates rc (and cascades)."""
    q = _crossing_setup(tw)
    Residency.objects.filter(resident=tw["people"]["c"], year=YEAR, month=3).delete()
    _crossing_run(tw, lambda: reconcile_month(YEAR, 3), FORSLAG_DELETE, q)
    assert not VagtTildeling.objects.filter(pk=tw["rc"].pk).exists()


def test_declare_fridag_vs_accept_trade_crossing_proposals_do_not_deadlock(tw: dict[str, Any]) -> None:
    q = _crossing_setup(tw)
    _crossing_run(tw, lambda: declare_fridag(date(2042, 3, 11), [VagtRegel.Kind.MORGEN]), FORSLAG_DELETE, q)
    assert not VagtTildeling.objects.filter(pk=tw["rc"].pk).exists()


def test_allocation_delete_vs_accept_trade_crossing_proposals_do_not_deadlock(tw: dict[str, Any]) -> None:
    """`_delete_replaceable_tildelinger` (the allocation delete) over the shifts of rc."""
    q = _crossing_setup(tw)
    vagter = list(Vagt.objects.filter(pk=tw["rc"].vagt_id))

    def delete() -> None:
        with transaction.atomic():  # the helper's callers (the allocators) always hold a transaction
            _delete_replaceable_tildelinger(vagter)

    _crossing_run(tw, delete, FORSLAG_DELETE, q)
    assert not VagtTildeling.objects.filter(pk=tw["rc"].pk).exists()


@pytest.mark.parametrize("variant", ["take_over", "flag_tildeling", "override_remove"])
def test_take_over_whole_historic_offer_cascade_does_not_deadlock(
    tw: dict[str, Any], make_resident: Callable[..., Resident], variant: str
) -> None:
    """Reviewer's 4th deadlock. Deleting the vacated row V also cascades into V's OTHER (historic) offers and
    their proposals. c offers V, b proposes rb (F_old), c withdraws (F_old lapses), c offers V again, b
    proposes rb again (F2 > F_old). `take_over_whole` paused right after its level-4 lock used to hold F2
    only, then reach F_old via the cascade; a writer on rb locks {F_old, F2} in one ascending pass."""
    a, b, c, d = (tw["people"][n] for n in "abcd")
    aften = Vagt.objects.create(
        periode=resolve_periode(date(2042, 3, 14)),
        date=date(2042, 3, 14),
        kind=VagtRegel.Kind.AFTEN,
        headcount=2,
        duration_minutes=180,
    )
    v = VagtTildeling.objects.create(vagt=aften, resident=c, status=T.TILDELT)
    VagtTildeling.objects.create(vagt=aften, resident=d, status=T.TILDELT)
    old_offer = offer_tildeling(v, c)
    f_old = propose_trade(old_offer, tw["rb"], b)
    withdraw_offer(old_offer, c)
    assert VagtBytteForslag.objects.get(pk=f_old.pk).status == F.BORTFALDET
    new_offer = offer_tildeling(v, c)
    f2 = propose_trade(new_offer, tw["rb"], b)
    assert f_old.pk < f2.pk
    rb_offer = offer_tildeling(tw["rb"], b) if variant == "take_over" else None

    t2_call: Callable[[], Any]
    if variant == "take_over":
        assert rb_offer is not None
        t2_call = lambda: take_over(rb_offer, a)  # noqa: E731
    elif variant == "flag_tildeling":
        t2_call = lambda: flag_tildeling(tw["rb"], d, "test")  # noqa: E731
    else:
        manager = make_resident(email="manager@gahk.dk", roles=(Role.KOKKENGRUPPE,))
        client = Client()
        client.force_login(manager)
        t2_call = lambda: client.post(f"/intern/koekken/gruppe/override/{tw['rb'].pk}/fjern")  # noqa: E731

    paused, resume, t1, o1 = _paused(lambda: take_over_whole(new_offer, d), FORSLAG_LOCK)
    assert paused.wait(JOIN)
    t2, o2 = _run_in_thread(t2_call)
    t2.join(1.5)
    resume.set()
    _joined(t1, t2)
    assert "error" not in o1, o1
    assert VagtBytte.objects.get(pk=new_offer.pk).status == B.OVERTAGET_HEL
    assert not VagtBytte.objects.filter(pk=old_offer.pk).exists()  # cascaded with V
    assert not VagtBytteForslag.objects.filter(status=F.AABEN).exists()
    # T2 either completed cleanly or was refused cleanly; never a deadlock or an unhandled error.
    error = o2.get("error")
    assert error is None or isinstance(error, KoekkenAllocationError), o2


# ======================================================================= P3 step 3: summer claiming
#
# `claim_vagt` and the `override_assign` view insert through `_insert_tildeling_locked`: lock the Vagt,
# re-count, insert. It takes no assignment-row lock, so it never goes back up the LOCK ORDER. Each test
# pauses one side while it HOLDS the Vagt lock (just after the `FOR UPDATE` statement), starts the other
# side and proves it blocks, then resumes and checks the end state.

SUMMER_DAY = date(2042, 7, 8)  # a Tuesday: aftenvagt has two places


@pytest.fixture
def sw(settings: object, make_resident: Callable[..., Resident]) -> Iterator[dict[str, Any]]:
    settings.DEBUG = True  # type: ignore[attr-defined]
    _reseed()
    DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": date(2042, 6, 1)})
    clear_cache()
    people = {n: make_resident(email=f"{n}@gahk.dk", first_name=n.upper()) for n in "abcd"}
    for i, r in enumerate(people.values()):
        room = Room.objects.create(legacy_index=300 + i, number=300 + i, floor="stuen", side="mod gaden")
        Residency.objects.create(resident=r, room=room, year=2042, month=7)
    periode = resolve_periode(SUMMER_DAY)
    morgen = Vagt.objects.create(
        periode=periode, date=SUMMER_DAY, kind=VagtRegel.Kind.MORGEN, headcount=1, duration_minutes=60
    )
    aften = Vagt.objects.create(
        periode=periode, date=SUMMER_DAY, kind=VagtRegel.Kind.AFTEN, headcount=2, duration_minutes=180
    )
    yield {"people": people, "morgen": morgen, "aften": aften}
    clear_cache()


VAGT_LOCK = re.compile(r'FROM "koekken_vagt" .*FOR UPDATE')


def test_two_claims_on_the_last_place_exactly_one_wins(sw: dict[str, Any]) -> None:
    p, vagt = sw["people"], sw["morgen"]
    paused, resume, first, first_out = _paused(lambda: claim_vagt(p["a"], vagt), VAGT_LOCK)
    assert paused.wait(JOIN)  # a holds the Vagt lock and has not counted yet
    second, second_out = _run_in_thread(lambda: claim_vagt(p["b"], vagt))
    second.join(1.5)
    assert second.is_alive(), "the second claim was not blocked by the Vagt lock (race is open)"
    resume.set()
    _joined(first, second)
    assert "error" not in first_out, first_out
    assert isinstance(second_out.get("error"), KoekkenAllocationError), second_out
    assert "ingen ledige pladser" in str(second_out["error"])
    assert list(VagtTildeling.objects.filter(vagt=vagt).values_list("resident", flat=True)) == [p["a"].pk]


def test_claim_versus_override_assign_on_the_last_place_exactly_one_row(
    sw: dict[str, Any], make_resident: Callable[..., Resident]
) -> None:
    p, vagt = sw["people"], sw["morgen"]
    manager = make_resident(email="manager@gahk.dk", roles=(Role.KOKKENGRUPPE,))
    client = Client()
    client.force_login(manager)
    paused, resume, claimer, claim_out = _paused(lambda: claim_vagt(p["a"], vagt), VAGT_LOCK)
    assert paused.wait(JOIN)
    override, override_out = _run_in_thread(
        lambda: client.post(
            "/intern/koekken/gruppe/override", {"vagt": vagt.pk, "resident": p["b"].pk}, follow=True
        )
    )
    override.join(1.5)
    assert override.is_alive(), "override_assign was not blocked by the claim's Vagt lock"
    resume.set()
    _joined(claimer, override)
    assert "error" not in claim_out and "error" not in override_out, (claim_out, override_out)
    assert VagtTildeling.objects.filter(vagt=vagt).count() == 1  # never overfilled
    messages_text = " ".join(str(m) for m in override_out["result"].context["messages"])
    assert "allerede fuld besætning" in messages_text


def test_claim_versus_take_over_whole_does_not_deadlock(sw: dict[str, Any]) -> None:
    """The claim holds the Vagt lock (and never an assignment-row lock); the whole-shift take-over holds its
    rows and then asks for the Vagt. It must simply wait for the claim, which is refused (shift full)."""
    p, aften = sw["people"], sw["aften"]
    row_a = VagtTildeling.objects.create(vagt=aften, resident=p["a"], status=T.TILDELT)
    VagtTildeling.objects.create(vagt=aften, resident=p["b"], status=T.TILDELT)
    bytte = offer_tildeling(row_a, p["a"])
    paused, resume, claimer, claim_out = _paused(lambda: claim_vagt(p["c"], aften), VAGT_LOCK)
    assert paused.wait(JOIN)
    taker, take_out = _run_in_thread(lambda: take_over_whole(VagtBytte.objects.get(pk=bytte.pk), p["b"]))
    taker.join(1.5)
    assert taker.is_alive(), "take_over_whole was not blocked on the Vagt the claim holds"
    resume.set()
    _joined(claimer, taker)
    assert isinstance(claim_out.get("error"), KoekkenAllocationError), claim_out
    assert "error" not in take_out, take_out
    aften.refresh_from_db()
    assert VagtTildeling.objects.filter(vagt=aften).count() <= aften.headcount  # consistent final state
    assert not VagtTildeling.objects.filter(vagt=aften, resident=p["c"]).exists()


def test_claim_committing_while_a_fridag_is_declared_never_hangs_or_half_applies(sw: dict[str, Any]) -> None:
    """declare_fridag paused after its row locks and its cascade collection, right before its first DELETE;
    a claim on the same date commits in the gap (claim takes no row locks, so it is not blocked). Either the
    fridag's commit fails on the deferred foreign key (full rollback, `IntegrityError` the management
    command turns into "re-run"), or it commits and has removed everything. Never a hang, never a half."""
    from django.db import IntegrityError

    p, aften = sw["people"], sw["aften"]
    VagtTildeling.objects.create(vagt=aften, resident=p["a"], status=T.TILDELT)

    def declare() -> object:
        with transaction.atomic():
            return declare_fridag(SUMMER_DAY, [VagtRegel.Kind.AFTEN])

    paused, resume, declarer, declare_out = _paused(
        declare, re.compile(r'^DELETE FROM "koekken_vagttildeling"'), after=False
    )
    assert paused.wait(JOIN)
    claimer, claim_out = _run_in_thread(lambda: claim_vagt(p["c"], aften))
    claimer.join(JOIN)
    assert not claimer.is_alive(), "the claim hung behind declare_fridag"
    resume.set()
    _joined(declarer)

    from koekken.models import Fridag

    if "error" in declare_out:
        assert isinstance(declare_out["error"], IntegrityError), declare_out
        assert "error" not in claim_out
        assert Vagt.objects.filter(pk=aften.pk).exists() and not Fridag.objects.exists()
        assert set(VagtTildeling.objects.filter(vagt=aften).values_list("resident", flat=True)) == {
            p["a"].pk,
            p["c"].pk,
        }
    else:
        assert not Vagt.objects.filter(pk=aften.pk).exists() and Fridag.objects.exists()
        assert not VagtTildeling.objects.filter(vagt_id=aften.pk).exists()  # no orphan row
        assert isinstance(claim_out.get("error"), KoekkenAllocationError) or "result" in claim_out
