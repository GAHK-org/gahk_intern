"""What the audit trail keeps of facilities and bookings: every booking, approval and cancellation."""

from audit import trail

from .models import Booking, Facility

trail.register(
    Facility,
    "facilities.facility",
    label="Facilitet",
    ignore={"created_at"},
    labels={
        "name": "Navn",
        "description": "Beskrivelse",
        "capacity": "Antal personer",
        "requires_approval": "Kræver godkendelse",
        "is_active": "Kan bookes",
    },
)
trail.register(
    Booking,
    "facilities.booking",
    name=lambda b: f"{b.facility} {b.starts_at:%d.%m.%Y %H.%M}",
    label="Booking",
    ignore={"created_at"},
    labels={
        "facility": "Facilitet",
        "booked_by": "Booket af",
        "starts_at": "Fra",
        "ends_at": "Til",
        "purpose": "Formål",
        "purpose_public": "Formål synligt for alle",
        "event": "Begivenhed",
        "status": "Status",
        "decided_by": "Afgjort af",
        "decided_at": "Afgjort",
        "decision_note": "Begrundelse",
        "cancelled_by": "Aflyst af",
        "cancelled_at": "Aflyst",
    },
)

trail.actions(
    {
        "facilities.facility.created": "Oprettede facilitet",
        "facilities.facility.changed": "Ændrede facilitet",
        "facilities.booking.created": "Bookede",
        "facilities.booking.approved": "Godkendte booking",
        "facilities.booking.rejected": "Afviste booking",
        "facilities.booking.cancelled": "Aflyste booking",
        "facilities.booking.event_synced": "Bookede til begivenhed",
        "facilities.booking.event_released": "Frigav begivenhedens bookinger",
    }
)
