"""Køkkenvagter — allocated kitchen-cleaning shifts plus a fairness ledger.

Full design: `docs/plans/2026-09-21-koekkenvagter-design.md` (P1 + Amendments 1-3) and
`docs/plans/2026-09-28-koekkenvagter-p2-design.md` (P2: tier-B allocation, the full preference
model, verification/flagging, the kitchen tablet, the four UI surfaces). P1 was the slot model,
generation, tier-A (morgen/frokost) allocation, the ledger and obligation posting, and the
launch-seeding command. Summer (P3) is designed in `docs/plans/2026-10-04-koekkenvagter-p3-design.md`,
which supersedes the original design doc's "Summer" section and its `FerieUge` model: summer is never
allocated, residents claim shifts themselves. P3 step 2 built `Fravaer` (informational away ranges,
add/delete only); later P3 steps add the rest.

This replaces an informal, manual kitchen-credit scoreboard. It does **not** touch `ak.AkEntry`,
which is a separate system (monthly krydser for dorm labour) — the two must never be conflated.

Load-bearing invariants, so a later edit does not undo them by accident:

* **`Vagt` snapshots `headcount` and `duration_minutes` from `VagtRegel` at generation time.** It does
  not FK `VagtRegel`. `VagtRegel` is an officer-editable schedule (the `ak.AkMonthlyCharge` pattern),
  so editing a rule must not retroactively change a month whose slots already exist. One narrow
  amendment (Amendment 4, design doc §5.3): an accepted whole-shift take-over
  (`koekken.services.take_over_whole`) is the one operation that may change a generated `Vagt`'s
  `headcount`/`duration_minutes`, and only from `(2, d)` to `(1, 2d)`. It is recorded by the
  `OVERTAGET_HEL` `VagtBytte`, and `generate_vagter`'s `get_or_create` never resets it.
* **`KoekkenPost.delta_minutes` is an `IntegerField` — never float, never `Decimal`.** This ledger
  converts to money at move-out (per the design doc's move-out-penalty decision), so accumulated
  rounding is not acceptable; integer minutes make every balance exact by construction, the same
  argument `ak.AkEntry.delta` makes for krydser.
* **`KoekkenPost` is append-only**, mirroring `ak.AkEntry`: corrections are new rows (`JUSTERING`,
  `TILBAGEFOERSEL`), never edits or deletes of a posted entry. Balance is `SUM(delta_minutes)`.
* **`VagtTildeling` is stored, not derived** — unlike `events.Rsvp`, which deliberately derives
  seating from claim order because claims there are voluntary. Assignment here is the authoritative
  *output* of an allocator (`koekken.services.allocate_tier_a`), so it is a row. Plain `TILDELT`
  rows are deliberately recomputed on every re-run (that is the idempotency), but once a row has
  moved past `TILDELT` — self-reported `UDFOERT`, or flagged `ANMELDT`/`IKKE_UDFOERT` — the allocator
  never deletes or overwrites it and never hands its resident a second row for that vagt, so that
  part of a resident's shift history genuinely survives a re-run of the allocator that produced it.
* **`Praeference` is scoped to `(resident, periode)`, not to a month.** A weekday-unavailable
  declaration holds for the whole semester it is made in — every month's tier-A run inside that
  `Periode` sees the same declarers, which is also why `Vagt`/`KoekkenPost` key off `Periode`, not a
  rolling window: periods are calendar-anchored.
* **Idempotency of monthly obligation posting is a database constraint**, not application discipline:
  `uniq_koekken_forpligtelse_per_period_month` mirrors `ak.AkEntry`'s
  `uniq_ak_monthly_per_period` exactly (a partial unique index on `kind="forpligtelse"`), so a
  double-run of `post_koekken_obligation` cannot double-post even if the calling code has a bug.

**Amendment 1** (`docs/plans/2026-09-21-koekkenvagter-design.md`, the section after P1's sign-off)
adds `Praeference.declared_at` (FCFS tiebreak), an allocation look-ahead window with a projected
ranking balance, and preference locking — see `koekken.services` for the mechanics
(`allocate_tier_a`'s guard/ranking, `set_preference`, `periode_deadline`, `roll_forward_allocation`).
It does **not** change any of the invariants above: `post_obligation` is untouched, `KoekkenPost` and
its integer minutes are untouched, and `VagtTildeling`'s shape and survivor handling are untouched —
only *when* `allocate_tier_a` may run and *how it ranks* changed.

`Periode.Kind.SOMMER` exists as a choice: summer is a real periode (its shifts are generated and its
obligation posts) but is never allocated — see the P3 design doc, `koekken.services.periode_is_allocated`.

**Amendment 5** (the design doc's "fridage" section) adds `Fridag`: concrete, per-(date, kind)
exclusion rows Køkkengruppen enters by hand (juleaften, nytårsaften, ...) so `generate_vagter` never
creates a `Vagt` for them in the first place. It is a new, self-contained leaf on top of everything
above — it changes nothing about `Vagt`'s snapshot invariant, the ledger's append-only shape, or
`VagtTildeling`'s survivor handling; `koekken.services.declare_fridag` is the one place that reaches
backwards into an already-generated month (delete the matching `Vagt` rows and re-post obligation via `post_obligation(...)`, never
re-allocate — 2026-10-04 supplement) when a
fridag is declared too late to be caught by generation alone, which the design doc's own arithmetic
(a periode is allocated ~122 days before it starts) says is the normal case, not an edge case.

**Amendment 4, step 1** (`docs/plans/2026-10-04-koekkenvagter-a4-design.md`) adds `VagtBytte`: one offer
of one `VagtTildeling` ("I cannot work this; anyone may take it"). Taking an offer over MOVES the
existing `VagtTildeling` row to the taker (pk unchanged), so credit, flags and the tablet follow
automatically; there is still no unclaim. A completed hand-off (`OVERTAGET`/`OVERTAGET_HEL`) is
identified by query (`koekken.services.handed_off_tildeling_filter`) and is excluded from the three
force re-run deletes, so it survives a deliberate re-allocation (the delete locks the candidate rows
first -- `select_for_update` -- so a concurrent take-over cannot slip past the exclusion). Expiry of an unclaimed offer is
derived (the shift has started) and never written. The Den Hurtige post is a later step.

**Amendment 4, step 2** adds `VagtBytteForslag`: a trade proposal on an open offer ("I'll give you my Y
for your X"). Accepting swaps the two rows' residents in one atomic exchange (pks unchanged); a row that
is the `modydelse` of an `ACCEPTERET` proposal is a completed hand-off too and survives a force re-run.

**Amendment 6** (`docs/plans/2026-10-07-koekkenvagter-a6-design.md`) adds `FestKredit`: one award of
credit for party work (1 kryds = 1 hour), recorded by Køkkengruppen. It writes two new ledger kinds,
`FESTKREDIT` (positive, one per helper) and `FESTBIDRAG` (negative, one per resident on the event
month's list), which share one `FestKredit` row via `KoekkenPost.festkredit`. The house funds every
award, so the self-balancing invariant now reads: **total obligation plus festbidrag = total credit**
(festbidrag exactly offsets festkredit). An undo is a set of `TILBAGEFOERSEL` rows, never a delete.
"""

