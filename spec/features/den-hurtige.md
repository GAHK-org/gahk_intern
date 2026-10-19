# Den Hurtige

The kollegium's quick-message chat for things relevant in the next hours, replacing the Messenger group.

## Access
- Every logged-in resident may read, post, reply and react.
- The feature can be limited to chosen roles (staged rollout) by one setting, which narrows the pages, the sidebar entry and who is notified together; administrators are always in. While limited, the feed shows an "Under test" chip. It is currently open to everyone.
- A single channel can also be restricted to roles, on top of that. A channel or message you may not see answers "not found", never "forbidden".
- Administrator and Inspektion moderate: they may delete anyone's message. An administrator previewing as a plain resident loses that.

## Channels
- Fixed channels, each its own feed: Den Hurtige (the default, at /intern/den-hurtige/), TV-Rezz, G. A. Sportsmann, GAHKroom and M.H.G.A.
- A channel picker, which doubles as the page title, switches channel and shows how many live messages each holds (deleted ones not counted). The counts stay current while the picker is open.
- Messages never appear outside the channel they were posted in. Replies, reactions and deletions always act in the message's own channel.
- New channels are added in code, not by admins.

## Posting
- A message is text of up to 500 characters, with an optional image. Ctrl/Cmd+Enter sends; Enter is a newline.
- Before it is sent, the author must choose how long it stays relevant: 30 min, 1 time, 2 timer, 4 timer, 12 timer, 1 døgn or 2 døgn. Nothing is preselected; without a choice the post is refused.
- A refused post (too long, no duration) keeps the typed text in the box; an attached image must be picked again.
- Images are downscaled in the browser and refused above 5 MB or if not an image. An iPhone HEIC photo may be up to 25 MB and is stored as JPEG, and a pasted image is attached like a picked one. A bad image is dropped with a warning; the message is still posted.
- No confirmation is shown: the message appearing in the feed is the confirmation.

## The feed
- Reads like a chat: oldest at top, newest at the bottom, grouped under day headings ("i dag", "i går", "12. marts", with the year only when not this year).
- Consecutive messages from the same person within 5 minutes are drawn as one run, with one name and one avatar. Names and avatars link to the author's profile.
- Each message shows how long until it leaves the feed ("12 timer", "45 min").
- New messages, replies, reactions and deletions appear on their own at once, and expired messages leave within a minute, without losing your scroll position, open panels or half-typed text. If the live connection drops, the feed checks every 5 seconds instead.
- On the Den Hurtige page pinch- and double-tap-zoom are disabled, so it behaves like an app; the rest of the site keeps zoom.
- Touch gestures: swipe a message right to open its thread; swipe your own message left to delete it (still asks first); swipe the thread right to close it.

## Archive
- When its time runs out a message leaves the feed and goes into the channel's archive, with its replies. Nothing is deleted on a timer.
- Scrolling up past the oldest live message loads the archive in chunks, ending with "Her begynder <channel>". A channel with nothing archived does not advertise it. The archive also has its own page that works without JavaScript.
- Archived messages are read-only: no replies, reactions or deletion, by anyone. Their threads can still be opened.
- Only a site administrator, through the Django admin, can remove an archived message.

## Replies
- Each message shows its reply count; replies open in a side panel (or their own page without JavaScript) that updates live.
- A reply may be text (up to 500 characters), an image, or both; an empty reply is refused.
- Opening a thread can be undone with the back gesture or Escape.

## Reactions
- Each resident has at most one emoji per message: a new emoji replaces theirs, tapping the same one removes it.
- A shortlist of common emoji is one tap; any single emoji is accepted, several or non-emoji are not.
- Holding, hovering or right-clicking a reaction shows who used that emoji.

## Deleting
- The author or a moderator can delete a live message, after a confirmation.
- Within 5 minutes of posting it disappears completely, with its replies.
- After that it becomes "Besked slettet": text, image and reactions are gone for good, but its replies stay and it keeps its place. It still moves to the archive on its original timer. It cannot be replied to, reacted to or deleted again.

## Notifications
Sent through Notifications, where each channel is its own category (see notifications.md).
- A new message goes to everyone who can read the channel except the author, as a push only, titled with the sender's name and a short excerpt. Tapping it opens the channel.
- A reply goes to the message's author (unless they reply to themselves), grouped per message ("Anders og Bente svarede"), and opens the thread. Ticking "Underret alle" also tells everyone else who can read the channel.
- A new reaction goes to the message's author, grouped and at low importance. Changing or removing a reaction tells nobody.
- Every channel notifies by default. The channel header shows 🔔 "Lyd til" or 🔕 "Lyd fra" and links to the channel's row in the notification settings, where it is turned off for all the resident's devices. A reply to your own message still reaches you in a muted channel; "Underret alle" does not.
- The page asks "Få besked på denne enhed?" while this device is not subscribed to push.
- A notification expires with the message it is about; a push not delivered by then is dropped.
- The installed app (PWA) opens on the Den Hurtige feed.

## Decisions
- Messages are archived rather than deleted because residents kept needing yesterday's messages back (who borrowed the drill, what time was agreed).
- The author must pick a duration because a preselected default meant nobody chose and nothing ever left the feed.
- Deletion splits at 5 minutes: an unread slip can vanish, but a message others have answered stays as a placeholder so the conversation still makes sense.
- Channels notify until muted rather than being opt-in, because a new channel nobody hears from is a channel nobody posts in.
- Reactions tell only the message's author: a dorm-wide buzz for every 👍 is the noise this feature exists to remove.
- Unknown and forbidden channels look the same, so a restricted channel's existence is never confirmed.
