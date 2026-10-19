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
- A month is charged once it is over: each resident on that month's final alumneliste is charged the scheduled number of krydser (2 by default).
- Residents not on the month's list are never charged, so someone removed from it before the month ends is not charged for it.
- The month charged is the calendar month that just ended, whether or not the next month's list has been published.
- Each resident is charged at most once per month, however often the charge is applied.
- The charge is booked at 04:10 on the 1st of the next month; if that is missed, it is booked on the first dashboard or AK page load after.

## Schedule
- AK-oversigt shows a 12-month schedule (januar–december): whether each month is charged and how many krydser.
- The schedule is the same every year.
- A charged month costs at least 1 kryds.
- Saving the schedule books nothing. The month in progress is charged with the schedule as it stands when the month ends; months already charged are never changed.

## Registering work
- A resident registers their own work with a whole number of krydser (at least 1) and a required description; it counts immediately, with no approval step.
- An officer can add any non-zero whole number to a resident's log, with an optional note: positive counts as work, negative as a manual adjustment.
- An officer can delete any entry from a resident's log.

## AK-oversigt
- Lists residents on the current month's alumneliste, lowest balance first, each linking to their log.
- Each log shows the balance and the latest 200 entries, with who made each change.

## Decisions
- The balance is a ledger of entries rather than a stored total, so it is always consistent and charges, work and adjustments are all just entries.
- A month is charged after it ends, from its final list. Charging it as it began used the list copied forward from the month before, so a resident who moved out and was removed later kept a charge for a month they did not live here.
- Each monthly charge is tied to its month, not to when it was written, so re-running a month can never charge twice.
- The schedule is per calendar month and repeats yearly, so officers set it once.
- A missed scheduled run would leave every balance wrong, so the first page load of a new month books it as a backstop.
- Saving the schedule changes no charges, so settled balances stay as they are.
