# Feature: One press, one send

A message, reply, post or comment is saved once, however many times its button is pressed while it
is being sent. Applies to every form on intern.

## Rules

- While a form is being sent, pressing its button again does nothing. This covers ordinary forms
  and htmx forms alike.
- An ordinary form that has been sent can be used again when Back brings its page back. A form
  whose answer is a file, so the page stays (Arkiv's downloads), can be sent again at once.
- A Den Hurtige message, a Den Hurtige reply, an opslagstavle post or an opslagstavle comment that
  repeats the same person's text in the same place within two minutes is not saved again. The
  sender is answered as for the first one.
- Sends with no text (a photo on its own) are never treated as copies.

## Decisions

- Both the browser and the server check. Safari sends every press of a form's button while the
  first is still on its way; Chrome sends only the first. The server catches what the browser
  cannot stop: a retry after a dropped connection, a second tab.
- Copies are found by their text, not by comparing photos. Comparing photos would mean opening
  them, and two different photos sent alone a moment apart are a real case.
- Two minutes: long enough for a slow upload pressed again, short enough that writing the same thing
  later is a new message.
- Two copies arriving together are saved once: the second waits on the sender's row until the first
  is saved, then finds it.
