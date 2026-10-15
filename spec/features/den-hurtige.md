# Den Hurtige

The kollegium's quick-message chat for things relevant in the next hours, replacing the Messenger group.

## Access
- Every logged-in resident may read, post, reply, react and subscribe to notifications.
- The feature can be limited to chosen roles (staged rollout) by one setting, which narrows the pages, the sidebar entry and notification sign-up together; administrators are always in. While limited, the feed shows an "Under test" chip. It is currently open to everyone.
- A single channel can also be restricted to roles, on top of that. A channel or message you may not see answers "not found", never "forbidden".
- Administrator and Inspektion moderate: they may delete anyone's message. An administrator previewing as a plain resident loses that.

## Channels
- Fixed channels, each its own feed: Den Hurtige (the default, at /intern/den-hurtige/), TV-Rezz, G. A. Sportsmann, GAHKroom and M.H.G.A.
- A tab strip switches channel and shows how many live messages each holds (deleted ones not counted).
- Messages never appear outside the channel they were posted in. Replies, reactions and deletions always act in the message's own channel.
- New channels are added in code, not by admins.

## Posting
- A message is text of up to 500 characters, with an optional image. Ctrl/Cmd+Enter sends; Enter is a newline.
- Before it is sent, the author must choose how long it stays relevant: 30 min, 1 time, 2 timer, 4 timer, 12 timer, 1 døgn or 2 døgn. Nothing is preselected; without a choice the post is refused.
- A refused post (too long, no duration) keeps the typed text in the box; an attached image must be picked again.
- Images are downscaled in the browser and refused above 5 MB or if not an image. A bad image is dropped with a warning; the message is still posted.
- No confirmation is shown: the message appearing in the feed is the confirmation.

## The feed
- Reads like a chat: oldest at top, newest at the bottom, grouped under day headings ("i dag", "i går", "12. marts", with the year only when not this year).
- Consecutive messages from the same person within 5 minutes are drawn as one run, with one name and one avatar.
- Each message shows how long until it leaves the feed ("12 timer", "45 min").
- New messages, replies, reactions and expiries appear on their own within seconds, without losing your scroll position, open panels or half-typed text.
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
- Reactions never send notifications.

## Deleting
- The author or a moderator can delete a live message, after a confirmation.
- Within 5 minutes of posting it disappears completely, with its replies.
- After that it becomes "Besked slettet": text, image and reactions are gone for good, but its replies stay and it keeps its place. It still moves to the archive on its original timer. It cannot be replied to, reacted to or deleted again.

## Notifications
- Residents opt in per device ("Slå notifikationer til") when push is set up on the server; this is separate from the notice board's and other features' notifications on the same device.
- A new message notifies every subscriber except the author, titled with the sender's name and a short excerpt. Tapping it opens the right channel.
- A reply notifies only the message's author by default (nobody if they reply to themselves). Ticking "Underret alle" notifies every subscriber except the replier. The notification says "<name> svarede" and opens the thread.
- Every channel notifies by default; a resident can mute a channel ("Lyd fra") on all their devices. A direct reply to your own message still reaches you in a muted channel; "Underret alle" does not.
- A notification not delivered within an hour is dropped.
- The installed app (PWA) opens on the Den Hurtige feed.

## Decisions
- Messages are archived rather than deleted because residents kept needing yesterday's messages back (who borrowed the drill, what time was agreed).
- The author must pick a duration because a preselected default meant nobody chose and nothing ever left the feed.
- Deletion splits at 5 minutes: an unread slip can vanish, but a message others have answered stays as a placeholder so the conversation still makes sense.
- Channels notify until muted rather than being opt-in, because a new channel nobody hears from is a channel nobody posts in.
- Reactions are silent: a dorm-wide buzz for every 👍 is the noise this feature exists to remove.
- Unknown and forbidden channels look the same, so a restricted channel's existence is never confirmed.
