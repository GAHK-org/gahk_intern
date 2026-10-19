# Reparationer

A kanban board where residents report things that are broken and the Viceværter and Reppergruppen track the repair to the end.

## Access
- Every logged-in resident can see the board and the archive, report a repair, and add notes.
- Viceværter (the month's Vicevært embedsgruppe) can move tickets between columns, except into "AK projekt" and "Færdig", and hand tickets between the two crews.
- Reppergruppen (the Repperne embedsgruppe), Inspektion and Administrator can do everything: every column, handoffs, archive, reopen and delete.

## Reporting
- A report has a title ("Hvad er der galt?", required), an optional place and an optional description.
- The place is chosen from a list: named common areas first (Vaskerummet, Festsalen, Ølkælderen, …), then every room floor by floor, each floor ending with its gang, toilet and køkken. There is no free-text place.
- A new ticket starts in "Ny" with Vicevært responsible.

## The board
- Columns in order: Ny, I gang, Afventer, AK projekt, Godkend af Repper, Færdig.
- Each card shows title, place, description, who is responsible, who reported it and when, and the number of notes.
- Search matches title, place, description and the reporter's name, and shows the number of hits.
- A filter shows all tickets, only common areas, or only rooms.
- Those allowed to move tickets drag a card to another column (on a phone: press and hold, then drag). A refused move, e.g. a Vicevært dropping on a manager-only column, puts the card back with the reason ("Kun Reppergruppen kan flytte en sag hertil.").
- Everyone else sees the board read-only.
- The board updates live as tickets are reported, moved, handed over, noted on or deleted, keeping the search and filter; it waits while a card is being dragged. Tickets archived by the nightly job leave on the next update or page load.

## A ticket
- The ticket page shows everything on the card plus its notes, and carries the move buttons (only the columns the viewer may use), the handoff button to the other crew, and the manager actions.
- Responsible and column are independent: a ticket can be "I gang" under either crew.
- Handing over goes both ways: Vicevært → Repper and back.
- Any resident can add a note. A note can be deleted by its author or a manager.
- A manager can delete a ticket outright.

## Archive
- A ticket that has been in Færdig for 30 days is archived automatically by a nightly job (03:50). It leaves the board but is kept.
- A manager can archive a Færdig ticket at once, or reopen an archived ticket, which returns it to Færdig on the board.
- The archive is a searchable list, newest archived first; archived tickets still open normally.

## Notifications
Sent through Notifications, under the Reparationer category (see notifications.md).
- A new report notifies the current Viceværter (not the reporter).
- A handoff to Reppergruppen notifies the current Reppere; a handoff back notifies the current Viceværter. The crew letting go is never notified.
- Only this month's holders of the role, administrators and superusers are notified.
- Column changes and notes send no notifications.

## Decisions
- "Færdig" and "AK projekt" are manager-only: closing a ticket and committing Reppergruppen's AK hours are their calls. "Godkend af Repper" exists so a Vicevært can mark something done without closing it.
- Responsible is separate from the column because who owns a ticket and how far it has come are independent.
- Either crew may hand a ticket either way: a handoff nobody can undo is one people hesitate to make.
- Finished tickets are archived, not deleted, because a repair's history is worth keeping searchable.
- Notifications go to current role holders only, so someone who rotated off a crew stops being paged.
