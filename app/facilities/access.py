"""Who may do what with facilities and bookings.

Every resident may book. Inspektionen and administrators MANAGE: they add and edit facilities,
approve or reject requests, and may cancel anybody's booking. The same pair that moderates the
house's other shared things (residents.permissions.MODERATION_ROLES).

What others see of a booking is "who and when": a booking is never secret, because the point of a
shared room is that people can find out who has it and ask. The PURPOSE is the booker's and the
managers' — except for an open event's booking, whose title the whole house can read on the event.
"""

from django.http import HttpRequest

from core.clock import current_datetime
from residents.models import Resident
from residents.permissions import MODERATION_ROLES, has_active_role, request_has_role

from .models import Booking, Status

MANAGER_ROLES = MODERATION_ROLES


def is_manager(resident: Resident) -> bool:
    """Real roles: what services ask (does this booking need approval?), never affected by preview."""
    return has_active_role(resident, *MANAGER_ROLES)


def request_is_manager(request: HttpRequest) -> bool:
    """Effective roles: what views ask, so "Se siden som beboer" shows a beboer's page."""
    return request_has_role(request, *MANAGER_ROLES)


def can_see_purpose(booking: Booking, resident: Resident, *, manager: bool) -> bool:
    return booking.purpose_public or manager or booking.booked_by_id == resident.pk


def can_cancel(booking: Booking, resident: Resident, *, manager: bool) -> bool:
    """The booker cancels their own until it is over; a manager cancels anybody's.

    An event's booking is the event's: its hosts change it by editing or cancelling the event, so the
    booking page sends them there rather than offering a second way that leaves the event saying
    "Festsalen" after the room is gone.
    """
    if not booking.is_holding or booking.ends_at <= current_datetime():
        return False
    if manager:
        return True
    return booking.event_id is None and booking.booked_by_id == resident.pk


def can_decide(booking: Booking, *, manager: bool) -> bool:
    return manager and booking.status == Status.PENDING
