# Feature: One press, one send

A message, reply, post or comment is saved once, however many times its button is pressed while it
is being sent. Applies to every form on intern; the server-side guarantee covers Den Hurtige messages
and replies and opslagstavle posts and comments.

## Rules

- The pressed button shows a busy ring until the send is done.
- While a form is being sent, pressing its button again does nothing. This covers ordinary forms
  and htmx forms alike.
- An ordinary form that has been sent can be used again when Back brings its page back. A form
  whose answer is a file, so the page stays (Arkiv's downloads), can be sent again at once.
- Each form carries a send token, made when the form is drawn. A send whose token was already saved
  by the same person is not saved again; it is answered as the first was (the new post's page, the
  updated feed or replies).
- Sending the same text, or the same photo, again on purpose is a new send with a new token, and is
  saved.
- A form that stays on the page after sending (Den Hurtige's composer and reply box) gets a new
  token after each send that went through. A failed send keeps its token, so sending it again
  cannot make a copy if the first did land.
- A page shown again from history gets new tokens.
- A refused send (no duration, too long) saves nothing, so the same token sends again.

## Decisions

- Both the browser and the server check. Safari sends every press of a form's button while the
  first is still on its way; Chrome sends only the first. The server catches what the browser
  cannot stop: a retry after a dropped connection.
- A token, not a comparison of what was sent: photos are covered, and a repeat that is meant is
  saved.
- Tokens are saved in the same transaction as what they sent. A copy arriving at the same moment
  waits on the token's unique index and then finds it; if the save fails, the token goes with it.
- Tokens are kept a day, then removed as the same person sends again. No scheduled job.
- A send without a token (a page drawn before tokens existed) is saved as before.
