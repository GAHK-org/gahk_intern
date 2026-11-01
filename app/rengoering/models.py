"""Søndagsrengøring — cleaning rotas, the alumne's sign-off, and Inspektionen's check.

Every Sunday each alumne on the rota cleans one area by 22.00 and signs off; a member of Inspektionen
checks at 22.00 and either approves it or records mangler, which must be fixed by 22.00 the next day
and are then re-checked. See spec/features/rengoering.md.

  * `CleaningArea` / `CleaningTask` are the checklist. Zone and group are plain strings on the task:
    they only group the list on screen, so models of their own would buy joins and admin forms for
    nothing. `kind` is there so køkkenrengøring can be added later without a second set of tables.
  * `Rota` is one planning period ("E26"); `CleaningAssignment` is one area on one date for one
    resident. The assignments are the source of truth once a rota is made — the rotation order is an
    input to the generator and is not stored, so a swap can never disagree with it.
  * `Inspection` is one round of checking one assignment: round 1 on the Sunday, 2+ the follow-ups.

The assignment's status is derived (`CleaningAssignment.status`), never stored, so it cannot drift
from the rows it summarises.
"""

import datetime

from django.db import models
from django.utils import timezone

# The hour (local time) by which a cleaning must be done and mangler fixed — "inden kl. 22.00".
DEADLINE_HOUR = 22
# An inspection decided before this hour (local) belongs to the previous evening's round: a check
# that started at 22.00 and ran past midnight should not push the mangler deadline a day further.
INSPECTION_DAY_STARTS_HOUR = 6


def at_deadline_hour(day: datetime.date) -> datetime.datetime:
    """22.00 local time on `day`, as an aware datetime."""
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(DEADLINE_HOUR)))


def default_fix_deadline(decided_at: datetime.datetime) -> datetime.datetime:
    """22.00 the day after the inspection evening — "mangler skal udbedres inden kl. 22.00 den
    følgende dag" (wiki: Rengøring)."""
    local = timezone.localtime(decided_at)
    evening = local.date()
    if local.hour < INSPECTION_DAY_STARTS_HOUR:
        evening -= datetime.timedelta(days=1)
    return at_deadline_hour(evening + datetime.timedelta(days=1))


class CleaningArea(models.Model):
    class Kind(models.TextChoices):
        SOENDAG = "soendag", "Søndagsrengøring"
        # Later: KOEKKEN, with its own dates and deadlines in services.

    name = models.CharField("Navn", max_length=100, unique=True)
    slug = models.SlugField(unique=True)
    description = models.CharField("Beskrivelse", max_length=255, blank=True)
    kind = models.CharField("Type", max_length=12, choices=Kind.choices, default=Kind.SOENDAG)
    order = models.PositiveSmallIntegerField("Rækkefølge", default=0)
    is_active = models.BooleanField("Aktiv", default=True)
    # The header a printed rota uses for this area ("Stuen, Gang, Toilet"), one per line — what the
    # import matches besides the name.
    import_aliases = models.TextField(
        "Andre navne på listen", blank=True, help_text="Én pr. linje, fx overskriften i PDF'en."
    )

    class Meta:
        ordering = ["order", "name"]
        verbose_name = "rengøringsområde"
        verbose_name_plural = "rengøringsområder"

    def __str__(self) -> str:
        return self.name

    def aliases(self) -> list[str]:
        return [line.strip() for line in self.import_aliases.splitlines() if line.strip()]


class CleaningTask(models.Model):
    area = models.ForeignKey(CleaningArea, on_delete=models.CASCADE, related_name="tasks")
    zone = models.CharField("Zone", max_length=100)  # "Toilet og bad"
    group = models.CharField("Gruppe", max_length=100)  # "Håndvask"
    text = models.CharField("Opgave", max_length=255)
    star_only = models.BooleanField("Kun i (*)-uger", default=False)
    order = models.PositiveIntegerField("Rækkefølge", default=0)
    is_active = models.BooleanField("Aktiv", default=True)

    class Meta:
        ordering = ["area", "order", "id"]
        verbose_name = "opgave"
        verbose_name_plural = "opgaver"

    def __str__(self) -> str:
        return f"{'(*) ' if self.star_only else ''}{self.text}"


class Rota(models.Model):
    name = models.CharField("Navn", max_length=50, unique=True)  # "E26"
    start_date = models.DateField("Første søndag")
    created_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-start_date"]
        verbose_name = "rengøringsliste"
        verbose_name_plural = "rengøringslister"

    def __str__(self) -> str:
        return self.name


