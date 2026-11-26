"""What the audit trail keeps of regnskab."""

from audit import trail

trail.actions(
    {
        "regnskab.account_number.updated": "Satte e-conomic kontonr.",
        "regnskab.alumneregninger.sent": "Sendte alumneregninger",
        "regnskab.alumneregning.not_received": "Sendte en alumneregning igen, der ikke kom frem",
    }
)
