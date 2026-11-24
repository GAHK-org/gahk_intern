"""What the audit trail keeps of regnskab: the alumneregning mail and følgebrev, and the sends.

What each alumne was sent is the send's own status record (AlumneregningSend, the status page); the
trail has regnskab pressing send, what each run of the job made of it, and a statement sent again.
"""

from audit import trail

from .models import AlumneregningSend, AlumneregningSettings, EconomicLedgerEntry

trail.register(
    AlumneregningSettings,
    "regnskab.alumneregning_settings",
    name=lambda _settings: "Alumneregning-indstillinger",
    label="Alumneregning-indstillinger",
    ignore={"updated_at"},
    labels={"message_template": "Mailtekst", "followup_pdf": "Følgebrev"},
)
trail.exempt(
    AlumneregningSend,
    "the send's delivery bookkeeping, one row per alumne and period, rewritten by retries and shown on "
    "the status page; the job's run is an event (regnskab.alumneregninger.made)",
)
trail.exempt(
    EconomicLedgerEntry,
    "a local copy of e-conomic's ledger, rewritten from e-conomic by every send; e-conomic is the record",
)

trail.actions(
    {
        "regnskab.account_number.updated": "Satte e-conomic kontonr.",
        "regnskab.alumneregninger.sent": "Sendte alumneregninger",
        "regnskab.alumneregninger.made": "Lavede alumneregninger",
        "regnskab.alumneregning.not_received": "Sendte en alumneregning igen, der ikke kom frem",
        "regnskab.alumneregning_settings.updated": "Rettede alumneregningens mail og følgebrev",
    }
)