class AssignmentStatus(models.TextChoices):
    KOMMENDE = "kommende", "Kommende"
    AFVENTER = "afventer", "Ikke taget endnu"
    IKKE_TAGET = "ikke_taget", "Ikke taget"
    TAGET = "taget", "Taget"
    GODKENDT = "godkendt", "Godkendt"
    MANGLER = "mangler", "Mangler"
    UDBEDRET = "udbedret", "Udbedret – afventer tjek"
    OVERSKREDET = "overskredet", "Frist overskredet"


class CleaningAssignment(models.Model):
    rota = models.ForeignKey(Rota, on_delete=models.CASCADE, related_name="assignments")
    area = models.ForeignKey(CleaningArea, on_delete=models.PROTECT, related_name="assignments")
    date = models.DateField("Dato")
    # Null = nobody on the list that week ("Ingen på listen"), or the resident was deleted.
    resident = models.ForeignKey(
        "residents.Resident",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cleaning_assignments",
        verbose_name="Alumne",
    )
    is_star_week = models.BooleanField("(*)-uge", default=False)
    signed_off_at = models.DateTimeField("Taget", null=True, blank=True)
    # Who pressed the button — differs from `resident` when Inspektionen signs off on their behalf.
    signed_off_by = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    comment = models.CharField("Kommentar", max_length=255, blank=True)  # the paper list's column

    class Meta:
        ordering = ["date", "area__order"]
        constraints = [
            # Two rotas can never both claim the same area on the same Sunday.
            models.UniqueConstraint(fields=["area", "date"], name="uniq_cleaning_area_date"),
        ]
        indexes = [models.Index(fields=["resident", "date"]), models.Index(fields=["date"])]
        verbose_name = "rengøringsvagt"
        verbose_name_plural = "rengøringsvagter"

    def __str__(self) -> str:
        who = self.resident.full_name if self.resident else "Ingen"
        return f"{self.area} {self.date:%d/%m/%Y}: {who}"

    @property
    def done_by(self) -> datetime.datetime:
        """When the cleaning must be done: 22.00 on the day."""
        return at_deadline_hour(self.date)

    def rounds(self) -> list["Inspection"]:
        """Inspections in round order. Sorted in Python so a prefetch_related("inspections") is used."""
        return sorted(self.inspections.all(), key=lambda i: i.round)

    def latest_decided(self) -> "Inspection | None":
        decided = [i for i in self.rounds() if i.outcome]
        return decided[-1] if decided else None

    def current_round(self) -> "Inspection | None":
        """The undecided round being worked on, if any."""
        open_rounds = [i for i in self.rounds() if not i.outcome]
        return open_rounds[-1] if open_rounds else None

    def status(self, now: datetime.datetime | None = None) -> AssignmentStatus:
        now = now or timezone.now()
        latest = self.latest_decided()
        if latest is None:
            if timezone.localdate(now) < self.date:
                return AssignmentStatus.KOMMENDE
            if self.signed_off_at:
                return AssignmentStatus.TAGET
            return AssignmentStatus.IKKE_TAGET if now > self.done_by else AssignmentStatus.AFVENTER
        if latest.outcome == Inspection.Outcome.GODKENDT:
            return AssignmentStatus.GODKENDT
        if latest.fixed_at:
            return AssignmentStatus.UDBEDRET
        if latest.deadline and now > latest.deadline:
            return AssignmentStatus.OVERSKREDET
        return AssignmentStatus.MANGLER


class Inspection(models.Model):
    class Outcome(models.TextChoices):
        GODKENDT = "godkendt", "Godkendt"
        MANGLER = "mangler", "Mangler"

    assignment = models.ForeignKey(CleaningAssignment, on_delete=models.CASCADE, related_name="inspections")
    round = models.PositiveSmallIntegerField("Runde", default=1)  # 1 = Sunday, 2+ = opfølgning
    inspector = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    checked_tasks = models.ManyToManyField(CleaningTask, blank=True, related_name="+")
    note = models.TextField("Noter", blank=True)
    outcome = models.CharField(
        "Resultat", max_length=10, choices=Outcome.choices, blank=True
    )  # "" = in progress
    # The tasks that were not ticked, frozen when the round is decided ([{"zone", "text"}, ...]) so
    # history keeps saying what was missing after Inspektionen edits the task list.
    missing = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)
    deadline = models.DateTimeField("Frist", null=True, blank=True)  # MANGLER only
    fixed_at = models.DateTimeField("Udbedret", null=True, blank=True)  # the alumne says it is fixed

    class Meta:
        ordering = ["assignment", "round"]
        constraints = [
            models.UniqueConstraint(fields=["assignment", "round"], name="uniq_inspection_round"),
        ]
        verbose_name = "rengøringstjek"
        verbose_name_plural = "rengøringstjek"

    def __str__(self) -> str:
        return f"{self.assignment} — runde {self.round}: {self.get_outcome_display() or 'i gang'}"
