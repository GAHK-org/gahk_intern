from django.db import models
from django.utils import timezone

from residents.models import Resident


class EconomicLedgerEntry(models.Model):
    """One posting from e-conomic's ledger, mirrored locally (F-010 regnskab, alumneregning)."""

    resident = models.ForeignKey(Resident, on_delete=models.PROTECT, related_name="economic_entries")
    entry_number = models.PositiveIntegerField()  # e-conomic's entryNumber, or journalEntryNumber if draft
    is_draft = models.BooleanField(default=False)
    date = models.DateField()
    text = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)  # + = resident owes GAHK (e-conomic's sign)
    entry_type = models.CharField(max_length=40, blank=True)
    voucher_number = models.PositiveIntegerField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    synced_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["date", "entry_number"]
        indexes = [models.Index(fields=["resident", "date"])]
        constraints = [
            # Scoped by resident, not just (entry_number, is_draft): e-conomic's numbering is not
            # necessarily company-wide, and ledger.sync_entries's own dedup is per-resident too.
            models.UniqueConstraint(
                fields=["resident", "entry_number", "is_draft"], name="uniq_entry_number_per_resident"
            )
        ]

    def __str__(self) -> str:
        return f"{self.resident.full_name} — {self.date} {self.amount} kr (e-conomic #{self.entry_number})"


# string.Template source for the default alumneregning mail body (regnskab.alumneregning._body
# substitutes $FORNAVN/$SALDO_LINE/$REG_NR/$KONTO_NR). Matches the wording the legacy script's
# message.txt used, kept as the starting point regnskab edits from in AlumneregningSettings.
DEFAULT_ALUMNEREGNING_MESSAGE = (
    "Hej $FORNAVN,\n"
    "\n"
    "Os i Regnskabsgruppen sender dig hermed (vedhæftet) din alumneregning.\n"
    "\n"
    "$SALDO_LINE\n"
    "\n"
    "Vi beder om at du generelt forsøger at holde din gæld så lav som muligt.\n"
    "\n"
    "Hvis du skylder mere end 600 kr. beder vi om at du indbetaler inden for udgangen af "
    "indeværende uge. Skulle du have brug for en speciel aftale om afbetaling, fordi din gæld er "
    "for stor til, at du kan betale den af på én gang, så bedes du kontakte det siddende "
    "inspektionsmedlem i Regnskabsgruppen.\n"
    "\n"
    "UDFLYTTER-INFO: Denne regning skal ikke betales.\n"
    "I tilfælde af at du skylder alumnekassen penge bliver dette fratrukket dit depositum som PKS "
    "tilbagebetaler snarest.\n"
    "\n"
    "------------------------------------------------------------------------\n"
    "\n"
    "På vegne af Regnskabsgruppen på G. A. Hagemanns Kollegium\n"
    "\n"
    "Reg. nr. $REG_NR Konto nr. $KONTO_NR\n"
    "\n"
    "------------------------------------------------------------------------"
)


class AlumneregningSettings(models.Model):
    """The alumneregning mail body + følgebrev PDF, editable by regnskab (F-010)."""

    message_template = models.TextField(default=DEFAULT_ALUMNEREGNING_MESSAGE)
    followup_pdf = models.FileField(upload_to="alumneregning/", blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Alumneregning-indstillinger"
        verbose_name_plural = "Alumneregning-indstillinger"

    def __str__(self) -> str:
        return "Alumneregning-indstillinger"

    @classmethod
    def get_solo(cls) -> "AlumneregningSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class AlumneregningSend(models.Model):
    """One billing mail actually attempted to one alumne for one period (F-010). Lets a retried or
    re-queued send (the Celery task re-run, the button pressed twice) skip whoever already got this
    exact period's bill (outcome="sent") rather than mailing them again, and doubles as the audit
    trail/status page for what went out and when — see regnskab.alumneregning.send_alumneregninger."""

    resident = models.ForeignKey(Resident, on_delete=models.PROTECT, related_name="alumneregning_sends")
    date_from = models.DateField()
    date_to = models.DateField()
    outcome = models.CharField(max_length=10, choices=[("sent", "Sendt"), ("failed", "Fejlede")])
    detail = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["resident__first_name", "resident__last_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["resident", "date_from", "date_to"], name="uniq_alumneregning_send_per_period"
            )
        ]

    def __str__(self) -> str:
        return (
            f"{self.resident.full_name} — {self.date_from} til {self.date_to} ({self.get_outcome_display()})"
        )
