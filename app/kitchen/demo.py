"""Demo køkkenvagter for `manage.py seed_demo`.

Three months of history and the calendar's three months ahead, written through the feature's own
services (apply_monthly_charge, complete_shift, mark_absent, manual_entry, enroll, disable_shift,
set_bonus), so every ledger row is one the real rules would write. The ledger's timestamps are then
moved back to when each thing happened, so a statement reads as a history rather than as everything
booked the moment the seed ran.

Each resident gets a PROFILE: where their balance is today, how readily they took the shifts behind
them, and what they have booked ahead. The profiles exist so that the overview has something in
every cell on a fresh checkout:

  * every zone today: green, yellow, red and under -6;
  * every combination of the overview's two verdicts: fine in 30 days and at the end of the
    calendar (flittig, stabil), fine in 30 days only (indhenter: catching up, nothing booked later),
    fine at the end only (planlægger: booked far ahead, nothing inside the binding 30 days), and
    neither (sløset, alarm).

A balance is hit exactly: the history is planned first, and each resident's statement opens with
the difference as "Overført saldo fra papirlisten" — which is also how the real feature will start.

States made visible on purpose, because each would otherwise take a developer several steps and
some waiting to construct:

  * statements with the opening balance, monthly charges, shifts (including a lone holder given
    both spots of an evening), an absence with the default fine and one with a reduced fine, a
    trade with extra points, and manual entries by Køkkengruppen;
  * on the plan: a free spot within 24 hours (red) and one within 72 hours (light red), a lone
    holder of a two-spot evening, a resident holding both spots of one (the other spot can be taken
    over), shifts for sale (one with extra points, one more than a week out, so it was never
    announced), a shift with bonus points, special shifts with several spots, cancelled shifts (the
    Christmas closure and a kitchen being painted);
  * beboer@ with a fresh sign-up still inside the regret window, and a shift of theirs for sale;
  * the history pages: completed shifts, unmanned ones and a cancelled one.

Shifts are put up for sale by setting the fields rather than through services.put_for_sale, which
would notify every resident: notifications.demo is a fixed picture its own test counts on.
"""

import datetime
import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.db.models import Max
from django.utils import timezone

from core.clock import current_datetime
from residents.models import Residency, Resident, Role, active_period

from . import services
from .models import KitchenAssignment, KitchenMonthlyState, KitchenPointEntry, KitchenShift

Kind = KitchenShift.Kind


@dataclass(frozen=True)
class Profile:
    balance: tuple[int, int]  # today's balance, picked in this range
    diligence: float  # relative chance of being the one who took a past shift
    # Book shifts until the forecast reaches this balance OUTLOOK_DAYS ahead / at the end of the
    # calendar. None: book nothing in that stretch.
    soon: int | None
    end: int | None


PROFILES = {
    "flittig": Profile((5, 12), 4, soon=3, end=2),
    "stabil": Profile((0, 4), 2, soon=0, end=0),
    "indhenter": Profile((-6, -2), 2, soon=0, end=None),
    "planlaegger": Profile((-4, -1), 2, soon=None, end=0),
    "sloeset": Profile((-5, -1), 1, soon=None, end=None),
    "alarm": Profile((-12, -7), 0.5, soon=None, end=None),
}
# The rest of the house, after the first few have covered every profile once: about a third in
# minus and a few far under, which fills the overview's low-balance list without making the house
# look as if it has given up.
WEIGHTS = {
    "flittig": 0.25,
    "stabil": 0.37,
    "indhenter": 0.12,
    "planlaegger": 0.1,
    "sloeset": 0.1,
    "alarm": 0.06,
}

# The documented logins, so each shows something definite: beboer@ is catching up (red now, fine in
# 30 days), the two kitchen logins can see the overview from either side of its rights.
LOGINS = {
    "admin@gahk.dk": ("stabil", 2),
    "formand@gahk.dk": ("flittig", 9),
    "ak@gahk.dk": ("planlaegger", -2),
    "oel@gahk.dk": ("sloeset", -4),
    "regnskab@gahk.dk": ("alarm", -8),
    "beboer@gahk.dk": ("indhenter", -3),
    "koekken@gahk.dk": ("flittig", 6),
    "inspektion@gahk.dk": ("stabil", 1),
}

