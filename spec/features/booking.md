# Booking — facilities and who has them when

At `/facilities/`, "Booking" in the sidebar under Værktøjer. Open to every resident.

A **facility** is anything the house lends out by the hour or the day: Festsalen, Spisesalen,
Musiklokalet, the guest room, the cargo bike. A **booking** holds one facility for one stretch of
time, either privately ("mine forældre overnatter") or for an event in Begivenheder.

## Who can do what

| Action | Who |
| --- | --- |
| See every facility's week, and who has booked what when | every resident |
| Book a facility privately | every resident |
| Read a booking's purpose | the booker and the managers; everyone for an open event's booking |
| Cancel a private booking | the booker, until it is over; a manager, any time before it ends |
| Approve or reject a request | managers |
| Add, edit or retire a facility; see its full history | managers |

**Managers** are Inspektionen and administrators (the same pair that moderates the house's other
shared things).

## Booking rules

- No overlaps: a facility is held by at most one booking at any instant. Touching is fine (one ends
  at 22.00, the next starts at 22.00).
- A request waiting for approval holds the time too. First come, first served applies to requests;
  a manager rejects one to free the slot.
- No booking in the past, and none longer than 14 days.
- A retired facility ("Kan bookes" off) takes no new bookings; its old ones stay.

## Approval

Per facility: with **Kræver godkendelse** on, a resident's booking starts as *Afventer godkendelse*
and the managers are notified. Approving or rejecting (with an optional reason) notifies the booker.
A manager's own booking is confirmed at once. A manager who cancels somebody's booking can give a
reason, and the booker is told.

## Events

- The event form has **Book lokaler**: tick facilities, and they are booked for the event's time.
  The event then needs an end time. A facility already taken is refused with who has it and when.
- The booking follows the event: moving it moves the booking (and sends an approved one back for
  approval), renaming it renames the booking, unticking a facility releases it, and cancelling or
  deleting the event releases them all.
- An empty "Sted" is filled in with the booked facilities.
- The event page lists its facilities with their status; a booking page links back to an open event.
- An event's booking is changed through the event, not on the booking page.
- An invite-only event's booking says "Lukket begivenhed", never the event's title.

## Pages

- **Booking**: every facility, free now or busy until when, and its next booking; your next bookings.
- **A facility**: its description and a week, Monday to Sunday, each day a 24-hour bar with the
  bookings listed beneath. A booking past midnight shows on each day it touches.
- **Mine bookinger**, **Anmodninger** (managers), **Historik** per facility (managers).

## Retention

Bookings are **kept forever**, including cancelled and rejected ones. An event is purged a week after
it is held; its booking stays, with the event link gone and the purpose kept.

## Decisions

- **One table for private and event bookings.** They compete for the same hours, so the overlap
  rule has to see both.
- **Overlaps are prevented under a row lock on the facility**, not only by the form, so two people
  booking the last free evening at once cannot both get it.
- **"Who and when" is public, the purpose is not.** A shared room only works if people can find out
  who has it and ask; why they have it is their own business.
- **Private events stay private from managers.** Begivenheder promises that only invitees see an
  invite-only event, so its booking carries no title.
- **Kept forever, unlike events.** "Who had Festsalen the night the window broke?" is asked months
  later.
- **`facilities`, not `rooms`**: that app is the residents' own rooms.
