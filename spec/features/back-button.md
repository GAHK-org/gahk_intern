# Feature: The Back button

What a page looks like when the browser's (or the phone's) Back and Forward bring it back. Applies to
every page of intern.

## Rules

- A page that nothing has changed since comes back instantly, exactly as it was left (scroll
  position, open thread, half-typed text).
- A page that something was written since (by this browser, in any tab) loads again, so it shows the
  current state: the comment just written, the item just moved, the button just pressed.
- Text that was sent is never typed back into a form. Text that was only typed and not sent stays.
- Menus are closed when a page comes back: the avatar menu, the bell, the phone sidebar, the channel
  picker, the photo album's menus and the opslagstavle card menu.
- Den Hurtige: sending a message does not leave the page. The box empties, the message appears, and
  Back leaves the chat in one press however many messages were sent.
- A refused message (too long, no duration) shows the warning with the text still in the box.
- The notifications page always loads again when it comes back, so an opened notification is no
  longer shown as unread.

## Decisions

- Reload only after a write, not on every Back: an instant Back is worth keeping wherever the page
  cannot be out of date.
- "After a write" compares two times from the server's clock, the page's own and the last write's,
  so a phone with a wrong clock cannot fool it.
- Previews and the push check the browser sends by itself are not writes; counting them would
  reload pages for nothing.
- Pages are not marked `no-store`. That would make Chrome load every page again on Back, and Chrome
  would still type sent text back into the form.
