# Feature: Outgoing mail — every mail the app sends

Intern sends all its mail from one one.com account, which refuses everything for a while after about
25 mails in 5 minutes. So no mail is sent directly: each is put in a queue and sent from there, at a
pace the account can take, and retried when it does not go through.

## What goes in the queue

- Every mail the app sends, including any sent by a third-party package. A mail is queued together
  with whatever caused it: if that is undone (a purchase that fails), the mail is never sent.
- Each mail has a kind, which decides how it is sent:

| Kind | Mails | Order | Pace | Given up after |
|---|---|---|---|---|
| Haster | password reset, welcome mail | first | at once | half the password link's lifetime (1 hour) |
| Almindelig | everything else, e.g. admissions auto-reply, debt warnings, admin mails | second | at once | 1 day |
| Masseudsendelse | alumneregninger, ølkælder overviews | last | at most 10 mails per 5 minutes, counting every mail sent | 3 days |

- A mail that carries something with its own expiry can be given a shorter lifetime than its kind's.

## Sending

- A mail is sent as soon as it is queued. A Haster mail is sent by the web server itself, so a
  password reset does not wait for background jobs that are busy with something else.
- Bulk mail waits when 10 mails have gone out in the last 5 minutes, and continues once there is
  room again. Other mail is never held back, so a password reset goes out during a monthly send.
- If one.com refuses the account (too many mails) or cannot be reached, sending pauses for 5–6
  minutes and the mail stays queued; that wait does not count against the mail.
- If one.com refuses one recipient for now (a full mailbox), that mail is retried with a growing
  wait: within minutes for a Haster mail, from 10 minutes up to hourly for the others.
- If one.com refuses a mail outright (no such address, a sender the account may not use), the mail
  has failed.
- A mail not sent within its lifetime is given up on (Udløbet).
- A mail whose sending was cut off (a deploy, a crashed job) may or may not have arrived. It is
  marked Ukendt and never sent again by itself.

## Seeing what happened

- A failed or expired mail fails the mail job on the worker-jobs page, naming the recipient and the
  reason. A mail server that is down fails the job only once mail has waited more than half an hour.
- Administrators see every mail in Django admin under "Outgoing mails", and can send a failed or
  Ukendt mail again while it is still within its lifetime.
- The alumneregninger status page shows each alumne's mail: in the queue, sent, failed or Ukendt.
  For an Ukendt mail, regnskab asks the alumne and, if it did not arrive, sends it again with one
  button. Sending the period again re-sends every statement whose mail failed or expired.

## Demo data

- `task seed` fills the queue with a mail of every kind and status — a bulk ølkælder overview held by
  the rate limit, resets sent, waiting again and expired, failures that can and cannot be sent again,
  one being sent, one Ukendt — made by the code that sends each, then given the state delivery would
  have left it in. Seeding sends nothing. Mails left in the queue are seeded only when mail is printed,
  not sent; with real SMTP they would reach the demo residents' gahk.dk addresses.

## Retention

- A sent mail's content is deleted once it is sent. A failed or Ukendt mail's content is deleted
  when its lifetime ends; it can no longer be sent again after that.
- Every mail's record is deleted after 90 days.

## Decisions

- **One queue for all mail, third-party included.** Choosing per call where mail goes would let any
  package that calls Django's send_mail bypass the limit and lock the account for everyone.
- **A password reset is not sent inside the request.** Sending it there would make the answer slower
  for an address with an account, which tells anyone who asks which addresses exist. Sending it from
  the web server just after answering keeps it as fast as before, without that leak.
- **The resident is not told when a reset mail fails**, as with Django's own reset. Telling them
  would reveal that the address has an account, and the cause is one administrators must fix.
- **A mail cut off mid-send is never re-sent by itself.** Two copies of an alumneregning are worse
  than one that someone has to check.
- **Mails have a lifetime.** A reset link arriving after it has expired, or a debt warning a week
  late, does more harm than none; a mail that has waited that long is reported instead.
