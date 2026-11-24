# People, residents and accounts

Three things the intranet keeps apart.

| | What it is | Holds |
|---|---|---|
| Person | A human the house knows | Name, birthday, phone |
| Resident | A person's membership of the house | Move-in, move-out, studie, fylgje, months on the alumneliste |
| Account | Something that logs in and acts | Login e-mail and password, site profile, e-conomic kontonr., what it did |

| Who | Person | Resident | Account |
|---|---|---|---|
| Lives here now | yes | yes | yes |
| Lived here in 1975, maybe dead | yes | yes | no |
| One of the kitchen staff, a guest | yes | no | yes |
| The shared kitchen login, the photo import script | no | no | yes |

## Behaviour

- The alumneliste and the fylgje name residents. Posts, krydser, bills and roles belong to accounts.
- Stamtræ shows every resident, with a login or not, and nobody who never lived here.
- A resident without a login is on the alumneliste without a profile link or picture, and can be
  picked as someone's fylgje.
- The list editor adds someone without an e-mail as a resident with no login; with an e-mail they
  get a login and a mail to set its password.
- The resident edit page (`/beboer/<resident>/rediger`) edits the person, their time here, and the
  login's e-mail when there is one.
- An account keeps a copy of its person's name, for showing and searching where the account is at
  hand; changing the person changes it.
- The legacy import keeps alumner without an e-mail as residents with no login.

## Decisions

- **Three, not one.** A human, their time at the house and a login change independently: alumner
  without e-mail, kitchen staff who never lived here, devices and scripts that are no one.
- **One resident per person**, however often they moved out and back; the months show the gaps.
- **One account per person for now.** Nothing needs more.
- **The site profile stays on the account** (picture, bio, links) until alumner without a login need
  profiles.
- **The login model was renamed in place.** It was `Resident`; its migrations were renamed to
  `Account` with the table kept, then given a table of its own. Every account became a person and a
  resident with the same id, so no month on the list was rewritten. The audit trail keeps the
  account's history under its old kind, `residents.resident`.
