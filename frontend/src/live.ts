// The page's live connection: one WebSocket to the topic its template declares with `data-live`.
// No-op on every page without one.
//
// THIS FILE KNOWS NOTHING ABOUT ANY FEATURE. It turns server signals into a `gahk:live` event on
// the document and leaves the page to decide what a signal means (feed.ts, opslagstavle-live.ts).
// A signal is a kind and a primary key, never content — see app/core/live.py for why — so every
// listener answers it the same way: by re-fetching through the ordinary HTTP views.
//
// Two promises the listeners rely on:
//
//   * `<html data-live-connected>` is present exactly while the socket is open. Den Hurtige's poll
//     filters on it, so the old 5s poll comes back by itself whenever the socket is gone.
//   * After a RE-connection, a `resync` signal is raised. Whatever happened while the socket was
//     down — a phone asleep in a pocket, a deploy restarting the server — was never signalled, so
//     the page must catch up on everything it shows rather than wait for the next change.

export interface LiveSignal {
  kind: "created" | "changed" | "deleted" | "resync";
  pk: number | null;
  category?: string;
}

export const LIVE_EVENT = "gahk:live";

// The server's close code for "you may not listen to this" (core/consumers.py). Retrying cannot
// change the answer, so it ends the attempt; the page keeps its poll.
const REFUSED = 4403;
const RETRY_MIN_MS = 1000;
const RETRY_MAX_MS = 30000;
const CONNECTED_ATTR = "data-live-connected";

const topic = document.querySelector<HTMLElement>("[data-live]")?.dataset.live;

function emit(signal: LiveSignal): void {
  document.dispatchEvent(new CustomEvent<LiveSignal>(LIVE_EVENT, { detail: signal }));
}

if (topic && "WebSocket" in window) {
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live/${encodeURIComponent(topic)}/`;
  let socket: WebSocket | null = null;
  let retryMs = RETRY_MIN_MS;
  let retryTimer: number | undefined;
  let refused = false;
  // Whether a socket has ever been open on this page: the first open needs no resync, because the
  // page was rendered a moment ago. Every later one does.
  let hadConnection = false;

  const connect = (): void => {
    window.clearTimeout(retryTimer);
    if (refused || socket) return;
    const ws = new WebSocket(url);
    socket = ws;

    ws.addEventListener("open", () => {
      retryMs = RETRY_MIN_MS;
      document.documentElement.setAttribute(CONNECTED_ATTR, "");
      if (hadConnection) emit({ kind: "resync", pk: null });
      hadConnection = true;
    });

    ws.addEventListener("message", (event: MessageEvent) => {
      try {
        emit(JSON.parse(event.data as string) as LiveSignal);
      } catch {
        // A malformed frame is dropped: the backstop poll and the next resync cover it.
      }
    });

    ws.addEventListener("close", (event: CloseEvent) => {
      if (socket !== ws) return; // an old socket closing after a deliberate replacement
      socket = null;
      document.documentElement.removeAttribute(CONNECTED_ATTR);
      if (event.code === REFUSED) {
        refused = true;
        return;
      }
      // Not while the page is hidden: a phone in a pocket would otherwise retry all night. The
      // visibilitychange handler below reconnects the moment somebody looks again.
      if (document.visibilityState === "hidden") return;
      // Jittered, so a server restart is not met by every open tab in the same millisecond.
      retryTimer = window.setTimeout(connect, retryMs * (0.5 + Math.random()));
      retryMs = Math.min(retryMs * 2, RETRY_MAX_MS);
    });
  };

  const reconnectNow = (): void => {
    retryMs = RETRY_MIN_MS;
    connect();
  };

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") reconnectNow();
  });
  window.addEventListener("online", reconnectNow);

  // An open WebSocket keeps a page out of the back/forward cache in several browsers, and one that
  // is restored from it comes back with a socket the browser has already killed. Close on the way
  // out, and on the way back treat it as the reconnection it is.
  window.addEventListener("pagehide", () => {
    const ws = socket;
    socket = null;
    window.clearTimeout(retryTimer);
    document.documentElement.removeAttribute(CONNECTED_ATTR);
    ws?.close();
  });
  window.addEventListener("pageshow", (event: PageTransitionEvent) => {
    if (event.persisted) reconnectNow();
  });

  connect();
}