OPENING = "Overført saldo fra papirlisten"


def _local(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)))


class _Ledger:
    """Runs services in time order and moves the entries each one wrote back to when it happened."""

    def __init__(self) -> None:
        self.last_id = KitchenPointEntry.objects.aggregate(m=Max("id"))["m"] or 0

    def at(self, moment: datetime.datetime) -> None:
        written = KitchenPointEntry.objects.filter(pk__gt=self.last_id)
        last = written.aggregate(m=Max("id"))["m"]
        if last:
            written.update(created_at=moment)
            self.last_id = last


def seed(residents: list[Resident], now: datetime.datetime, rng: random.Random) -> int:
    """Create the demo shifts and statements. Returns the number of ledger entries written.

    `now` is replaced by core.clock's: every rule here reads that clock, so a dev clock set to a
    later date gives a seed that agrees with the pages instead of shifts "tomorrow" that started.
    """
    if len(residents) < 8:
        return 0
    now = current_datetime()
    today = timezone.localdate(now)
    year, month = active_period()
    start = services.add_months(datetime.date(year, month, 1), -2)
    months = [services.add_months(start, i) for i in range(3)]
    by_email = {r.email: r for r in residents}
    officer = (
        by_email.get("koekken@gahk.dk")
        or Resident.objects.filter(role_assignments__role=Role.KOKKENGRUPPE).first()
        or residents[0]
    )

    living: dict[tuple[int, int], set[int]] = defaultdict(set)
    for resident_id, y, m in Residency.objects.filter(resident__in=residents).values_list(
        "resident_id", "year", "month"
    ):
        living[(y, m)].add(resident_id)

    profile = _profiles(residents, by_email, rng)
    target = {
        r.pk: LOGINS[r.email][1] if r.email in LOGINS else rng.randint(*PROFILES[profile[r.pk]].balance)
        for r in residents
    }

    services.ensure_calendar()
    services.create_standard_shifts(start, today)
    _special_shifts(today, start, officer)

    # ---- the history --------------------------------------------------------------------------
    past = list(KitchenShift.objects.filter(ends_at__lte=now, date__gte=start).order_by("starts_at", "kind"))
    cancelled = next(
        (s for s in past if s.kind == Kind.MIDDAY and s.date == today - datetime.timedelta(days=20)), None
    )
    if cancelled:
        # disable_shift refuses a started shift, as it should; history is written directly.
        cancelled.is_disabled, cancelled.disabled_reason = True, "Vandskade i køkkenet"
        cancelled.save(update_fields=["is_disabled", "disabled_reason"])
    holders = _plan_history(past, residents, profile, living, rng, now)
    absences = _plan_absences(past, holders, residents, profile, now)
    trade = _plan_trade(past, holders, residents, rng)
    manual = [
        (
            _local(start + datetime.timedelta(days=24), 11),
            residents[3],
            2,
            "Hjalp med at gøre køkkenet rent efter vandskaden",
        ),
        (
            _local(start + datetime.timedelta(days=41), 9),
            residents[-2],
            -1,
            "Korrektion: vagten var registreret to gange",
        ),
    ]

    # Each resident's history adds up to `delta`; the opening balance makes up the rest.
    delta: dict[int, int] = defaultdict(int)
    for y, m in [(d.year, d.month) for d in months]:
        for resident_id in living[(y, m)]:
            delta[resident_id] -= services.MONTHLY_CHARGE
    for shift in past:
        if shift.is_disabled:
            continue
        held = holders.get(shift.pk, [])
        for resident, spots in held:
            awarded = (shift.spots if len(held) == 1 else spots) * shift.points_per_spot_total
            delta[resident.pk] += awarded
            if (shift.pk, resident.pk) in absences:
                fine = absences[(shift.pk, resident.pk)]
                delta[resident.pk] -= awarded + (awarded if fine is None else fine)
    if trade:
        shift, seller, buyer = trade
        delta[seller.pk] -= 2
        delta[buyer.pk] += 2
    for _at, resident, amount, _message in manual:
        delta[resident.pk] += amount

    for shift in past:
        for resident, spots in holders.get(shift.pk, []):
            booked = shift.starts_at - datetime.timedelta(days=rng.randint(2, 25), hours=rng.randint(0, 10))
            assignment = KitchenAssignment.objects.create(
                shift=shift, resident=resident, spots=spots, self_enrolled_at=booked
            )
            KitchenAssignment.objects.filter(pk=assignment.pk).update(created_at=booked)

    ledger = _Ledger()
    # (when, tie-break, what, payload): a charge before a shift ending the same minute, an absence
    # after the shift it is about.
    events: list[tuple[datetime.datetime, int, str, Any]] = []
    events += [(_local(d, 4, 15), 0, "charge", d) for d in months]
    events += [(s.ends_at, 1, "complete", s) for s in past if not s.is_disabled]
    events += [(at, 2, "manual", (resident, amount, message)) for at, resident, amount, message in manual]
    if trade:
        events.append((trade[0].starts_at - datetime.timedelta(days=2), 2, "trade", trade))
    for (shift_id, resident_id), fine in absences.items():
        shift = next(s for s in past if s.pk == shift_id)
        at = min(shift.ends_at + datetime.timedelta(hours=16), now - datetime.timedelta(minutes=30))
        events.append((at, 3, "absence", (shift, resident_id, fine)))
    events.sort(key=lambda e: (e[0], e[1]))

    opened = _local(start, 0) - datetime.timedelta(hours=1)
    for resident in residents:
        amount = target[resident.pk] - delta[resident.pk]
        if amount:
            services.post_entry(
                resident.pk, amount, OPENING, KitchenPointEntry.Kind.MANUAL, created_by=officer
            )
    ledger.at(opened)

    for at, _order, kind, payload in events:
        if kind == "charge":
            services.apply_monthly_charge(payload.year, payload.month)
        elif kind == "complete":
            services.complete_shift(payload)
            KitchenShift.objects.filter(pk=payload.pk).update(completed_at=payload.ends_at)
        elif kind == "manual":
            resident, amount, message = payload
            services.manual_entry(resident, amount, message, officer)
        elif kind == "trade":
            shift, seller, buyer = payload
            label = f"{shift.title} {timezone.localtime(shift.starts_at):%d.%m.%Y}"
            services.post_entry(
                seller.pk, -2, f"Ekstra point for salg af {label}", KitchenPointEntry.Kind.TRADE, shift=shift
            )
            services.post_entry(
                buyer.pk, 2, f"Ekstra point for køb af {label}", KitchenPointEntry.Kind.TRADE, shift=shift
            )
        elif kind == "absence":
            shift, resident_id, fine = payload
            assignment = KitchenAssignment.objects.get(shift=shift, resident_id=resident_id)
            services.mark_absent(assignment, fine, officer)
        ledger.at(at)

    state = KitchenMonthlyState.get()
    state.year, state.month = year, month
    state.save(update_fields=["year", "month"])

    # ---- the plan ahead -----------------------------------------------------------------------
    _plan_ahead(residents, by_email, profile, now, rng)
    return KitchenPointEntry.objects.filter(resident__in=residents).count()


