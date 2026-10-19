// Opslagstavlen, live. No-op on every page without a live board or a live detail region.
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
// The detail page re-draws its whole live region (opslagstavle:live_detail) on a signal naming its
// post. The comment form sits inside that region with data-morph-skip, so a comment being written
// survives somebody else reacting — feed.ts sets the morph callback that honours the marker.
//
// Plain fetch + Idiomorph rather than htmx: one request answers for many cards here, and htmx swaps
// one target per request. htmx.process() afterwards wires up the hx-* attributes on anything new.

import { Idiomorph } from "idiomorph/htmx";
import { LIVE_EVENT, type LiveSignal } from "./live";

const CARD_PREFIX = "opslag-";

const htmx = (): { process: (el: Element) => void } | undefined =>
  (window as unknown as { htmx?: { process: (el: Element) => void } }).htmx;

/** The fragment at `url`, "" if the thing it draws is gone (404), or null to leave the page alone —
 * a 204 from an expired session, a network failure. */
async function fetchFragment(url: string): Promise<string | null> {
  try {
    const response = await fetch(url, { credentials: "same-origin" });
    if (response.status === 404) return "";
    if (response.status !== 200) return null;
    return await response.text();
  } catch {
    return null;
  }
}

function morphInto(old: Element, fresh: Element): void {
  Idiomorph.morph(old, fresh, { morphStyle: "outerHTML" });
  const now = fresh.id ? document.getElementById(fresh.id) : null;
  htmx()?.process(now ?? old);
}

/** One request at a time per page, with everything that arrived meanwhile batched into the next —
 * three reactions in a second cost two fetches, and the last state is never the one skipped. */
function serialised<T>(run: (batch: Set<T>) => Promise<void>): (item: T) => void {
  const pending = new Set<T>();
  let busy = false;
  const flush = async (): Promise<void> => {
    if (busy || pending.size === 0) return;
    busy = true;
    const batch = new Set(pending);
    pending.clear();
    try {
      await run(batch);
    } finally {
      busy = false;
      void flush();
    }
  };
  return (item: T) => {
    pending.add(item);
    // A microtask later, so every signal raised in this tick (a resync's whole page) is one batch.
    queueMicrotask(() => void flush());
  };
}

// ---- the board -------------------------------------------------------------------------------

const board = document.querySelector<HTMLElement>("[data-live-board]");

if (board) {
  const liveUrl = board.dataset.liveUrl ?? "";
  const category = board.dataset.category ?? "";
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

// ---- one post --------------------------------------------------------------------------------

const DETAIL_ID = "js-notice-live";
const detail = document.getElementById(DETAIL_ID);

if (detail) {
  const pk = Number(detail.dataset.liveNotice);
  const liveUrl = detail.dataset.liveUrl ?? "";

  const showDeleted = (): void => {
    const region = document.getElementById(DETAIL_ID);
    if (!region) return;
    const note = document.createElement("div");
    note.className = "card-flat card-pad";
    note.setAttribute("role", "status");
    note.innerHTML = "<p><strong>Opslaget er blevet slettet.</strong></p>";
    region.replaceWith(note);
  };

  const refresh = serialised<true>(async () => {
    const html = await fetchFragment(liveUrl);
    if (html === null) return;
    const answer = document.createElement("template");
    answer.innerHTML = html;
    const fresh = answer.content.getElementById(DETAIL_ID);
    const region = document.getElementById(DETAIL_ID);
    if (!region) return;
    if (fresh) morphInto(region, fresh);
    else showDeleted();
  });

  document.addEventListener(LIVE_EVENT, (event: Event) => {
    const signal = (event as CustomEvent<LiveSignal>).detail;
    if (signal.kind === "resync" || (signal.kind === "changed" && signal.pk === pk)) refresh(true);
    else if (signal.kind === "deleted" && signal.pk === pk) showDeleted();
  });
}
