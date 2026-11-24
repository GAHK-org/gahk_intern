"""What the audit trail keeps of AK (aktivitetskrydser): every line of the ledger and the monthly
schedule, and who wrote it.

The ledger is append-only in the feature too, but a line can be deleted by an AK officer and the
monthly charge rewrites its own lines; the trail is what still says they were there.
"""

from audit import trail
from core.danish import MONTHS

from .models import AkAutoApply, AkEntry, AkMonthlyCharge

trail.register(
    AkEntry,
    "ak.entry",
    name=lambda entry: f"{entry.resident.full_name}: {entry.delta:+d} ({entry.get_kind_display()})",
    label="AK-post",
    # When the line was written: the event's own time.
    ignore={"created_at"},
    labels={
        "resident": "Beboer",
        "delta": "Krydser",
        "kind": "Slags",
        "reason": "Begrundelse",
        "year": "År",
        "month": "Måned",
        "created_by": "Skrevet af",
    },
)
trail.register(
    AkMonthlyCharge,
    "ak.monthly_charge",
    name=lambda charge: f"AK-træk i {MONTHS[charge.month]}",
    label="Månedligt AK-træk",
    # Who saved the schedule and when: the event says so.
    ignore={"updated_at", "updated_by"},
    labels={"month": "Måned", "krydser": "Krydser", "active": "Trækkes"},
)
trail.exempt(AkAutoApply, "which month was last charged, kept by the monthly charge; that is its own event")

trail.actions(
    {
        "ak.labour.logged": "Registrerede sit AK-arbejde",
        "ak.entry.added": "Tilføjede en AK-post",
        "ak.entry.deleted": "Slettede en AK-post",
        "ak.schedule.saved": "Gemte AK-skemaet",
        "ak.schedule.month_added": "Oprettede månedens AK-træk med standardværdien",
        "ak.month.charged": "Trak månedens AK-krydser",
    }
)
