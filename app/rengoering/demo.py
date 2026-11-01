"""Demo søndagsrengøring for `manage.py seed_demo`.

The areas and task lists from areas_data, and one rota over demo residents ("Demo") that started a
few Sundays ago, so every state is on screen on a fresh checkout without waiting for a Sunday:

  * past Sundays approved at once, approved after a follow-up, with mangler fixed and waiting for a
    re-check, past their deadline, and never signed off — so the overview, the history and its
    repeat-offender ordering all have something to show;
  * the latest Sunday half-walked: some areas decided, one with ticks in progress, some untouched;
  * `beboer@gahk.dk` with an open mangler on the latest Sunday, so the dashboard card and "Min
    rengøring" show the mangler, the deadline and the "udbedret" button straight away.

No real names: the actual E26 list is imported with `import_cleaning_rota` and stays out of git.
"""

import datetime
import random

from django.utils import timezone

from residents.models import Resident

from .models import (
    CleaningArea,
    CleaningAssignment,
    CleaningTask,
    Inspection,
    at_deadline_hour,
    default_fix_deadline,
)
from .services import load_areas, plan_rota, save_rota, visible_tasks

ROTA_NAME = "Demo"
WEEKS_BACK = 6  # Sundays already behind us
WEEKS = 26
PEOPLE_PER_AREA = 3
DEMO_LOGIN = "beboer@gahk.dk"
NOTES = [
    "Afløb i brusebadet er ikke tømt for hår.",
    "Planterne ser ikke ud til at være vandet.",
    "Der mangler toiletpapir.",
    "Pap-affaldet er ikke tømt.",
]


def seed(residents: list[Resident], now: datetime.datetime, rng: random.Random) -> None:
    load_areas()
    areas = list(CleaningArea.objects.filter(is_active=True))
    today = timezone.localdate(now)
    last_sunday = today - datetime.timedelta(days=(today.weekday() + 1) % 7)
    start = last_sunday - datetime.timedelta(weeks=WEEKS_BACK - 1)

    pool = [r for r in residents if not r.is_kitchen and r.email != DEMO_LOGIN]
    rng.shuffle(pool)
    rotations: dict[CleaningArea, list[Resident | None]] = {}
    for i, area in enumerate(areas):
        rotations[area] = list(pool[i * PEOPLE_PER_AREA : (i + 1) * PEOPLE_PER_AREA])
    # The demo login takes the slot that falls on the latest Sunday in the first area.
    demo = next((r for r in residents if r.email == DEMO_LOGIN), None)
    if demo and areas and rotations[areas[0]]:
        rotations[areas[0]][(WEEKS_BACK - 1) % PEOPLE_PER_AREA] = demo

    rota = save_rota(ROTA_NAME, plan_rota(start, WEEKS, 5, rotations), None)
    inspector = next((r for r in residents if r.is_superuser), None)

    for a in rota.assignments.filter(date__lte=last_sunday).select_related("area", "resident"):
        tasks = list(visible_tasks(a))
        evening = at_deadline_hour(a.date) + datetime.timedelta(minutes=rng.randint(5, 50))
        latest = a.date == last_sunday
        if a.resident == demo and latest:
            _sign_off(a, evening - datetime.timedelta(hours=2))
            _decide(
                a,
                inspector,
                tasks,
                Inspection.Outcome.MANGLER,
                evening,
                rng,
                deadline=now + datetime.timedelta(hours=20),
            )
            continue
        roll = rng.random()
        if roll < 0.1:
            # never signed off; inspected anyway and found wanting (only for weeks gone by)
            if not latest:
                _decide(a, inspector, tasks, Inspection.Outcome.MANGLER, evening, rng)
            continue
        _sign_off(a, evening - datetime.timedelta(hours=rng.randint(1, 5)))
        if latest and roll > 0.7:
            continue  # the walk has not reached this one yet
        if latest and roll > 0.6:
            Inspection.objects.create(assignment=a, inspector=inspector).checked_tasks.set(
                tasks[: len(tasks) // 2]
            )
            continue
        if roll < 0.7:
            _decide(a, inspector, tasks, Inspection.Outcome.GODKENDT, evening, rng)
            continue
        first = _decide(a, inspector, tasks, Inspection.Outcome.MANGLER, evening, rng)
        if roll < 0.85 and not latest:
            fixed = evening + datetime.timedelta(hours=18)  # before the 22.00 deadline the next day
            first.fixed_at = fixed
            first.save(update_fields=["fixed_at"])
            _decide(
                a,
                inspector,
                tasks,
                Inspection.Outcome.GODKENDT,
                fixed + datetime.timedelta(hours=1),
                rng,
                round_=2,
            )
        elif roll < 0.92:
            first.fixed_at = evening + datetime.timedelta(hours=18)
            first.save(update_fields=["fixed_at"])  # waiting for the re-check
        # else: never fixed — past its deadline (overskredet)


def _sign_off(a: CleaningAssignment, when: datetime.datetime) -> None:
    a.signed_off_at = when
    a.signed_off_by = a.resident
    a.save(update_fields=["signed_off_at", "signed_off_by"])


def _decide(
    a: CleaningAssignment,
    inspector: Resident | None,
    tasks: list[CleaningTask],
    outcome: str,
    when: datetime.datetime,
    rng: random.Random,
    deadline: datetime.datetime | None = None,
    round_: int = 1,
) -> Inspection:
    missing = (
        rng.sample(tasks, k=min(len(tasks), rng.randint(1, 3)))
        if outcome == Inspection.Outcome.MANGLER
        else []
    )
    i = Inspection.objects.create(
        assignment=a,
        round=round_,
        inspector=inspector,
        outcome=outcome,
        note=rng.choice(NOTES) if missing else "",
        missing=[{"zone": t.zone, "text": t.text} for t in missing],
        started_at=when,
        decided_at=when,
        deadline=(deadline or default_fix_deadline(when)) if missing else None,
    )
    i.checked_tasks.set([t for t in tasks if t not in missing])
    return i
