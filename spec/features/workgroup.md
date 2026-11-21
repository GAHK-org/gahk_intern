# Feature: Din embedsgruppe

One page per embedsgruppe that gathers what the group needs: who is in it, its Drive folders, its wiki page and the
intranet pages it looks after. `Din embedsgruppe` in the sidebar opens your own group this month.

## Behaviour

- Membership is the active month's alumneliste. Your group is the one you are in this month.
- Every resident can open every group's page. Your own is headed `Din embedsgruppe`; other groups are headed `Embedsgruppe`.
- With no group this month, the page says so.
- Every page lists the other groups with members this month, with their member counts.
- A profile's `Embedsgruppe` links to that group's page.
- The kitchen account cannot open it.

### Members

- Each member with picture, name (to their profile), room, phone and e-mail. You are marked `Dig`.
- Once next month's alumneliste exists, the page shows who joins and who leaves the group from that month.

### Drive

- Links to the group's folders in this order: the current period, the next period once it has a folder, `Uden periode`,
  and the group folder with every period.
- Each link says whether you can edit or only read.
- Only folders you can open are shown. A group you are not in usually shows none.
- The card is hidden while Drive is closed to you.

### Wiki

- `Gruppens side i wikien` searches the wiki for the group's name. It opens the page directly when one has exactly that
  name, and lists the matches otherwise.

### Gruppens sider

- The intranet pages the group's role is needed for, plus the boards the group runs for everyone, each with a
  short description:

| Group | Pages |
| --- | --- |
| Indstillingen | Næste måneds alumneliste, Ansøgninger, Værelsesudbud, Hjemmeside |
| Inspektionen | Reparationer, Drive, Hjemmeside |
| AK-gruppen | AK-oversigt, Værelsestjek-oversigt |
| Ølkælderen | Ølkælder-admin, Salgsoverblik, Personoversigt, Produkter |
| Regnskabsgruppen | Regnskab, Alumneregningens tekster |
| PR-gruppen | Hjemmeside, Nyheder, Begivenheder på gahk.dk, Sider |
| Repperne | Reparationer, Arkiverede reparationer |
| Vicevært | Reparationer |
| Fotogruppen | Fotoalbum, GAHKFlix |

- A link shows only to someone who holds the role, and a page behind a rollout gate only once the gate lets them in.
- Groups without a role (Køkkengruppen today) have no such section.

## Decisions

- The wiki link is a search rather than a fixed page address, because the wiki's page names are not stored here. A page
  named after the group is still reached in one click.
- Other groups' pages are open to everyone: the alumneliste already shows who is in which group, and it helps to see
  who to ask.
- A Drive folder you can only see the size of is left out, because it would not open.