def _profiles(residents: list[Resident], by_email: dict[str, Resident], rng: random.Random) -> dict[int, str]:
    names = list(PROFILES)
    others = [r for r in residents if r.email not in LOGINS]
    result = {by_email[email].pk: name for email, (name, _balance) in LOGINS.items() if email in by_email}
    for i, resident in enumerate(others):
        result[resident.pk] = (
            names[i] if i < len(names) else rng.choices(list(WEIGHTS), weights=list(WEIGHTS.values()))[0]
        )
    return result


def _special_shifts(today: datetime.date, start: datetime.date, officer: Resident) -> None:
    horizon = services.calendar_horizon()
    specials = [
        (start + datetime.timedelta(days=30), 10, 14, 6, 4, "Rusfest – oprydning"),
        (today + datetime.timedelta(days=10), 15, 19, 4, 3, "Fællesspisning – madlavning"),
        (min(datetime.date(today.year, 12, 12), horizon), 21, 23, 6, 3, "Julefrokost – opvask"),
    ]
    for day, begins, ends, spots, points, title in specials:
        KitchenShift.objects.create(
            kind=Kind.SPECIAL,
            date=day,
            starts_at=_local(day, begins),
            ends_at=_local(day, ends),
            spots=spots,
            points_per_spot=points,
            description=title,
            created_by=officer,
        )


