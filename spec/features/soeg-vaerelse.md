# Søg værelse

The room-application round: residents apply for rooms Indstillingen puts up for grabs, and the highest kvotient wins.

## Access
- Every resident can see their own kvotient, apply, edit their application, see their own applications (Mine ansøgninger) and withdraw them.
- An application's detail page is visible to its owner and to Indstillingen.
- The Indstilling role manages the round (Værelsesudbud): creates and closes offers, sees applicants, settles ties and ends the round. Administrators can do the same.

## The round
- A round always targets next month, i.e. the month after the alumneliste currently in effect; neither offers nor applications let anyone pick the month.
- Indstillingen puts a room *i udbud* by entering its room number. Offering the same room twice for the same month does nothing.
- The round has no deadline; it lasts until Indstillingen ends it.

## Kvotient (K)
- K = a · 100 / (a + b + 12), rounded to two decimals; K is 0 if the denominator is not positive.
  - a = months lived at GAHK from move-in up to the target month, minus months of orlov.
  - b = months from the target month until the resident expects to finish studying.
- Higher K ranks first. The page shows the formula, the a and b values and the resulting K.
- K updates live as the resident fills in the study-end month/year and orlov, and can be checked at any time, even when no rooms are on offer.
- Move-in comes from the resident's registered move-in date. Without one, the page tells them to contact Indstillingen and they cannot apply.
- Orlov is pre-filled with a suggestion: every month since move-in in which the resident was not on the alumneliste. The resident may change it.

## Applying
- An application holds up to five ranked priorities chosen from the rooms on offer; at least one is required.
- A resident has one application per round. Submitting again replaces the priorities and recalculates K, but keeps the original submission time. The form opens pre-filled with the existing application.
- K is fixed at submission, using the resident's own study-end and orlov figures.
- Withdrawing an application deletes it.

## Allocation
- Indstillingen sees a live projection: for each offered room, the resident who would currently win it, or a tie.
- Applicants are taken in K order, highest first. Each gets their highest-priority room that is still free, and nobody wins more than one room.
- Submission time never breaks a tie. If the top contender for a room has the same K as another unplaced applicant who also listed it, the room is marked tied (uafgjort). All tied residents are held back from every other room until the tie is settled.
- The applicants page for a room lists everyone who listed it, ranked by K and then priority, and marks the projected winner and any tied applicants.
- Indstillingen settles a tie by recording the winner of the house's coin flip (lodtrækning). That room is then treated as decided, which can resolve other ties further down.

## Ending the round
- "Afslut runde" writes the result straight onto next month's alumneliste:
  - Everyone on this month's list who is not yet on next month's is copied forward with their room, embedsgruppe, rengøring and roles.
  - Each winner moves into the room they won and keeps their embedsgruppe and rengøring. Their old room becomes free.
  - Anyone else still in a won room is treated as the departing occupant. They are removed from next month's list along with their roles for that month.
- If nothing is tied, every offer and application in the round is cleared.
- If some rooms are still tied, only the settled rooms and the winners' applications are cleared. Indstillingen settles the remaining ties and ends the round again.
- Afterwards the admin page shows a summary once: who moved from which room to which (or "ny beboer"), who was removed as a leaver, and which rooms are still tied.

## Closing an offer
- Closing an offer removes the room from the round. It also deletes every application in that round that listed the room, including that application's priorities for other rooms.

## Decisions
- Ties are settled by coin flip in the house, not by who applied first, so the site records Indstillingen's choice instead of inventing an order.
- Tied residents are held back rather than moved down to their next choice, so the coin flip decides their first choice and the algorithm never quietly gives one of them a worse room.
- Ending a round copies the whole list forward before placing winners. Otherwise every resident outside the round would disappear from the list when the new month started.
- Leavers are removed only after every winner has been placed. Rooms change hands like musical chairs, and a winner who moves out of a room that someone else won must not be deleted.
- Suggested orlov counts months missing from the alumneliste. The list goes back further than any current resident's move-in, so a gap always means a real absence, not missing data.
