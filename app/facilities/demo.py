"""Demo facilities and bookings for `manage.py seed_demo`, run after events.demo.

Each booking makes one state visible on a fresh checkout: a free and a busy facility on the
overview, a request waiting in Anmodninger, a rejected and a cancelled one in somebody's history, a
booking past midnight (two bars on the week), and an event's booking (the room on the event page).
Written directly, not through services: seed_demo runs unaudited and sends no notifications.
"""

import random
from datetime import datetime, timedelta

from django.utils import timezone

from events.models import Event
from residents.models import Resident

from .models import Booking, Facility, Status

FACILITIES = [
    # (name, capacity, requires_approval, description)
    ("Festsalen", 120, True, "Nøglen hentes hos Inspektionen. Gulvet skal fejes og vaskes bagefter."),
    ("Spisesalen", 80, False, "Stolene stilles op igen efter brug."),
    ("Musiklokalet", 8, False, "Ingen forstærkere efter kl. 22 på hverdage."),
    ("Gæsteværelset", 2, False, "Sengetøj ligger i skabet. Husk at tage det af igen."),
    ("Ladcyklen", None, False, "Står i cykelkælderen. Koden til låsen er 1908."),
]


def _at(now: datetime, days: int, hour: int) -> datetime:
    """`days` from now at a round LOCAL hour (see events.demo._at for why it localises first)."""
    return (timezone.localtime(now) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def seed(residents: list[Resident], now: datetime, rng: random.Random) -> int:
    facilities = {
        name: Facility.objects.create(name=name, capacity=cap, requires_approval=approval, description=text)
        for name, cap, approval, text in FACILITIES
    }
    people = rng.sample(residents, 6)

    def book(
        facility: str, who: int, start: datetime, end: datetime, purpose: str, **extra: object
    ) -> Booking:
        return Booking.objects.create(
            facility=facilities[facility],
            booked_by=people[who],
            starts_at=start,
            ends_at=end,
            purpose=purpose,
            **extra,
        )

    this_hour = timezone.localtime(now).replace(minute=0, second=0, microsecond=0)
    book(
        "Musiklokalet", 0, this_hour - timedelta(hours=1), this_hour + timedelta(hours=2), "Øvning"
    )  # busy now
    book("Musiklokalet", 1, _at(now, 1, 19), _at(now, 1, 22), "Bandprøve")
    book("Gæsteværelset", 2, _at(now, 2, 15), _at(now, 4, 11), "Mine forældre overnatter")
    book("Festsalen", 3, _at(now, 9, 20), _at(now, 10, 2), "30-års fødselsdag", status=Status.PENDING)
    book(
        "Festsalen",
        4,
        _at(now, 6, 18),
        _at(now, 6, 23),
        "Fest for holdet",
        status=Status.REJECTED,
        decided_by=people[5],
        decided_at=now,
        decision_note="Gulvet bliver lakeret den uge",
    )
    book(
        "Ladcyklen",
        0,
        _at(now, 3, 10),
        _at(now, 3, 14),
        "Flyttehjælp",
        status=Status.CANCELLED,
        cancelled_by=people[0],
        cancelled_at=now,
    )
    book("Spisesalen", 1, _at(now, -12, 17), _at(now, -12, 22), "Kagekonkurrence")

    dinner = Event.objects.filter(title="Fællesspisning", ends_at__isnull=False).order_by("starts_at").first()
    if dinner is not None and dinner.ends_at is not None:
        Booking.objects.create(
            facility=facilities["Spisesalen"],
            booked_by=dinner.organiser,
            starts_at=dinner.starts_at,
            ends_at=dinner.ends_at,
            purpose=dinner.title,
            purpose_public=True,
            event=dinner,
        )
        if not dinner.location:
            dinner.location = "Spisesalen"
            dinner.save(update_fields=["location"])
    return Booking.objects.count()
