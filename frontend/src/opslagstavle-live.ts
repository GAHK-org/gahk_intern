// Opslagstavlen's board, live. No-op on every page without one (board.html's [data-live-board]).
//
// What the board does with a signal from ./live, and why each is different:
//
//   changed  a card on screen is re-fetched (opslagstavle:live_cards) and MORPHED into place, so a
//            reaction, an edit, a pin or a new comment count appears without anything around it
//            moving. A card not on this page is none of this page's business.
//   deleted  the card is removed.
//   created  NOTHING IS INSERTED. The banner at the top counts it instead and links to page 1. An
//            insertion pushes every card down by one under somebody reading, and on page 2 the new
//            post does not even belong on the page — the pager has shifted by one under them.
//   resync   every card on screen is re-fetched in one request; any the answer leaves out was
//            deleted while the socket was down, and goes.
//
// The detail page is a generic live region (live-region.ts; opslagstavle/_detail_live.html). Only
// the board needs its own handling, because one request answers for many cards here.

import { LIVE_EVENT, concerns, topicsOf, type LiveSignal } from "./live";
import { fetchFragment, morphInto, serialised } from "./live-region";

const CARD_PREFIX = "opslag-";

// ---- the board -------------------------------------------------------------------------------

const board = document.querySelector<HTMLElement>("[data-live-board]");

if (board) {
  const liveUrl = board.dataset.liveUrl ?? "";
  const category = board.dataset.category ?? "";
  const topics = topicsOf(board);
  const banner = document.getElementById("js-board-new");
  let unseen = 0;

  const cardsOnScreen = (): HTMLElement[] =>
    Array.from(board.querySelectorAll<HTMLElement>(`[id^="${CARD_PREFIX}"]`));
  const cardFor = (pk: number): HTMLElement | null => document.getElementById(`${CARD_PREFIX}${pk}`);

  const refresh = serialised<number>(async (pks) => {
    const query = new URLSearchParams();
    for (const pk of pks) query.append("id", String(pk));
    const html = await fetchFragment(`${liveUrl}?${query}`);
    if (html === null) return;
    const answer = document.createElement("template");
    answer.innerHTML = html;
    for (const pk of pks) {
      const old = cardFor(pk);
      if (!old) continue;
      const fresh = answer.content.getElementById(`${CARD_PREFIX}${pk}`);
      if (fresh) morphInto(old, fresh);
      else old.remove();
    }
  });

  document.addEventListener(LIVE_EVENT, (event: Event) => {
    const signal = (event as CustomEvent<LiveSignal>).detail;
    if (!concerns(signal, topics)) return;
    if (signal.kind === "resync") {
      for (const card of cardsOnScreen()) refresh(Number(card.id.slice(CARD_PREFIX.length)));
      return;
    }
    if (signal.pk === null) return;
    if (signal.kind === "created") {
      if (category && signal.category !== category) return;
      unseen += 1;
      const label = banner?.querySelector("span");
      if (banner && label) {
        label.textContent = `${unseen} ${unseen === 1 ? "nyt opslag" : "nye opslag"} — vis`;
        banner.hidden = false;
      }
    } else if (signal.kind === "deleted") {
      cardFor(signal.pk)?.remove();
    } else if (cardFor(signal.pk)) {
      refresh(signal.pk);
    }
  });
}
