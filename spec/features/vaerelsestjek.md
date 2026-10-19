# Værelsestjek

Condition reports for each room, usually made when someone moves in or out, with a score, a comment and a photo for each criterion.

## Access
- Any logged-in resident can browse rooms, read reports and submit a new report.
- Only the AK role can use the AK overview and its export. Administrators can too.

## Finding a room
- The overview shows each floor's plan with a button for each room, and greys out rooms that have never been checked. Hovering a button shows the date of the last check.
- Below the plans, a table lists every room with its floor, side and last check (date and who), plus links to view the report and to start a new check.

## Making a report
- The form lists a fixed set of criteria (walls, ceiling lamp, closet doors, …), sorted so that criteria for the same object sit together.
- Each criterion has a score, a comment and an optional photo, and its scale and legend are shown underneath.
- Each criterion uses one of three scales: 1–5, 0–2 or 0–1. A blank score is allowed.
- The form is pre-filled with the current report's scores and comments. Each row stays locked until "Opdater" is clicked. Without JavaScript every row is simply editable.
- A score outside its scale is dropped for that criterion only, with a warning. The rest of the report is still saved.
- Photos must be JPEG, PNG, GIF, WebP or HEIC and at most 5 MB by default; GIFs fall under the site-wide animation limit instead, and a HEIC photo may be up to 25 MB and is stored as JPEG. The browser shrinks large photos before upload. A rejected photo gets a warning and is skipped, and the rest of the report is saved.
- Criteria left with no score, comment or photo are not stored.
- Saving always creates a new report that becomes the room's current one, recorded with who made it and when.

## History
- Earlier reports are kept. On the check page, "Vis tidligere rapport" fills the form with an older report's values.
- Submitting from an older report still creates a new report. The old one is never changed.
- A new report keeps each criterion's photo from the current report (including images imported from the old site) unless a new photo is uploaded for it, which replaces it. A photo cannot be removed, only replaced.

## Viewing a room
- The room page shows the current report: who made it and when, and each criterion's score with its scale, comment and photos (including images imported from the old site).
- A collapsed "Forklaring af skalaerne" section explains every scale.

## AK overview
- A table with one row for each room that has a report and one column for each criterion. It shows the current scores, plus the date of the last check and who made it.
- Each room links to its report page.
- It can be exported as CSV or Excel with the same columns. Cells that would start a spreadsheet formula are neutralised.

## Decisions
- Any resident can make a check, not just Inspektionen. Checks are done by whoever is around, and restricting them to one embedsgruppe meant the work stalled. Every report records its author, so the history stays attributable.
- An off-scale score is dropped instead of rejecting the whole report: the photo inputs cannot be refilled, so a rejection would mean photographing the room again. It is not clamped either, because a 5 stored as 2 would look exactly like a real 2.
- In the AK table and the export, each score sits under its own criterion's column. The old site filled cells by position, so one missing criterion pushed every later score under the wrong heading.
- Photos carry over because a browser cannot pre-fill a file input; otherwise correcting one score would drop every photo from the room's current report.
- Rows are locked as read-only, not disabled, because disabled fields are not submitted. The form therefore still works without JavaScript.
- Photo uploads use the site-wide image rules. Every resident can upload, so an SVG accepted here could run script from the site's own address.