from datetime import date, time

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from residents.models import Resident


def _declared_at_default() -> date:
    """Default for `Praeference.declared_at`: today, read through `core.clock` (imported locally to
    avoid a models-import-time dependency, matching `residents.models.active_period`'s own reason for
    the same local import) so a DevClock override under DEBUG is honoured -- see Amendment 1's A1.1
    (the FCFS tiebreak) and A1.5's DevClock walk test."""
    from core.clock import current_date

    return current_date()


class VagtRegel(models.Model):
    """One row per (kind, weekend/weekday): the officer-editable duration + headcount schedule.

    A table, not constants — following `ak.AkMonthlyCharge`'s precedent of an officer-editable
    schedule rather than logic baked into code. Seeded (migration 0001): morgen 1 h / 1 person,
    frokost 1 h / 1 person (same both weekday and weekend), weekday aften 3 h / 2 people, weekend
    aften 2 h / 1 person — see the design doc's "arithmetic" section for where those numbers came
    from. `Vagt` generation snapshots duration/headcount; editing a rule here only affects months
    generated afterwards.

    **`start_time` (P2, §5) is NOT snapshotted onto `Vagt`, unlike `duration_minutes`/`headcount`
    above -- read it live, straight off this table, every time.** That is a deliberate asymmetry
    with the snapshot invariant right above it, not an oversight: `duration_minutes`/`headcount` are
    *allocation inputs* that must not retroactively change a published month (that is the whole
    point of the snapshot). `start_time` is instead an *operational* fact used only at the moment a
    resident taps "done" at the tablet (`koekken.services.marking_window`) -- if the kitchen moves
    lunch duty to 13:00, every future lunch shift should open at 13:00 immediately, and a past
    shift's already-evaluated window is unaffected regardless, because it was checked against
    whatever this table said at tap time. Seeded (migration 0004): morgen 06:00, frokost 12:00,
    aften 17:00 -- the same start time for a kind's weekday and weekend row, per the design doc's
    table in "The marking window".
    """

    class Kind(models.TextChoices):
        MORGEN = "morgen", "Morgenvagt"
        FROKOST = "frokost", "Frokostvagt"
        AFTEN = "aften", "Aftenvagt"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    weekend = models.BooleanField(help_text="Gælder for lørdag/søndag frem for hverdage.")
    duration_minutes = models.PositiveSmallIntegerField()
    headcount = models.PositiveSmallIntegerField()
    start_time = models.TimeField(
        default=time(6, 0),
        help_text="Hvornår vagten kan markeres udført fra køkken-tabletten (læses direkte herfra, "
        "aldrig snapshottet på Vagt -- se modellens docstring).",
    )

    class Meta:
        ordering = ["kind", "weekend"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "weekend"], name="uniq_vagtregel_kind_weekend"),
        ]

    def __str__(self) -> str:
        when = "weekend" if self.weekend else "hverdag"
        return f"{self.get_kind_display()} ({when}): {self.duration_minutes} min, {self.headcount} pers."


