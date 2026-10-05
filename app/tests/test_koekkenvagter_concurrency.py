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
from django.db import connection
from django.test import Client

from core.clock import clear_cache
from core.models import DevClock, Room
from koekken.models import Vagt, VagtBytte, VagtBytteForslag, VagtRegel, VagtTildeling
from koekken.services import (
    KoekkenAllocationError,
    accept_trade,
    allocate_month,
    offer_tildeling,
    propose_trade,
    resolve_periode,
    take_over,
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
