# Feature: Date and time fields

How every date and time a person enters in the intranet is picked: events, Hjemmeside's news and
front-page events, the report and filter periods (regnskab, ølkælder, ansøgninger), and residents'
dates.

## Dates

- A date box shows the date in Danish with its weekday, e.g. "fre. 5. dec. 2026".
- Clicking it opens a calendar: Danish month and day names, Monday first, week numbers, today
  marked, a month and a year dropdown, "I dag", and "Ryd" when the date is optional.
- A date can also be typed: "5.12.2026", "5/12/26", "5-12", "0512", "24 dec", "2026-12-05". With
  no year, the date nearest the field's own date (or today) is meant. Anything else is refused with
  "Skriv datoen som fx 24.12.2026."
- The keyboard works in the calendar: arrows move a day or a week, Page Up/Down a month (with Shift
  a year), Enter picks, Escape closes.
- On a phone the date box brings up no keyboard; the calendar is the way in.
- Without JavaScript the browser's own date box is used.

## Times

- A time is always 24-hour, shown as "19:30", next to its date ("dato kl. tt:mm").
- It is typed — "19:30", "19.30", "19,30", "1930" or "19" — or picked from a list of half-hours that
  opens under the box and follows what is typed. "1930" becomes "19:30" when the box is left.
- A date without a time, or a time without a date, is refused with a message saying which is
  missing.

## Fields that belong together

- An event's end: typing an end time with no end date puts it on the start's day, or the day after
  when it is earlier than the start (a party ending at 02:00). Moving the start moves the end with
  it, keeping the length. The end's calendar cannot go before the start's day.
- An event's svarfrist: its calendar opens on the event's month and cannot go past the start's day.
  It does not move when the start does.
- The form checks all of this again when it is saved.

## Decisions

- **No browser time picker anywhere.** It draws the clock in the device's regional format, so a
  phone or Mac set to a US region showed AM/PM on a Danish page. A test fails if one comes back.
- **Our own calendar, not the browser's date box.** The browser's box is a row of segments on a
  desktop, follows the device's region (month first, Sunday first) and makes a birthday decades of
  clicks away. The browser's box stays as the fallback without JavaScript, and the value sent is
  still YYYY-MM-DD, so no form or filter had to change.
- **Half-hours in the list.** Events start on the hour or half-hour; other minutes are typed.
- **The svarfrist stays put when the start moves.** It is often a fixed date ("svar inden
  fredag"); moving it silently would be worse than the form saying it is now after the start.