class Periode(models.Model):
    """A semester (or the short summer period): the ledger's key.

    Calendar-anchored, not a rolling window — `EFTERAAR` is always Sep–Jan, `FORAAR` always Feb–Jun,
    `SOMMER` always Jul–Aug (see the design doc's "Decisions" table). `year` is the calendar year the
    period *starts* in, so `EFTERAAR 2026` runs 2026-09-01..2027-01-31.
    """

    class Kind(models.TextChoices):
        EFTERAAR = "efteraar", "Efterår"
        FORAAR = "foraar", "Forår"
        SOMMER = "sommer", "Sommer"  # self-signup, never allocated (P3)

    kind = models.CharField(max_length=10, choices=Kind.choices)
    year = models.PositiveSmallIntegerField()
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        ordering = ["year", "start_date"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "year"], name="uniq_periode_kind_year"),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.year}"


class Vagt(models.Model):
    """One generated shift: a (date, kind) with headcount/duration snapshotted from `VagtRegel`.

    `koekken.services.generate_vagter` is the only writer. `headcount` and `duration_minutes` are
    copied at generation time and never re-read from `VagtRegel` afterwards — see the module
    docstring's first invariant.
    """

    periode = models.ForeignKey(Periode, on_delete=models.CASCADE, related_name="vagter")
    date = models.DateField()
    kind = models.CharField(max_length=10, choices=VagtRegel.Kind.choices)
    headcount = models.PositiveSmallIntegerField()
    duration_minutes = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["date", "kind"]
        indexes = [models.Index(fields=["date"])]
        constraints = [
            models.UniqueConstraint(fields=["date", "kind"], name="uniq_vagt_date_kind"),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.date:%Y-%m-%d}"


