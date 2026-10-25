# @mentions (#229)

Typing `@` in a post or comment field offers residents on the active alumneliste and the
embedsgrupper with members this month. Picking one inserts a token into the text:

- `@[Anders Hansen](42)`: resident 42. Rendered as a link to their profile.
- `@[Inspektionen](g55)`: embedsgruppe (`core.Workgroup`) 55. Rendered highlighted, with no link
  (groups have no page of their own).

The token is the only storage: there is no mentions table. It keeps the name as written, so an old
post still reads right after someone moves out or the group is renamed. A hand-typed `@Anders`
without the token is plain text.

## Where

Opslagstavlen (posts and comments), Den Hurtige (messages and replies), Reparationer (description and
notes) and Begivenheder (description and comments). Fields opt in with the `data-mentionable` attribute; the picker is
`frontend/src/mention.ts`, fed by `residents:mention_lookup` (`/intern/beboere/naevn`).

## Rendering

`core.links.linkify` (`|links`, plain text) and the Markdown pipeline (`core.markdown`) both render
the token. The rendered element carries `data-mention="resident|group"`; the Markdown allowlist
admits that attribute with exactly those two values and nothing else.

## Notifications

Category `mentions` ("Når du bliver nævnt", Høj), its own group on the settings page. Logic in
`core.mentions.notify_mentions`:

- Only residents on this month's alumneliste who pass the feature's access gate (channel roles,
  invite list) are told. The author never is.
- A group mention reaches the group's members this month. Direct beats group: someone named both ways
  gets "… nævnte dig …" once.
- A mention replaces the new-post, new-event, reply or room notification the same person would
  otherwise get for the same thing.
- Editing a notice or an event only notifies residents the edit newly reaches. The other surfaces
  have no edit.
