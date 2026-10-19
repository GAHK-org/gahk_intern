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
//            deleted while the socket was down, and goes. The same request asks which posts were
//            created after the page was drawn (`?siden=`), and the banner is set from that.
//
// A re-fetched card that no longer belongs where it sits — pinned or unpinned, or moved out of the
// category this board is filtered to — is NOT morphed in place, which would leave it in the wrong
// strip; the banner says "Opslagstavlen er ændret" and links to this page redrawn.
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
  // The newest post that existed when the page was drawn; a resync asks what came after it.
  const newest = board.dataset.newest ?? "0";
  const topics = topicsOf(board);
  const banner = document.getElementById("js-board-new") as HTMLAnchorElement | null;
  const firstPage = banner?.href ?? "";
  // The new posts this page is not showing, by pk rather than counted: a resync can replace the
  // set with the server's own answer, and a post deleted again leaves it instead of being counted.
  let unseen = new Set<number>();
  // A card on screen that no longer belongs where it sits (pinned, unpinned, moved category).
  let moved = false;

  const cardsOnScreen = (): HTMLElement[] =>
    Array.from(board.querySelectorAll<HTMLElement>(`[id^="${CARD_PREFIX}"]`));
  const cardFor = (pk: number): HTMLElement | null => document.getElementById(`${CARD_PREFIX}${pk}`);

  const showBanner = (): void => {
    const label = banner?.querySelector("span");
    if (!banner || !label) return;
    const n = unseen.size;
    if (n) {
      label.textContent = `${n > 98 ? "99+" : n} ${n === 1 ? "nyt opslag" : "nye opslag"} — vis`;
      banner.href = firstPage; // new posts are on page 1
    } else if (moved) {
      label.textContent = "Opslagstavlen er ændret — vis";
      banner.href = location.href; // this page, redrawn
    }
    banner.hidden = !n && !moved;
  };

  /** Whether a re-fetched card still belongs where the old one sits. */
  const belongs = (old: HTMLElement, fresh: HTMLElement): boolean =>
    fresh.classList.contains("is-pinned") === !!old.closest(".board-pinned") &&
    (!category || fresh.dataset.category === category);

  // Items are card pks, plus "since" when the board must also ask what it missed (a resync).
  const refresh = serialised<number | "since">(async (items) => {
    const query = new URLSearchParams();
    for (const item of items) if (item !== "since") query.append("id", String(item));
    if (items.has("since")) {
      query.set("siden", newest);
      if (category) query.set("kategori", category);
    }
    const html = await fetchFragment(`${liveUrl}?${query}`);
    if (html === null) return;
    const answer = document.createElement("template");
    answer.innerHTML = html;
    for (const item of items) {
      if (item === "since") continue;
      const old = cardFor(item);
      if (!old) continue;
      const fresh = answer.content.getElementById(`${CARD_PREFIX}${item}`);
      if (!fresh) old.remove();
      else if (belongs(old, fresh)) morphInto(old, fresh);
      else moved = true;
    }
    const since = answer.content.getElementById("js-board-new-since");
    if (since) unseen = new Set((since.dataset.pks ?? "").split(",").filter(Boolean).map(Number));
    showBanner();
  });

  document.addEventListener(LIVE_EVENT, (event: Event) => {
    const signal = (event as CustomEvent<LiveSignal>).detail;
    if (!concerns(signal, topics)) return;
    if (signal.kind === "resync") {
      // Every card on screen, and what was posted while the socket was down, in one request.
      refresh("since");
      for (const card of cardsOnScreen()) refresh(Number(card.id.slice(CARD_PREFIX.length)));
      return;
    }
    if (signal.pk === null) return;
    if (signal.kind === "created") {
      if (category && signal.category !== category) return;
      unseen.add(signal.pk);
      showBanner();
    } else if (signal.kind === "deleted") {
      cardFor(signal.pk)?.remove();
      if (unseen.delete(signal.pk)) showBanner();
    } else if (cardFor(signal.pk)) {
      refresh(signal.pk);
    }
  });
}