def _weighted(
    pool: list[Resident], profile: dict[int, str], rng: random.Random, exclude: set[int]
) -> Resident | None:
    candidates = [r for r in pool if r.pk not in exclude]
    if not candidates:
        return None
    weights = [PROFILES[profile[r.pk]].diligence for r in candidates]
    return rng.choices(candidates, weights=weights)[0]


def _plan_history(
    past: list[KitchenShift],
    residents: list[Resident],
    profile: dict[int, str],
    living: dict[tuple[int, int], set[int]],
    rng: random.Random,
    now: datetime.datetime,
) -> dict[int, list[tuple[Resident, int]]]:
    """Who held each past shift. About one in ten stays unmanned; a two-spot evening is held by two
    people, by one person for both spots, or by a lone holder of one spot (who is then given both)."""
    holders: dict[int, list[tuple[Resident, int]]] = {}
    for shift in past:
        if shift.is_disabled or rng.random() < 0.1:
            continue
        pool = [r for r in residents if r.pk in living[(shift.date.year, shift.date.month)]]
        first = _weighted(pool, profile, rng, set())
        if first is None:
            continue
        if shift.spots == 1:
            holders[shift.pk] = [(first, 1)]
            continue
        roll = rng.random()
        if roll < 0.2 and shift.kind == Kind.EVENING:
            holders[shift.pk] = [(first, 2)]
        elif roll < 0.35 and shift.kind == Kind.EVENING:
            holders[shift.pk] = [(first, 1)]  # a lone holder: complete_shift gives them both
        else:
            taken: list[tuple[Resident, int]] = [(first, 1)]
            for _ in range(
                shift.spots - 1 if shift.kind == Kind.EVENING else rng.randint(1, shift.spots - 2)
            ):
                other = _weighted(pool, profile, rng, {r.pk for r, _ in taken})
                if other:
                    taken.append((other, 1))
            holders[shift.pk] = taken
    return holders


def _plan_absences(
    past: list[KitchenShift],
    holders: dict[int, list[tuple[Resident, int]]],
    residents: list[Resident],
    profile: dict[int, str],
    now: datetime.datetime,
) -> dict[tuple[int, int], int | None]:
    """Two recent one-spot shifts that their holder did not turn up for: one fined the default (the
    shift's points again), one with the fine reduced to 1. Inside the 7 days mark_absent allows, and
    given to the slack, as it would be."""
    slack = [r for r in residents if profile[r.pk] in ("sloeset", "alarm")]
    # Over a day ago, so the fine is booked the morning after, well after the shift's own points.
    recent = [
        s
        for s in past
        if s.spots == 1
        and not s.is_disabled
        and s.starts_at > now - datetime.timedelta(days=6)
        and s.ends_at < now - datetime.timedelta(hours=20)
    ]
    absences: dict[tuple[int, int], int | None] = {}
    for shift, resident, fine in zip(recent[-2:], slack[:2], (None, 1), strict=False):
        holders[shift.pk] = [(resident, 1)]
        absences[(shift.pk, resident.pk)] = fine
    return absences


