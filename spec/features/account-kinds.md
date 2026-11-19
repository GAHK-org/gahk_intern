# Account kinds

Every account has a kind, which decides which pages exist for it. Roles are separate and add powers
on top (moderate, edit gahk.dk, the embedsgrupper's tools); the kind decides the pages.

- **Resident** (`resident`): every account except the ones below. Reaches every page, subject to
  login and roles as before.
- **Kitchen** (`kitchen`): the kitchen staff's shared login (`Resident.is_kitchen`, ticked in Django
  admin). Not a resident.

A kind other than resident reaches only the pages opened to it and gets 403 everywhere else. Its
live updates are limited the same way: a feature opens its socket topics to the kinds that may listen.

## Pages for every kind

Logging in and out, the password pages, uploaded files (`/media/`, which keep their own checks), the
calendar feed, the service worker and `/install`.

The account pages sit at the root, not under `/admin/`, which holds the site-admin tools:
`/login`, `/logout`, `/password-change`, `/password-reset` and the reset link
`/password-reset/<uid>/<token>/`.

## The kitchen account

- Lands on Den Hurtige's Køkkenet channel; the dashboard and Den Hurtige's bare address send it there.
- Den Hurtige: only the Køkkenet channel, which every resident can also read and post in.
- Opslagstavlen: posts, edits and comments on its own posts. Anyone else's post is a 404. No album or
  GAHKFlix embeds.
- Reparationer: reports and comments like a resident, never moves or archives cards.
- Notifikationer: only its channel, comments and reactions on its posts, and admin broadcasts.
- Live updates: Den Hurtige, Reparationer and its notifications. Not Opslagstavlen, whose signals are
  about everyone's posts.
- An administrator can see the site as it does with "Se side som → Køkkenet", and end the preview
  from there.

## Decisions

- **Closed unless opened.** A page is for residents unless its view is marked open to another kind,
  so a new page is closed to the kitchen until somebody opens it on purpose. The check is on the view
  a URL resolves to, not on path prefixes: a new page under an opened prefix stays closed, and a path
  that is no page is a plain 404.
- **One account model.** The kitchen account and, later, former residents are accounts like any
  other. A former resident is the same person with the same posts and history, so a separate user
  table would gain nothing and cost changing every reference to the account.
- **Prepared for former residents.** They are planned as their own kind, opened to Den Hurtige,
  Opslagstavlen and whatever else is decided then. Not implemented yet.