class VagtTildeling(models.Model):
    """One resident assigned to one `Vagt`. Authoritative output of the allocator — stored, not
    derived (see the module docstring).

    The full status enum is defined here even though P1's allocator only ever writes `TILDELT`:
    `UDFOERT`/`ANMELDT`/`IKKE_UDFOERT` belong to the self-report + flagging workflow, which is P2 —
    but the column shape should not need a migration when that lands.
    """

    class Status(models.TextChoices):
        TILDELT = "tildelt", "Tildelt"
        UDFOERT = "udfoert", "Udført"
        ANMELDT = "anmeldt", "Anmeldt"
        IKKE_UDFOERT = "ikke_udfoert", "Ikke udført"

    vagt = models.ForeignKey(Vagt, on_delete=models.CASCADE, related_name="tildelinger")
    resident = models.ForeignKey(Resident, on_delete=models.CASCADE, related_name="koekken_tildelinger")
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.TILDELT)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["vagt", "resident"], name="uniq_vagttildeling_vagt_resident"),
        ]

    def __str__(self) -> str:
        return f"{self.resident.full_name} -> {self.vagt} ({self.get_status_display()})"


class FestKredit(models.Model):
    """One award of party credit (Amendment 6): "Nytårsfest, 8 helpers, 5 h each". The award is funded by
    the house (every resident on the event month's list pays an even share), so the ledger stays
    self-balancing. The hours live on the linked `KoekkenPost` rows, not here. Undo is by reversal
    (`fortrudt_at`/`fortrudt_by` plus `TILBAGEFOERSEL` rows), never deletion -- the rows PROTECT this."""

    navn = models.CharField(max_length=100, verbose_name="Begivenhed")
    dato = models.DateField()
    created_by = models.ForeignKey(
        Resident,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="koekken_festkreditter_oprettet",
    )
    created_at = models.DateTimeField(default=timezone.now)
    fortrudt_at = models.DateTimeField(null=True, blank=True)
    fortrudt_by = models.ForeignKey(
        Resident,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="koekken_festkreditter_fortrudt",
    )

    class Meta:
        ordering = ["-dato", "-created_at"]
        verbose_name = "festkredit"
        verbose_name_plural = "festkreditter"

    def __str__(self) -> str:
        return f"{self.navn} ({self.dato:%d.%m.%Y})"


