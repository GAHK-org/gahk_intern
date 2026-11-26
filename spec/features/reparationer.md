# Feature: Reparationer — the repair board

A kanban board of things broken on the kollegium, at `/reparationer/`. Any resident reports a fault;
Viceværterne and Reppergruppen work it through the columns.

## Who can do what

- Every logged-in resident: see the board and the archive, report a repair (title, place,
  description, up to six photos), and add notes with photos to any repair.
- A note can be deleted by its author, or by Reppergruppen, Inspektionen or an administrator.
- Viceværterne move a repair through every column except "AK projekt" and "Færdig".
- Reppergruppen, Inspektionen and administrators move it anywhere, archive and reopen it, and delete
  it.
- Both crews hand a repair to the other crew, in either direction.

## The board

- Columns, in order: Ny, I gang, Afventer, AK projekt, Godkend af Repper, Færdig.
- A repair has a column and, separately, a crew holding it (Vicevært or Repper). It starts with
  Viceværterne, who triage.
- A card turns yellow after a month in the same column and red after two; "Godkend af Repper" after
  a week and two. A move resets it; a hand-over or a note does not. Færdig is always green.
- Search covers title, place, description and the reporter's name. A filter shows common areas or
  rooms only.
- The board updates live while open.

## Notifications

- A new repair tells every current Vicevært (not the reporter). A hand-over tells the crew that
  received it, never the one letting go. @-mentions in a description or a note tell those mentioned.

## Archive

- A repair in Færdig for 30 days is archived every night: off the board, still in the searchable
  archive and readable on its own page. A manager can archive a finished repair at once, or reopen
  one from the archive.

## Audit trail

- Recorded (audit-trail.md): reporting a repair with its photos, every move and hand-over, archiving
  and reopening, deleting, and notes added and deleted, each by the person who did it.
- The nightly archiving is one event by the system, with how many it archived, naming each repair.
- Left out as fields: when a repair entered its column and when it was archived (the move and archive
  events say so), and created/changed timestamps.

## Decisions

- Column and crew are two separate things: a repair can be "I gang" under either crew.
- Closing a repair and committing it to an AK session are Reppergruppen's calls, so a Vicevært who
  thinks it is done moves it to "Godkend af Repper".
- Finished repairs are archived, not deleted: their history is worth finding later.
