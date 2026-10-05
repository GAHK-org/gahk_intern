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
from koekken.models import Vagt, VagtBytte, VagtRegel, VagtTildeling
from koekken.services import (
    KoekkenAllocationError,
    allocate_month,
    offer_tildeling,
    resolve_periode,
    take_over,
)
from residents.models import Residency, Resident, Role

pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

T = VagtTildeling.Status
B = VagtBytte.Status
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