class KoekkenPost(models.Model):
    """The append-only fairness ledger. Balance is `SUM(delta_minutes)` — see
    `koekken.services.balance_for`. Mirrors `ak.AkEntry`'s shape; read that model first.

    `month` (1..12) is only meaningful for `FORPLIGTELSE` (the monthly obligation charge) — it is the
    authoritative "which month" key for that kind, exactly as `AkEntry.year`/`AkEntry.month` are for
    `MONTHLY`, which is what lets `uniq_koekken_forpligtelse_per_period_month` guarantee a re-run of
    `post_koekken_obligation` never double-charges. It stays null for every other kind.
    """

    class Kind(models.TextChoices):
        FORPLIGTELSE = "forpligtelse", "Forpligtelse"  # monthly obligation charge (negative delta)
        ARBEJDE = "arbejde", "Udført arbejde"  # P2: credit for a self-reported UDFOERT shift
        JUSTERING = "justering", "Manuel justering"
        STARTSALDO = "startsaldo", "Startsaldo"  # launch rebase, see seed_koekken_balances
        TILBAGEFOERSEL = "tilbagefoersel", "Tilbageførsel"  # P2: reverses a flagged IKKE_UDFOERT
        FESTKREDIT = "festkredit", "Festkredit"  # A6: credit to a party helper (positive)
        FESTBIDRAG = "festbidrag", "Festbidrag"  # A6: the house's share of funding an award (negative)

    resident = models.ForeignKey(Resident, on_delete=models.CASCADE, related_name="koekken_posts")
    delta_minutes = (
        models.IntegerField()
    )  # minutes; negative = debit. Never float/Decimal — see module docstring.
    kind = models.CharField(max_length=15, choices=Kind.choices)
    # PROTECT, not CASCADE: this is the ledger's key and its rows are the record. A Periode with
    # posted entries must not be deletable out from under them (see core.Room's PROTECT on
    # Residency for the same reasoning — an accidental delete must be loud, not a silent data loss).
    periode = models.ForeignKey(Periode, on_delete=models.PROTECT, related_name="koekken_posts")
    month = models.PositiveSmallIntegerField(null=True, blank=True)  # 1..12; FORPLIGTELSE only
    vagt = models.ForeignKey(
        Vagt, null=True, blank=True, on_delete=models.SET_NULL, related_name="koekken_posts"
    )
    # PROTECT for the same reason as `periode`: an award with ledger rows must never be deletable.
    festkredit = models.ForeignKey(
        FestKredit, null=True, blank=True, on_delete=models.PROTECT, related_name="poster"
    )
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        Resident,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="koekken_posts_created",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["resident", "created_at"])]
        constraints = [
            # At most one FORPLIGTELSE per resident per (periode, month) — the obligation-posting
            # idempotency guard. Exactly `ak.AkEntry`'s uniq_ak_monthly_per_period.
            models.UniqueConstraint(
                fields=["resident", "periode", "month"],
                condition=Q(kind="forpligtelse"),
                name="uniq_koekken_forpligtelse_per_period_month",
            ),
            # At most one STARTSALDO per resident, structurally — mirrors the partial-unique pattern
            # above and `ak.AkEntry`'s own constraints. Without it, `KoekkenPostAdmin` allows creating
            # a second STARTSALDO row for the same resident, and both `seed_koekken_balances` and
            # `koekken.demo` key their `update_or_create(resident=, kind=STARTSALDO)` on there being at
            # most one — a second row makes that `get()` raise `MultipleObjectsReturned` as a bare
            # traceback instead of a clean `CommandError`.
            models.UniqueConstraint(
                fields=["resident"],
                condition=Q(kind="startsaldo"),
                name="uniq_koekken_startsaldo_per_resident",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.resident.full_name}: {self.delta_minutes:+d} min ({self.get_kind_display()})"


class Praeference(models.Model):
    """A resident's kitchen-duty preference for one `Periode`. Exactly one field here is a HARD
    constraint (`weekday_unavailable`); the three soft day-preferences (which weekdays a resident
    would prefer for morgen/frokost/aften) live in the related `PraeferenceDag` model below, not as
    columns here -- see that model's docstring for why. P2 design doc §2.

    **Amendment 1 (A1.1, A1.3):** `declared_at` is when this row was (last) written, via
    `koekken.services.set_preference` -- it is the FCFS tiebreak `allocate_tier_a` sorts on
    (`(projected_balance ASC, declared_at ASC, pk ASC)`), and re-declaring counts as declaring again.
    A resident with no row for a `Periode` has no `declared_at` and sorts LAST on a tie -- see
    `allocate_tier_a`'s ranking helper, which uses `date.max` as the sentinel, never a null default.
    """

    resident = models.ForeignKey(Resident, on_delete=models.CASCADE, related_name="koekken_praeferencer")
    periode = models.ForeignKey(Periode, on_delete=models.CASCADE, related_name="praeferencer")
    weekday_unavailable = models.BooleanField(
        default=False,
        help_text="Ruter beboeren til weekend-puljen for morgen-/frokostvagt (tier A) i denne periode.",
    )
    declared_at = models.DateField(
        default=_declared_at_default,
        help_text="Hvornår denne præference (senest) blev erklæret -- bruges som FCFS-tiebreak ved "
        "allokering (Amendment 1, A1.1).",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["resident", "periode"], name="uniq_praeference_resident_periode"),
        ]

    def __str__(self) -> str:
        flag = "hverdage utilgængelig" if self.weekday_unavailable else "ingen præference"
        return f"{self.resident.full_name} ({self.periode}): {flag}"