def _plan_trade(
    past: list[KitchenShift],
    holders: dict[int, list[tuple[Resident, int]]],
    residents: list[Resident],
    rng: random.Random,
) -> tuple[KitchenShift, Resident, Resident] | None:
    """A past evening the holder bought from someone else, who paid 2 extra points to be rid of it."""
    middle = past[len(past) // 2 :]
    for shift in middle:
        held = holders.get(shift.pk, [])
        if shift.kind == Kind.EVENING and len(held) == 2:
            buyer = held[0][0]
            seller = next(
                r for r in rng.sample(residents, len(residents)) if r.pk not in {h.pk for h, _ in held}
            )
            return shift, seller, buyer
    return None


class _Plan:
    """The calendar ahead, with who holds what kept in memory: asking each shift its free spots
    from the database, once per candidate per booking, is a hundred thousand queries."""

    def __init__(self, now: datetime.datetime, rng: random.Random) -> None:
        self.now, self.rng = now, rng
        self.future = list(
            KitchenShift.objects.filter(starts_at__gt=now, is_disabled=False).order_by("starts_at")
        )
        self.taken: dict[int, int] = defaultdict(int)
        self.held: dict[int, set[int]] = defaultdict(set)
        for shift_id, resident_id, spots in KitchenAssignment.objects.filter(
            shift__in=self.future
        ).values_list("shift_id", "resident_id", "spots"):
            self.taken[shift_id] += spots
            self.held[resident_id].add(shift_id)
        # Kept free on purpose: one inside 24 hours (red on the plan), an evening inside 72 (light red).
        day = now + datetime.timedelta(hours=24)
        self.keep_free = {
            s.pk
            for s in (
                self.first(lambda s: s.starts_at <= day),
                self.first(
                    lambda s: (
                        s.kind == Kind.EVENING and day < s.starts_at <= now + datetime.timedelta(hours=72)
                    )
                ),
            )
            if s
        }

    def first(self, test: Callable[[KitchenShift], bool]) -> KitchenShift | None:
        return next((s for s in self.future if test(s)), None)

    def open(self, shift: KitchenShift, resident: Resident, spots: int = 1) -> bool:
        return (
            shift.pk not in self.keep_free
            and self.taken[shift.pk] + spots <= shift.spots
            and shift.pk not in self.held[resident.pk]
        )

    def take(self, shift: KitchenShift, resident: Resident, spots: int = 1) -> None:
        services.enroll(shift, resident, spots=spots)
        self.taken[shift.pk] += spots
        self.held[resident.pk].add(shift.pk)

    def book_until(
        self,
        resident: Resident,
        goal: int | None,
        when: datetime.datetime,
        window: tuple[datetime.datetime, datetime.datetime],
    ) -> None:
        """Take shifts in `window` until the forecast reaches `goal` at `when`. One of the nearest
        dozen open ones each time, the evenings likelier as they are worth more: people book what is
        coming up, which leaves the plan full near today and sparser further out, as a real one is,
        and someone short of points takes the shifts that give the most."""
        if goal is None:
            return
        balance = KitchenPointEntry.balance_for(resident.pk)
        projected = services.balance_at(balance, services.forecast(resident), when)
        while projected < goal:
            options = [
                s for s in self.future if window[0] < s.starts_at <= window[1] and self.open(s, resident)
            ]
            if not options:
                return
            nearest = options[:12]
            shift = self.rng.choices(nearest, weights=[s.points_per_spot_total for s in nearest])[0]
            self.take(shift, resident)
            if shift.ends_at <= when:
                projected += shift.points_per_spot_total


def _plan_ahead(
    residents: list[Resident],
    by_email: dict[str, Resident],
    profile: dict[int, str],
    now: datetime.datetime,
    rng: random.Random,
) -> None:
    today = timezone.localdate(now)
    horizon = services.calendar_horizon()
    outlook = now + datetime.timedelta(days=services.OUTLOOK_DAYS)

    # The Christmas closure, a kitchen being painted, and a Sunday morning nobody wants.
    christmas = [datetime.date(today.year, 12, d) for d in (24, 25, 26)]
    for shift in KitchenShift.objects.filter(date__in=christmas, starts_at__gt=now):
        services.disable_shift(shift, "Juleferie – køkkenet er lukket")
    painted = KitchenShift.objects.filter(kind=Kind.MIDDAY, date=today + datetime.timedelta(days=8)).first()
    if painted:
        services.disable_shift(painted, "Køkkenet males")
    sunday = today + datetime.timedelta(days=(6 - today.weekday()) % 7 or 7)
    bonus = KitchenShift.objects.filter(kind=Kind.MORNING, date=sunday).first()
    if bonus:
        services.set_bonus(bonus, 2)

    plan = _Plan(now, rng)
    # Arranged first, while the plan still has room for them: beboer@'s shift for sale inside the
    # week and the sign-up they made three minutes ago, both spots of one evening held by one
    # resident (the second can be taken over from the plan), and a lone holder of one spot of an
    # evening inside the week.
    beboer = by_email.get("beboer@gahk.dk")
    selling = fresh = None
    if beboer:
        selling = plan.first(
            lambda s: (
                now + datetime.timedelta(days=2) < s.starts_at <= now + datetime.timedelta(days=6)
                and plan.open(s, beboer)
            )
        )
        fresh = plan.first(
            lambda s: (
                s.kind == Kind.MORNING
                and now + datetime.timedelta(days=12) < s.starts_at <= now + datetime.timedelta(days=25)
                and plan.open(s, beboer)
            )
        )
        for arranged in (selling, fresh):
            if arranged:
                plan.take(arranged, beboer)
    flittige = [r for r in residents if profile[r.pk] == "flittig"]
    if len(flittige) > 1:
        double = plan.first(
            lambda s: (
                s.kind == Kind.EVENING
                and s.date > today + datetime.timedelta(days=3)
                and plan.open(s, flittige[0], spots=2)
            )
        )
        if double:
            plan.take(double, flittige[0], spots=2)
        lone = plan.first(
            lambda s: (
                s.kind == Kind.EVENING
                and s.starts_at <= now + datetime.timedelta(days=7)
                and plan.taken[s.pk] == 0
                and plan.open(s, flittige[1])
            )
        )
        if lone:
            plan.take(lone, flittige[1])
    # The special shifts ahead, about half taken.
    for shift in [s for s in plan.future if s.kind == Kind.SPECIAL]:
        for resident in rng.sample(residents, k=min(shift.spots // 2, len(residents))):
            if plan.open(shift, resident):
                plan.take(shift, resident)

    # Everyone else books toward their profile's goals: the logins first, so they reach theirs; the
    # diligent last, as they take what is left.
    for resident in sorted(residents, key=lambda r: (r.email not in LOGINS, profile[r.pk] == "flittig")):
        p = PROFILES[profile[resident.pk]]
        plan.book_until(resident, p.soon, outlook, (now + datetime.timedelta(hours=6), outlook))
        plan.book_until(resident, p.end, _local(horizon, 0), (outlook, _local(horizon, 23)))

    # Booked over the past weeks, not all just now — or every one of them would sit in the regret
    # window. beboer@'s fresh sign-up is the one that does.
    upcoming = list(
        KitchenAssignment.objects.filter(shift__starts_at__gt=now).select_related("shift", "resident")
    )
    for assignment in upcoming:
        if fresh and beboer and (assignment.shift_id, assignment.resident_id) == (fresh.pk, beboer.pk):
            assignment.self_enrolled_at = assignment.created_at = now - datetime.timedelta(minutes=3)
            continue
        booked = now - datetime.timedelta(days=rng.randint(1, 20), hours=rng.randint(1, 12))
        assignment.self_enrolled_at = assignment.created_at = booked
    KitchenAssignment.objects.bulk_update(upcoming, ["self_enrolled_at", "created_at"])

    # For sale: beboer@'s, one inside the week sweetened with 2 extra points (both announced when
    # they were put up), and one further out, which was not announced.
    week = [
        a
        for a in upcoming
        if a.shift.starts_at <= now + datetime.timedelta(days=7) and a.resident.email not in LOGINS
    ]
    later = [
        a
        for a in upcoming
        if now + datetime.timedelta(days=10) < a.shift.starts_at <= outlook and a.resident.email not in LOGINS
    ]
    mine = next(
        (
            a
            for a in upcoming
            if selling and beboer and (a.shift_id, a.resident_id) == (selling.pk, beboer.pk)
        ),
        None,
    )
    for chosen, extra in ((mine, 0), (week[0] if week else None, 2), (later[0] if later else None, 1)):
        if chosen:
            _for_sale(chosen, extra, now)


def _for_sale(assignment: KitchenAssignment, extra: int, now: datetime.datetime) -> None:
    assignment.for_sale = True
    assignment.sale_bonus = extra
    assignment.put_for_sale_at = now - datetime.timedelta(hours=5)
    assignment.save(update_fields=["for_sale", "sale_bonus", "put_for_sale_at"])
