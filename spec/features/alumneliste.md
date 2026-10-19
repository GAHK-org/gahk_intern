# Alumneliste

The monthly list of who lives in which room and belongs to which embedsgruppe and rengøring. It serves as the resident directory and decides who has which role on the site.

## Access
- Every logged-in resident can browse, search, sort and export the directory, including past months.
- The Indstilling role edits the monthly list, adds residents and edits a resident's core data. Only Indstillingen sees the room-clash warning and the ✎ edit links.
- The Regnskab role sees the move-out settlement page.
- Administrators hold every role.

## The month in effect
- The list in effect is the newest one whose month has already begun. Before any list exists, the current calendar month counts.
- A list prepared for next month changes nothing, including access, until that month begins.
- If nobody prepares a new list, last month's stays in effect, roles included. Nothing runs automatically at month rollover.

## Directory
- Shows the month in effect by default. A picker offers every month that has a list, newest first, including a next-month list that is still being prepared.
- Columns: name (with profile picture and a link to the profile), room, embedsgruppe, rengøring, fylgje, birthday, move-in date, study, phone and email.
- Fylgje shows the linked resident, or else the free-text fylgje.
- Live search on first name, last name, study or email.
- Sorted by name by default. Clicking a column header sorts by it (name, room, embedsgruppe, rengøring, birthday, move-in or study), and clicking again reverses the order.
- Badges mark the oldest and the youngest resident (by birthday) and whoever has lived here longest (by move-in date). The same badges appear on their profile page.
- Export: the list as shown (month, search and sort) as CSV (opens correctly in Excel with Danish characters) or as an Excel workbook, with the same columns. Cells that would start a spreadsheet formula are neutralised.
- If two residents share a room in the viewed month, Indstillingen gets a warning naming the rooms. For the month in effect or next month it links to the editor on that month.

## Editing the list (Indstillingen)
- The editor works on next month by default. It can also switch to the month in effect, so a mistake in the live list can be fixed. No other month can be edited.
- When editing the month in effect, the page warns that changes take effect immediately, access included.
- If next month has no list yet, one button copies the current list over: rooms, embedsgrupper, rengøring and the roles that come with them.
- For each resident: change room, embedsgruppe and rengøring, or remove them from the month.
- A save is all-or-nothing. It is refused, with nothing saved and the edits kept on screen, if:
  - two residents share a room, or
  - an embedsgruppe or rengøring with a fixed size does not have exactly that many members (size 0 means no limit).
- On the month in effect you cannot remove yourself, nor move yourself out of the embedsgruppe that gives you Indstilling access (an administrator can); either change is refused with a message. Next month has no such limit.
- Removing a resident also removes their roles for that month.

## Adding residents
- Add an existing resident to the month with a room, an embedsgruppe and a rengøring. This is refused if the room is already taken that month.
- Create a new resident with first name, last name, email, room, embedsgruppe and rengøring, and optionally phone, birthday and fylgje. This is refused if the email already exists (in any letter case), the room is taken, or the birthday is not a valid date or lies in the future.
- A new resident has no password. They get a welcome email with a link to set one, and a pointer to "glemt kodeord" in case the link has expired.
- If the welcome email fails, the resident is still created and the editor is told to have them use "glemt kodeord".
- A refused add keeps what was typed.

## Roles from the list
- A privileged embedsgruppe grants its role for that month: Indstillingen, Inspektionen, Køkkengruppen, AK-gruppen, Ølkælderen, Regnskabsgruppen, PR-gruppen, Repperne, Vicevært and Fotogruppen. Every other embedsgruppe grants nothing.
- Each save in the editor recalculates a resident's group roles for that month from their embedsgruppe. Administrator is not an embedsgruppe; a resident who is an administrator in the month in effect keeps that role on the edited list.
- Gaining the administrator role for the first time starts a request that a second administrator must approve before it gives Django-admin access. The other administrators are emailed.
- Anyone who holds any role in any month can enter the Django admin; it then decides what they may do.

## Editing a resident (Indstillingen)
- The ✎ next to a name opens the resident's core data: name, email, phone, birthday, study, move-in and move-out dates, fylgje (any other resident) and free-text fylgje.
- After saving, Indstillingen returns to the directory with the same month, search and sort.

## Regnskab (move-out settlement)
- For a chosen month, the page lists residents who were on the previous month's list but are not on this one, with the room they left, their AK balance (krydser) and their ølkælder balance. This is what the deposit is settled from.
- A search finds any resident, current or former, by name or email and shows the same balances.

## Decisions
- Roles depend on the month because embedsgrupper rotate monthly. Handing over a group on the list hands over its access.
- A future list is held back until its month begins, so Indstillingen can prepare next month without changing who has access now.
- Only next month and the month in effect can be edited. Older lists feed the stamtræ, the kvotient and role history, and nothing in the house's routine needs to change them.
- You cannot remove yourself from the live list or leave Indstillingen on it, because either would immediately take away your own Indstilling access in the middle of editing.
- Administrator needs a second person's approval, because both the role editor and the monthly list can be used by the group itself. Otherwise the group would decide its own membership.
- AK is shown in krydser rather than kroner, because the site gives AK labour no monetary value; Regnskab converts it by its own rules.
