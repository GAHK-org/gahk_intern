# AK-krydser

The bookkeeping of the labour every resident owes the house, counted in krydser.

## Access
- Every logged-in resident sees their own balance and their latest 100 entries, and can register work they have done.
- The AK embedsgruppe sees every current resident's balance and any resident's log, adds or deletes entries for anyone, and sets the monthly schedule.

## Balance
- The balance is the sum of all entries: monthly charges subtract, work adds back.
- It may go negative, which means the resident is behind; the goal is to stay at 0 or above.
- The balance also appears in Regnskab.

## Monthly charge
- Every month, each resident on that month's alumneliste is charged the scheduled number of krydser (2 by default).
- Residents not on the alumneliste are never charged; if someone is removed from the list, their charge for that month is removed.
- The month only starts once its alumneliste exists and the month has begun; a list prepared for next month does not count yet.
- Each resident is charged at most once per month, however often the charge is applied.
- The charge is booked at 04:10 on the 1st; if that is missed (or the list is published later), it is booked on the first internal page load (dashboard or AK pages) after the month starts.

## Schedule
- AK-oversigt shows a 12-month schedule (januar–december): whether each month is charged and how many krydser.
- The schedule is the same every year.
- A charged month costs at least 1 kryds.
- Saving the schedule re-books the current month at once (adding, updating or removing charges); earlier months are never changed.

## Registering work
- A resident registers their own work with a whole number of krydser (at least 1) and a required description; it counts immediately, with no approval step.
- An officer can add any non-zero whole number to a resident's log, with an optional note: positive counts as work, negative as a manual adjustment.
- An officer can delete any entry from a resident's log.

## AK-oversigt
- Lists residents on the current month's alumneliste, lowest balance first, each linking to their log.
- Each log shows the balance and the latest 200 entries, with who made each change.

## Decisions
- The balance is a ledger of entries rather than a stored total, so it is always consistent and charges, work and adjustments are all just entries.
- Each monthly charge is tied to its month, not to when it was written, so re-running a month can never charge twice.
- The schedule is per calendar month and repeats yearly, so officers set it once.
- A missed scheduled run would leave every balance wrong, so the first page load of a new month books it as a backstop.
- Saving the schedule only re-books the current month, so settled balances stay as they are.