class PraeferenceDag(models.Model):
    """One SOFT day-preference: "for `kind` (morgen/frokost/aften), I would prefer `weekday`."
    P2 design doc §2. Unlike `Praeference.weekday_unavailable`, nothing here ever excludes anyone
    from anything -- these rows only ever influence WHICH slot a resident already being seated is
    offered (`koekken.services.allocate_tier_b`'s preferred-day matching, and the day-preference part
    of tier-A's soft floor), never WHO is picked next. That "preferences decide which slot, never who
    is next" rule is this feature's central fairness invariant; see `allocate_tier_b`'s docstring.

    **A related model, not three list columns, and not one combined per-weekday structure** -- see
    the P2 design doc's "Storage" subsection for the full reasoning; in short: a single per-weekday
    "choose morgen/frokost/either/neither" shape cannot express wanting BOTH Tuesday morgen AND
    Tuesday frokost, and this repo has no `ArrayField` despite running Postgres (a `JSONField` list of
    ints would get no DB-level validation, unlike the choices + unique constraint here). `kind` reuses
    `VagtRegel.Kind` rather than inventing a parallel enum, because it is the exact same three shift
    kinds and the allocator's real query ("which days does this resident prefer for kind K") becomes
    one filtered prefetch instead of three differently-shaped ones.
    """

    praeference = models.ForeignKey(Praeference, on_delete=models.CASCADE, related_name="dage")
    kind = models.CharField(max_length=10, choices=VagtRegel.Kind.choices)
    weekday = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(6)],
        help_text="0 = mandag .. 6 = søndag (Python's date.weekday()).",
    )

    class Meta:
        ordering = ["kind", "weekday"]
        constraints = [
            models.UniqueConstraint(
                fields=["praeference", "kind", "weekday"], name="uniq_praeferencedag_praeference_kind_weekday"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.praeference.resident.full_name}: {self.get_kind_display()} ugedag {self.weekday}"


class VagtAnmeldelse(models.Model):
    """A flag on one `VagtTildeling`: "denne vagt blev ikke udført" -- P2 design doc §6. ONE uniform
    action, available on any shift dated today or earlier regardless of its `VagtTildeling.Status`
    (self-reported, still just assigned, or already flagged before) -- the ledger consequence of an
    upheld ruling differs by what `previous_status` was, but the flag itself is the same row shape
    either way. Named after the existing `VagtTildeling.Status.ANMELDT` choice ("anmeldt" = flagged/
    reported), which P1 already reserved for exactly this state without a view ever setting it.

    **Flagger identity is never hidden** -- `flagged_by` is a plain, always-populated FK, shown to
    Køkkengruppen AND to the flagged resident (design doc §6: "one consequence is recorded here
    without arguing it" -- no anonymity mechanism exists or should be added here.

    `previous_status` freezes what `vagt_tildeling.status` was AT THE MOMENT OF FLAGGING (always
    `UDFOERT` or `TILDELT` in practice -- see `koekken.services.flag_tildeling`), because that is what
    decides the ledger consequence of an upheld ruling (`koekken.services.resolve_anmeldelse`) and
    what a DISMISSED ruling restores the assignment to. Read it, never re-derive it from
    `vagt_tildeling.status`, which `flag_tildeling` immediately overwrites to `ANMELDT`.
    """

    class Status(models.TextChoices):
        AABEN = "aaben", "Åben"
        OPRETHOLDT = "opretholdt", "Opretholdt"
        AFVIST = "afvist", "Afvist"

    vagt_tildeling = models.ForeignKey(VagtTildeling, on_delete=models.CASCADE, related_name="anmeldelser")
    flagged_by = models.ForeignKey(
        Resident, on_delete=models.CASCADE, related_name="koekken_anmeldelser_lavet"
    )
    previous_status = models.CharField(max_length=15, choices=VagtTildeling.Status.choices)
    reason = models.TextField(blank=True, verbose_name="Begrundelse")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.AABEN)
    resolved_by = models.ForeignKey(
        Resident,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="koekken_anmeldelser_afgjort",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # At most one OPEN flag per assignment at a time -- mirrors the STARTSALDO partial-unique
            # pattern above. A second flag on the same vagt_tildeling is only meaningful once the
            # first has been resolved (a re-flag after a dismissal is a genuinely new claim).
            models.UniqueConstraint(
                fields=["vagt_tildeling"],
                condition=Q(status="aaben"),
                name="uniq_vagtanmeldelse_open_per_tildeling",
            ),
        ]

    def __str__(self) -> str:
        return f"Anmeldelse af {self.vagt_tildeling} af {self.flagged_by.full_name} ({self.get_status_display()})"


