"""Booking — the kollegium's shared rooms and things, and who has them when.

A facility is anything the house lends out by the hour or the day: Festsalen, Musiklokalet, the guest
room. A booking holds one facility for one stretch of time, either for an event (begivenheder) or
for something private ("mine forældre overnatter").

Named `facilities`, never `rooms`: that app is the residents' own rooms (søg værelse, værelsestjek).

TWO KINDS OF BOOKING, ONE TABLE. An event's booking and a private one compete for the same hours, so
the overlap rule has to see both; a second table would make every "is it free?" a union. An event's
booking is the row with `event` set, and it follows the event: its times, its title, its
cancellation (facilities.api, called from events).

KEPT FOREVER, unlike the events they belong to. "Who had Festsalen the night the window broke?" is
asked months later, so a booking outlives its event: `event` goes NULL when the event is purged, and
`purpose` keeps what it was for.
"""

import datetime

from django.conf import settings
from django.db import models

# The longest single booking. A guest room for a fortnight is real; a typo in the year that holds
# Festsalen until next spring is not, and nobody would notice it until they tried to book.
MAX_BOOKING_LENGTH = datetime.timedelta(days=14)

# What a booking for an invite-only event says it is for. The event's own title stays with the event:
# begivenheder promises that a non-invitee (moderators included) cannot see a private event, and the
# managers who see every booking's purpose are exactly those moderators.
PRIVATE_EVENT_PURPOSE = "Lukket begivenhed"


class Facility(models.Model):
    """One room or thing the house lends out."""

    name = models.CharField(max_length=100, unique=True, verbose_name="Navn")
    description = models.TextField(
        blank=True,
        verbose_name="Beskrivelse",
        help_text="Hvor den er, hvor nøglen ligger, og hvad man skal huske bagefter.",
    )
    capacity = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="Antal personer",
        help_text="Lad stå tomt, hvis det ikke giver mening.",
    )
    # Per facility, not per house: the guest room can be first come, first served while Festsalen
    # needs Inspektionen to say yes.
    requires_approval = models.BooleanField(
        default=False,
        verbose_name="Kræver godkendelse",
        help_text="Bookinger venter, til Inspektionen har godkendt dem.",
    )
    # Retired rather than deleted: its bookings are kept forever (see the module docstring), and they
    # PROTECT it.
    is_active = models.BooleanField(default=True, verbose_name="Kan bookes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Facilitet"
        verbose_name_plural = "Faciliteter"

    def __str__(self) -> str:
        return self.name


class Status(models.TextChoices):
    PENDING = "pending", "Afventer godkendelse"
    CONFIRMED = "confirmed", "Bekræftet"
    REJECTED = "rejected", "Afvist"
    CANCELLED = "cancelled", "Aflyst"


# The statuses that hold the facility. A request waiting for approval holds it too: first come, first
# served applies to requests as well, and two people planning a party on one Saturday is the thing
# this exists to prevent. A manager rejects the request to free the slot.
HOLDING = (Status.PENDING, Status.CONFIRMED)


class BookingQuerySet(models.QuerySet["Booking"]):
    def holding(self) -> "BookingQuerySet":
        return self.filter(status__in=HOLDING)

    def overlapping(self, starts_at: datetime.datetime, ends_at: datetime.datetime) -> "BookingQuerySet":
        """Bookings sharing any instant with [starts_at, ends_at). Touching is not overlapping: one
        booking may end at 22.00 and the next start at 22.00."""
        return self.filter(starts_at__lt=ends_at, ends_at__gt=starts_at)


class Booking(models.Model):
    """One facility, held for one stretch of time."""

    facility = models.ForeignKey(
        Facility, on_delete=models.PROTECT, related_name="bookings", verbose_name="Facilitet"
    )
    # PROTECT: the history is the point, and a resident row is not deleted in normal use.
    booked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", verbose_name="Booket af"
    )
    starts_at = models.DateTimeField(verbose_name="Fra")
    ends_at = models.DateTimeField(verbose_name="Til")
    purpose = models.CharField(max_length=140, blank=True, verbose_name="Formål")
    # Whether every resident may read `purpose`. True only for an open event's booking, whose title
    # the whole house can see on the event anyway; a private booking's purpose is for its booker and
    # the managers.
    purpose_public = models.BooleanField(default=False)
    # SET_NULL, not CASCADE: an event is purged a week after it is held, and its booking is kept.
    # Every live path releases the booking before the event goes (facilities.api); this only catches
    # the purge of events already over.
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Begivenhed",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.CONFIRMED, verbose_name="Status"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Afgjort af",
    )
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name="Afgjort")
    # Why a manager rejected or cancelled it, told to the booker.
    decision_note = models.CharField(max_length=200, blank=True, verbose_name="Begrundelse")
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Aflyst af",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True, verbose_name="Aflyst")

    objects = BookingQuerySet.as_manager()

    class Meta:
        ordering = ["starts_at", "pk"]
        verbose_name = "Booking"
        verbose_name_plural = "Bookinger"
        indexes = [models.Index(fields=["facility", "starts_at"], name="booking_facility_starts_idx")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")), name="booking_ends_after_start"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.facility} {self.starts_at:%d.%m.%Y %H.%M}"

    @property
    def is_holding(self) -> bool:
        return self.status in HOLDING
