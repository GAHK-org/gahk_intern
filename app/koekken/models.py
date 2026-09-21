"""Køkkenvagter — allocated kitchen-cleaning shifts plus a fairness ledger.

Full design: `docs/plans/2026-09-21-koekkenvagter-design.md`. This is **P1 only** (per that doc's
"Phasing" table): the slot model, generation, tier-A (morgen/frokost) allocation, the ledger and
obligation posting, and the launch-seeding command. Tier-B (aftenvagt) signup, preferences beyond
the single `weekday_unavailable` flag, verification/flagging, and the summer `FerieUge` presence
model are **not** built here — they are P2/P3, deliberately absent rather than stubbed.

This replaces an informal, manual kitchen-credit scoreboard. It does **not** touch `ak.AkEntry`,
which is a separate system (monthly krydser for dorm labour) — the two must never be conflated.

Load-bearing invariants, so a later edit does not undo them by accident:

* **`Vagt` snapshots `headcount` and `duration_minutes` from `VagtRegel` at generation time.** It does
  not FK `VagtRegel`. `VagtRegel` is an officer-editable schedule (the `ak.AkMonthlyCharge` pattern),
  so editing a rule must not retroactively change a month whose slots already exist.
* **`KoekkenPost.delta_minutes` is an `IntegerField` — never float, never `Decimal`.** This ledger
  converts to money at move-out (per the design doc's move-out-penalty decision), so accumulated
  rounding is not acceptable; integer minutes make every balance exact by construction, the same
  argument `ak.AkEntry.delta` makes for krydser.
* **`KoekkenPost` is append-only**, mirroring `ak.AkEntry`: corrections are new rows (`JUSTERING`,
  `TILBAGEFOERSEL`), never edits or deletes of a posted entry. Balance is `SUM(delta_minutes)`.
* **`VagtTildeling` is stored, not derived** — unlike `events.Rsvp`, which deliberately derives
  seating from claim order because claims there are voluntary. Assignment here is the authoritative
  *output* of an allocator (`koekken.services.allocate_tier_a`), so it is a row, and a resident's
  shift history survives even if the allocator that produced it is re-run.
* **`Praeference` is scoped to `(resident, periode)`, not to a month.** A weekday-unavailable
  declaration holds for the whole semester it is made in — every month's tier-A run inside that
  `Periode` sees the same declarers, which is also why `Vagt`/`KoekkenPost` key off `Periode`, not a
  rolling window: periods are calendar-anchored.
* **Idempotency of monthly obligation posting is a database constraint**, not application discipline:
  `uniq_koekken_forpligtelse_per_period_month` mirrors `ak.AkEntry`'s
  `uniq_ak_monthly_per_period` exactly (a partial unique index on `kind="forpligtelse"`), so a
  double-run of `post_koekken_obligation` cannot double-post even if the calling code has a bug.

`Periode.Kind.SOMMER` exists as a choice even though nothing generates a summer period's slots yet
(that needs `FerieUge`, weekly presence — P3, see the design doc's "Summer" section on why `Residency`
alone is wrong for July/August). Leaving the choice in now means the eventual P3 migration adds a
model, not a schema change to this one.
"""

from django.db import models
from django.db.models import Q
from django.utils import timezone

from residents.models import Resident


class VagtRegel(models.Model):
    """One row per (kind, weekend/weekday): the officer-editable duration + headcount schedule.

    A table, not constants — following `ak.AkMonthlyCharge`'s precedent of an officer-editable
    schedule rather than logic baked into code. Seeded (migration 0001): morgen 1 h / 1 person,
    frokost 1 h / 1 person (same both weekday and weekend), weekday aften 3 h / 2 people, weekend
    aften 2 h / 1 person — see the design doc's "arithmetic" section for where those numbers came
    from. `Vagt` generation snapshots these values; editing a rule here only affects months
    generated afterwards.
    """

    class Kind(models.TextChoices):
        MORGEN = "morgen", "Morgenvagt"
        FROKOST = "frokost", "Frokostvagt"
        AFTEN = "aften", "Aftenvagt"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    weekend = models.BooleanField(help_text="Gælder for lørdag/søndag frem for hverdage.")
    duration_minutes = models.PositiveSmallIntegerField()
    headcount = models.PositiveSmallIntegerField()

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
        SOMMER = "sommer", "Sommer"  # P3: no generation/allocation targets this yet.

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
        ]

    def __str__(self) -> str:
        return f"{self.resident.full_name}: {self.delta_minutes:+d} min ({self.get_kind_display()})"


class Praeference(models.Model):
    """A resident's kitchen-duty preference for one `Periode`. P1 holds only the single boolean
    tier-A needs; the aftenvagt weekday preference field is P2 (tier-B signup does not exist yet).
    """

    resident = models.ForeignKey(Resident, on_delete=models.CASCADE, related_name="koekken_praeferencer")
    periode = models.ForeignKey(Periode, on_delete=models.CASCADE, related_name="praeferencer")
    weekday_unavailable = models.BooleanField(
        default=False,
        help_text="Ruter beboeren til weekend-puljen for morgen-/frokostvagt (tier A) i denne periode.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["resident", "periode"], name="uniq_praeference_resident_periode"),
        ]

    def __str__(self) -> str:
        flag = "hverdage utilgængelig" if self.weekday_unavailable else "ingen præference"
        return f"{self.resident.full_name} ({self.periode}): {flag}"
