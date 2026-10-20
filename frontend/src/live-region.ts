// Live regions: an element that re-draws itself from the server when a signal for it arrives.
// No-op on every page without a [data-live-region].
//
// The markup says everything:
//
//   data-live="topic …"    which topics to listen to (also what ./live subscribes the socket to)
//   data-live-region       this element is re-drawn on a signal
//   data-live-url          where to read it from; omitted = the page's own URL, which the view
//                          answers with just this element when asked by htmx (the repair board)
//   data-live-pk           only signals naming this pk (one post, one event); omitted = any signal
//   data-live-gone         what to say instead when the thing is deleted (a "deleted" signal, or a
//                          404 on re-reading); omitted = leave it as it is
//   data-live-busy         set by a page script while it must not be redrawn (a card mid-drag);
//                          a signal arriving meanwhile is held and served the moment it is removed
//
// The answer must contain an element with the region's own id; it is MORPHED into place (idiomorph),
// so only what changed is touched and anything marked data-morph-skip (a half-written comment) is
// not touched at all. htmx.process() afterwards wires the hx-* attributes on anything new.
//
// A 204 or a network failure leaves the region alone, and so does a 200 without the region in it —
// a session that expired has the login page answer, and that must not read as "it was deleted".
//
// The views behind data-live-url never render `messages`: these reads fire at moments the reader
// did not choose, and must not eat a flash message waiting for the next page.

// htmx is IMPORTED, never read off `window`: htmx 2's ES-module build (what main.ts bundles)
// defines no global, so `window.htmx` is undefined here and every `window.htmx?.…` call was a
// silent no-op. That is how Den Hurtige's feed came to ignore its live signals and update only on
// the 60s backstop. The import is the same instance main.ts loaded.
import htmx from "htmx.org";
import { Idiomorph } from "idiomorph/htmx";
import { LIVE_EVENT, concerns, topicsOf, type LiveSignal } from "./live";

/** Raised on a region by a page script that wants it re-read now (the channel picker opening). */
export const REFRESH_EVENT = "gahk:live-refresh";

/** The fragment at `url`, "" if the thing it draws is gone (404), or null to leave the page alone —
 * a 204 from an expired session, a network failure. */
export async function fetchFragment(url: string): Promise<string | null> {
  try {
    const response = await fetch(url, { credentials: "same-origin", headers: { "HX-Request": "true" } });
    if (response.status === 404) return "";
    if (response.status !== 200) return null;
    return await response.text();
  } catch {
    return null;
  }
}

export function morphInto(old: Element, fresh: Element): void {
  Idiomorph.morph(old, fresh, { morphStyle: "outerHTML" });
  const now = fresh.id ? document.getElementById(fresh.id) : null;
  htmx.process(now ?? old);
}

/** One request at a time, with everything that arrived meanwhile batched into the next — three
 * reactions in a second cost two fetches, and the last state is never the one skipped. */
export function serialised<T>(run: (batch: Set<T>) => Promise<void>): (item: T) => void {
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

function setUp(initial: HTMLElement): void {
  const id = initial.id;
  if (!id) return; // nothing to find in the answer; a template bug, not a runtime state
  const current = (): HTMLElement | null => document.getElementById(id);
  const topics = topicsOf(initial);
  const pk = initial.dataset.livePk ? Number(initial.dataset.livePk) : null;
  let held = false;

  const showGone = (): void => {
    const region = current();
    const text = region?.dataset.liveGone;
    if (!region || !text) return;
    const note = document.createElement("div");
    note.className = "card-flat card-pad";
    note.setAttribute("role", "status");
    const strong = document.createElement("strong");
    strong.textContent = text;
    note.append(strong);
    region.replaceWith(note);
  };

  const refresh = serialised<true>(async () => {
    const region = current();
    if (!region) return;
    const html = await fetchFragment(region.dataset.liveUrl || location.pathname + location.search);
    if (html === null) return;
    if (html === "") {
      showGone();
      return;
    }
    const answer = document.createElement("template");
    answer.innerHTML = html;
    const fresh = answer.content.getElementById(id);
    const now = current();
    if (!fresh || !now) return;
    if (now.hasAttribute("data-live-busy")) {
      held = true; // drawn while it waited; the drop that ends the drag re-asks
      return;
    }
    morphInto(now, fresh);
  });

  const ask = (): void => {
    const region = current();
    if (!region) return;
    if (region.hasAttribute("data-live-busy")) held = true;
    else refresh(true);
  };

  document.addEventListener(LIVE_EVENT, (event: Event) => {
    const signal = (event as CustomEvent<LiveSignal>).detail;
    if (!concerns(signal, topics)) return;
    if (signal.kind !== "resync" && pk !== null && signal.pk !== pk) return;
    if (signal.kind === "deleted" && pk !== null) showGone();
    else ask();
  });

  document.addEventListener(REFRESH_EVENT, (event: Event) => {
    if ((event.target as Element | null)?.id === id) ask();
  });

  // The busy flag coming off is the moment a held signal is served.
  new MutationObserver(() => {
    const region = current();
    if (held && region && !region.hasAttribute("data-live-busy")) {
      held = false;
      refresh(true);
    }
  }).observe(document.body, { subtree: true, attributes: true, attributeFilter: ["data-live-busy"] });
}

document.querySelectorAll<HTMLElement>("[data-live-region]").forEach(setUp);