class Fridag(models.Model):
    """One excluded `(date, kind)` pair -- Amendment 5 (A5.2): a day the kitchen does not need
    covering for that shift type at all, e.g. juleaften, nytårsaften. `koekken.services.is_fridag` is
    the SINGLE question `generate_vagter` asks per `(date, kind)` before creating a `Vagt` for it --
    see that function's docstring for why it is built as a seam rather than an inline query, and never
    bypass it with a direct `Fridag.objects` check elsewhere.

    Mirrors `Vagt`'s own `(date, kind)` key exactly, and is the same shape as P2's
    `PraeferenceDag(praeference, kind, weekday)` -- this app's existing idiom for "one row per
    excluded/selected (something, kind)" rather than a new one. A whole day off is simply every kind
    excluded for that date; it needs no separate flag.

    Concrete dates only, entered per periode -- there is deliberately no recurring month/day rule here
    (the design doc's "fridage" section: Danish holidays split awkwardly between fixed dates like
    juleaften and movable ones like påske, which a rule engine would handle no better than a human
    with a calendar). `reason` ("Juleaften") is shown on the tablet and in Køkkengruppen's list; the
    UI writes the same text across a date's rows, so duplication across kinds for the same date is
    harmless and has a single writer.
    """

    date = models.DateField()
    kind = models.CharField(max_length=10, choices=VagtRegel.Kind.choices)
    reason = models.CharField(max_length=255, blank=True, verbose_name="Begrundelse")

    class Meta:
        ordering = ["date", "kind"]
        constraints = [
            models.UniqueConstraint(fields=["date", "kind"], name="uniq_fridag_date_kind"),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.date:%Y-%m-%d} (fridag)"


class VagtBytte(models.Model):
    """One offer of one `VagtTildeling` -- Amendment 4 (design doc `2026-10-04-koekkenvagter-a4-design.md`).
    "I cannot work this shift; anyone may take it over." Offering moves nothing: the offerer stays
    responsible until somebody takes it (`koekken.services.take_over` / `take_over_whole`).

    **Expiry is derived and never written.** An `AABEN` offer whose shift has started is "expired"
    whenever it is read (`koekken.services.has_started`): not listed, not takeable. It stays `AABEN` in
    the database; no scheduled task flips it.

    **Rows are moved, not recreated.** A take-over changes `resident` on the existing `VagtTildeling`
    (its pk survives, and so does this offer as history). The one exception is the whole-shift collapse,
    which re-points `tildeling` at the partner's surviving row BEFORE deleting the vacated one, so the
    offer survives as `OVERTAGET_HEL`. `BYTTET` is written only by step 2 (trading) but is defined now
    so that step needs no choices-only migration.
    """

    class Status(models.TextChoices):
        AABEN = "aaben", "Åben"
        OVERTAGET = "overtaget", "Overtaget"
        OVERTAGET_HEL = "overtaget_hel", "Hele vagten overtaget"
        BYTTET = "byttet", "Byttet"
        TRUKKET = "trukket", "Trukket tilbage"
        BORTFALDET = "bortfaldet", "Bortfaldet"

    tildeling = models.ForeignKey(VagtTildeling, on_delete=models.CASCADE, related_name="byttetilbud")
    tilbudt_af = models.ForeignKey(Resident, on_delete=models.CASCADE, related_name="koekken_byttetilbud")
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.AABEN)
    overtaget_af = models.ForeignKey(
        Resident,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="koekken_overtagelser",
    )
    created_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)
    # The Den Hurtige post that advertises this offer (Amendment 4 step 3), if the offerer shared it. The
    # link exists only so the post can be archived when the offer closes (`services._archive_hurtig_posts`,
    # and the `post_delete` receiver in koekken.signals for cascades); Den Hurtige never reads it. SET_NULL
    # because the author may hard-delete the post within Den Hurtige's grace period.
    hurtig_post = models.ForeignKey(
        "den_hurtige.QuickPost", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # At most one OPEN offer per assignment -- the VagtAnmeldelse pattern.
            models.UniqueConstraint(
                fields=["tildeling"],
                condition=Q(status="aaben"),
                name="uniq_vagtbytte_open_per_tildeling",
            ),
        ]

    def __str__(self) -> str:
        return f"Byttetilbud af {self.tildeling} af {self.tilbudt_af.full_name} ({self.get_status_display()})"


