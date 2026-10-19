# Opslagstavlen

The kollegium's noticeboard: posts with pictures, comments and reactions, replacing the Facebook group.

## Access
- Every resident can read, post, comment and react.
- Only the author can edit a post.
- A post can be deleted by its author or by an administrator or Inspektionen.
- A comment can be deleted by its author or by an administrator or Inspektionen, not by the post's author.
- Administrators and Inspektionen pin and unpin posts.
- An administrator previewing the site as a resident loses the moderation controls.

## Posts
- A post has a category (Nyt & info, Begivenhed, Værelsesrunden, Fødselsdag, Praktisk, Andet) and a Markdown body of up to 8,000 characters. There is no title.
- Pictures are uploaded from the compose toolbar and placed in the body. Pictures from other sites are not shown.
- Raw HTML in a post is shown as text.
- The composer has a preview that renders exactly as the saved post will.
- An edited post shows "Redigeret". Pinning does not count as an edit.
- Each post shows the author's embedsgruppe as it was when they wrote it. Authors' names and avatars, on posts and comments, link to their profile.
- A post can point at an upcoming open begivenhed; the card shows a chip linking to it. If the event is deleted the chip disappears and the post stays. A private event can never be linked, and the chip is hidden if the event is later made private.

## The board
- Newest first, 15 per page, filterable by category.
- Up to 5 pinned posts sit above the list on every page, most recently pinned first.
- Every post has its own page.
- The board updates live: reactions, edits, pins and comment counts change in place on the cards shown, and deleted posts disappear. New posts are not inserted; a banner ("N nye opslag — vis") links to the first page. If a card shown is pinned, unpinned or moved out of the filtered category, the banner offers to redraw the page instead.
- A post's own page updates the post and its comments live, leaving a half-written comment alone, and says so if the post is deleted.

## Comments and reactions
- Comments are plain text up to 1,000 characters, with one optional photo; a photo alone is a valid comment.
- Each resident has one reaction per post: picking another emoji replaces it, picking the same one removes it.

## Notifications
Sent through Notifications, where each resident chooses per category (see notifications.md).
- New post: everyone who can open the board except the author, with the post as plain text.
- New comment: the post's author only, grouped per post.
- New reaction: the post's author only, grouped and at low importance. Changing or removing a reaction tells nobody.
- An author who can no longer open the board is not told.
- A notification opens the post itself.

## Retention
- Posts are kept indefinitely. Only the author or a moderator removes one, and its comments, reactions and pictures go with it.
- Pictures uploaded in the composer but never used in a saved post are deleted after a day, by a nightly job at 03:40.

## Decisions
- Kept indefinitely: the board is the house's record (værelsesrunder, practical notices, who announced what). Pagination and the category filter keep old posts out of the way.
- Moderators can delete but never edit others' posts: removal is visible, a silent rewrite under someone's name is not.
- Post authors cannot delete comments on their own post; that invites exactly the disputes Inspektionen exists to settle.
- Comments and reactions notify only the post's author, so a busy thread is not a house-wide alarm. There is no "Underret alle" as in Den Hurtige: a noticeboard has no single shared audience.
- Posts are stored as Markdown and rendered on every view, so a tightened safety rule applies to old posts immediately.
- New posts are announced by a banner, not inserted: inserting them would shift every card under someone reading, and on page 2 or later they do not belong on the page at all.
- Not a chat: content for the next few hours belongs in Den Hurtige, where messages leave the feed on their own.
