# Feature: Kitchen duties

The dorm's kitchen operates on residents helping at the kitchen.

Working in the kitchen, a resident will earn a certain amount of kitchen duty points (køkkenkryds) for each shift.


## Kitchen duty point account
Implement a ledger based kitchen duty point account for each resident.
Each transaction should be documented (like a bank account). E.g., balance before, amount and balance after (include message that describes reason for transaction).

Kitchen duty points are integers.


## Staying neutral

A resident should aim to have a positive or zero balance.
Each month, all residents should automatically be subtracted 4 points from their account.


### Balance forecast
A resident's account page shows a diagram predicting their balance up to the end of the shift calendar, assuming nothing changes:
- Each shift they hold adds its points when it ends, for the spots they hold. The forecast is conservative: a lone holder of a multi-spot shift is not counted for the free spots, although they are given them if nobody else signs up, as somebody else may. A shift put for sale counts until it is sold; a cancelled shift does not count.
- The monthly charge is subtracted on the 1st of each month: for a month whose alumneliste is published, if they are on it; otherwise if they live here now and their move-out date is not before that month.

It shows the balance now, the lowest point, the balance in 30 days and at the end of the calendar. 30 days is the window in which a shift can no longer be unenrolled from, only sold, so that part of the forecast is binding; beyond it shifts can still be unenrolled freely, so the rest is tentative. The diagram marks where the binding part ends and draws the tentative part dashed.

The diagram has three zones: green (3 points or more), yellow (0 to 2, both included) and red (below 0). Under -6 the red zone turns deeper and hatched, the line turns red, and a banner above the diagram asks the resident to take a shift.
The same steps are available as a table under the diagram.

## Shifts and shift calendar
A shift is taken by one or more residents (amount of spots).
A shift with multiple spots may be taken by the same resident. In this case, the resident will receive the total amount of points for the specifed amount of spots.

The standard week calendar is the following:

Mon-fri:
- Morning duty (6:30 to 7:30, 1 spot), worth 1 point pr. spot
- Midday duty (13:00 to 14:00, 1 spot), worth 1 point pr. spot
- Evening duty (17:30 to 21:00, 2 spots), worth 3 points pr. spot (4 points pr. spot on fridays)

Sat-sun:
- Morning duty (8:00 to 10:00, 1 spot), worth 1 point pr. spot
- Midday duty (13:00 to 14:00, 1 spot), worth 1 point pr. spot
- Evening duty (19:00 to 20:00, 1 spot), worth 1 point pr. spot on saturdays and 2 points pr. spot on sundays

The calendar is available 3 months into the future.

The calendar is public for all residents (including points, who has taken the spots etc...).

Points are awarded by transaction at the time a shift ends.

### Integration to private calendar
When a resident is assigned to a shift, the shift should appear in the residents private calendar (iCal).


## Administration and exceptions
Users with administrative access (administrators or members of the Køkkengruppe) are considered administrators in this context.

Administrators may manually disable specific kitchen shifts in the calendar (only shifts in the future). Include description/reason for why shift is disabled.

Administrators may create special non-reccuring shifts that also appear in the shift calendar (include description of shift, points per spot, amount of spots).

Administrators may make manual transactions of points for any user (include description/reason).

If it has been deemed that a resident has not taken their shift, an administrator may mark the shift as "not done/absent". This option is available for shifts occuring in the past 7 days.
A transcation will revert the obtained points plus a fine. By default the fine is equivalent to the amount of points the shift is worth, however, administrators may adjust the fine.

Administrators may add bonus points to specific shifts, that are awarded on top.

Members of Inspektionen may see the overview and every resident's forecast and statement, but nothing else administrative: no special shifts, cancelling, bonus, absence or manual transactions.

### Overview
An overview page for administrators and Inspektionen, covering everyone on this month's alumneliste:
- Figures: residents, how many are in minus now (and under -6), how many will be in minus in 30 days and at the end of the calendar by the forecast, and unmanned shifts in the next 7 days (and shifts for sale).
- The distribution over the zones now, in 30 days and at the end of the calendar.
- Low balance now: residents on 2 points or less, judged twice: whether the forecast has them at 0 or more in 30 days (binding), and at the end of the calendar (tentative). A resident who is short in 30 days but fine at the end has booked shifts further out, which they may still drop.
- All residents, lowest balance first: balance now, a small forecast diagram, the balance in 30 days and at the end of the calendar, the shifts they hold and their next shift.

Everything tentative (the end-of-calendar figures and the forecast after 30 days) is drawn outlined or dashed, so it is never mistaken for the binding figures.

Each resident links to a page with their forecast, upcoming shifts and statement; administrators also make manual transactions there.


## Assignment of shifts
A resident may self enroll in any unoccupied future shift.
If the shift is more than 30 days into the future, the resident may unenroll without any consequences.

### Shifts with multiple spots
If a shift is only assigned to a single resident at the time the shift ends, the assigned resident will automatically be assigned to all remaining spots. If a shift has multiple assigned residents, but remaining spots, nothing happens.

If a resident has self-enrolled multiple spots on the same shift, another resident may override assignment and self-enroll in the shift. This shall happen before the shift starts.

### Trading shifts
If a resident has signed up for a shift, but they for some reason wish not to take the shift, they may put up their shift "for sale".

The price is the initial amount of awarded points for the shift. Optionally, the seller may offer up to 3 of their own points in addition if they deem it necessary. These points are subtracted from their account if the shift is traded.


### Market page
The market page should consist of two parts.
At the top, all shifts put for sale will appear.
Underneath, any unassigned shifts within the next 72 hours.
Last, the calendar view, allowing signing up for shifts in the greater future.


## Notifications
Sent under the notification category Køkkenvagter, so each resident chooses in-app, push and vibration on the notification settings page. A notification disappears when the shift it is about has started.

All residents will be notified if:
- A shift is put for sale and the shift takes place within the next 7 days
- There are any unassigned shifts within the next 24 hours

All residents with a negative balance will be notified if:
- There are any unassigned shifts within the next 72 hours

All residents with a negative balance under -10 will be notified if:
- There are any unassigned shifts within the next 7 days (notified weekly)