class VagtBytteForslag(models.Model):
    """A trade proposal on an open offer -- Amendment 4 step 2: "I'll give you my `modydelse` (Y) for
    your offered row (X)". The offerer accepts or declines; accepting swaps the residents of X and Y in
    one atomic exchange (`koekken.services.accept_trade`), never two separate take-overs.

    **Expiry is derived, never written.** An `AABEN` proposal is live only while both X and Y are
    unstarted, `TILDELT`, free of flag history and still held by the offerer / the proposer
    respectively; otherwise it is stale and shown/acted on as nothing, yet stays `AABEN` in the database
    (the same lazy shape as `VagtBytte`). Moves and withdrawals that invalidate a proposal persist
    `BORTFALDET` explicitly (`services._close_forslag`), always after locking the rows first.
    """

    class Status(models.TextChoices):
        AABEN = "aaben", "Åben"
        ACCEPTERET = "accepteret", "Accepteret"
        AFVIST = "afvist", "Afvist"
        TRUKKET = "trukket", "Trukket tilbage"
        BORTFALDET = "bortfaldet", "Bortfaldet"

    bytte = models.ForeignKey(VagtBytte, on_delete=models.CASCADE, related_name="forslag")
    modydelse = models.ForeignKey(VagtTildeling, on_delete=models.CASCADE, related_name="byttemodydelser")
    foreslaaet_af = models.ForeignKey(Resident, on_delete=models.CASCADE, related_name="koekken_bytteforslag")
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.AABEN)
    created_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # At most one OPEN proposal per (offer, offered-in-exchange row).
            models.UniqueConstraint(
                fields=["bytte", "modydelse"],
                condition=Q(status="aaben"),
                name="uniq_vagtbytteforslag_open_per_bytte_modydelse",
            ),
        ]

    def __str__(self) -> str:
        return f"Bytteforslag fra {self.foreslaaet_af.full_name} på {self.bytte_id} ({self.get_status_display()})"


class Fravaer(models.Model):
    """One away range ("væk fra-til") a resident has registered -- P3 design doc §2/§3.

    **Informational only.** Nothing in allocation, claiming, obligation or generation ever reads this
    table (`is_fridag` is not a consumer either -- see its docstring); it exists so the whole house can
    see who is away each week. Collected and shown for summer only in v1, but general in shape (it could
    later carry exchange, internship, illness), so it is not keyed to a `Periode`.

    Add and delete only -- no edit; a resident deletes and re-adds. Ranges are inclusive. Overlap with
    the same resident's other ranges is refused in `koekken.services.add_fravaer`, not in the database
    (this repo uses no Postgres exclusion constraints); only `start_date <= end_date` is a DB check.
    """

    resident = models.ForeignKey(Resident, on_delete=models.CASCADE, related_name="koekken_fravaer")
    start_date = models.DateField()
    end_date = models.DateField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["start_date"]
        constraints = [
            models.CheckConstraint(condition=Q(start_date__lte=F("end_date")), name="fravaer_start_lte_end"),
        ]
        indexes = [models.Index(fields=["resident", "start_date"])]

    def __str__(self) -> str:
        return f"{self.resident.full_name} væk {self.start_date:%Y-%m-%d} - {self.end_date:%Y-%m-%d}"
