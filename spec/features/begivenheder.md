# Begivenheder

Events with a time, a place and a guest list that residents answer ja or nej to, replacing the Facebook group and the sign-up sheet in the kitchen.

## Access
- Every resident can use begivenheder.
- An *åbent* event is visible to every resident. A *kun inviterede* event is visible only to its organiser, co-organisers and invitees.
- Anyone who can see an event can answer it and comment on it.
- Hosts (the organiser and co-organisers) edit, aflys, invite and uninvite.
- Only the original organiser adds or removes co-organisers.
- A private event you are not invited to does not exist as far as you can tell: its page is "not found", not "forbidden".
- Inspektionen and administrators get no special access to private events; a reported one is handled by a superuser in the Django admin.

## Creating an event
- An event has a title, a description (Markdown), an optional picture, a place, a start, an optional end, an optional number of seats and an optional svarfrist.
- A new event cannot start in the past; the end must be after the start; the svarfrist must be before the start.
- An invite-only event needs at least 4 invitees besides the organiser, on create and on every edit.
- Visibility is chosen explicitly. An open event may still carry invites as a nudge.

## Answering, seats and venteliste
- Answers are ja or nej.
- With a seat limit, the earliest ja answers get the seats and the rest form the venteliste in answer order.
- When someone drops out, the next person on the venteliste gets the seat and is notified.
- Changing an answer back to ja puts you at the back of the queue.
- Raising the limit seats people from the venteliste immediately, without a notification.
- The limit cannot be lowered below the number already seated.
- Answers close at the svarfrist, or when the event starts if there is none. After that nobody can change their answer, including dropping out.
- Hosts reopen answers by moving or clearing the svarfrist.

## Aflys and delete
- A host can delete an event only while nobody has said ja.
- After that the only option is Aflys: the event is greyed out, frozen against edits and new answers, and everyone who said ja is notified.

## Comments
- Plain-text comments, up to 1,000 characters, with one optional photo; a photo alone is a valid comment.
- A comment can be deleted by its author or by a host.
- Comments have no reactions and disappear with the event.

## Calendar
- The list shows upcoming events only; there is no archive of past events.
- A month view shows every event in the visible weeks, including the padding days from neighbouring months. On a phone, days show dots; tapping a day lists its events under the grid.
- Every event can be downloaded as a calendar file.
- Each resident has a personal calendar subscription URL containing the events they said ja to. Waitlisted events show as tentative and do not mark the time as busy; aflyste events show as cancelled.
- The subscription URL is a secret that works without logging in. Residents can renew it, which invalidates the old one.
- Calendar entries carry no attendee or organiser addresses and no alarms.

## Opslagstavlen
- A noticeboard post can point at an open event; the event page lists the posts about it ("Omtalt på opslagstavlen").
- Private events can never be linked from the board.

## Notifications
Web push, opt-in per device.
- New open event: everyone opted in except the organiser.
- New private event: the invitees only.
- Change of time, title or place: everyone who said ja. Description edits notify nobody.
- Aflyst: everyone who said ja.
- Moved up from the venteliste: that person.
- New comment: the hosts.
- Svarfrist within 24 hours: everyone who can answer and has not (current residents for an open event, invitees for a private one), once per event. Checked daily at 17:00 and whenever someone opens the list.

## Retention
- A held event is deleted 7 days after it ends (or starts, if it has no end), with its answers, comments and pictures.
- An aflyst event is kept for 30 days so subscribed calendars pick up the cancellation.
- Deletion runs nightly at 04:00 and whenever someone opens the list or the calendar.

## Decisions
- No "måske": a maybe can neither hold nor release a seat.
- Answers lock at the deadline, so the seat list never reshuffles after people were told they were in.
- Aflys instead of delete once people have committed: a deleted event stays forever in calendars that already imported it.
- No archive and no record of who came: the board is the house's record, events are a short tail.
- Moderators cannot see private events, because "except Inspektionen" would break the one promise a private event makes.
- The 4-invitee minimum keeps private events from becoming a DM channel.
- The calendar subscription lives outside the login-protected area, because calendar clients cannot log in and a broken feed fails silently.
