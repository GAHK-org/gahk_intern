// The page's live connection: ONE WebSocket for every topic the page declares with `data-live`
// (space-separated where one element wants several). No-op on a page that declares none.
//
// Nearly every page declares at least one: the bell in the top bar listens to its resident's inbox.
// Den Hurtige adds its channels, a board or an event page its own. They all share the one socket,
// which is why every signal names its topic and every listener checks it.
//
// THIS FILE KNOWS NOTHING ABOUT ANY FEATURE. It turns server signals into a `gahk:live` event on
// the document and leaves the page to decide what a signal means (feed.ts, live-region.ts, bell.ts,
// opslagstavle-live.ts).
// A signal is a kind and a primary key, never content — see app/core/live.py for why — so every
// listener answers it the same way: by re-fetching through the ordinary HTTP views.
//
// Two promises the listeners rely on:
//
//   * `data-live-on` is on every `[data-live]` element whose topics the server actually JOINED, and
//     only while the socket is open. Den Hurtige's polls filter on it (`this.closest(...)`), so the
//     old 5s poll comes back by itself whenever the socket is gone — and also when the socket is
//     open but the server skipped this element's topic (core/consumers.py says why that can happen).
//     `<html data-live-connected>` marks only that a socket is open, and nothing should poll on it.
//   * After a RE-connection, a `resync` signal is raised. Whatever happened while the socket was
//     down — a phone asleep in a pocket, a deploy restarting the server — was never signalled, so
//     the page must catch up on everything it shows rather than wait for the next change.

export interface LiveSignal {
  /** null only on a resync, which concerns every topic at once. */
  topic: string | null;
  kind: "created" | "changed" | "deleted" | "resync";
  pk: number | null;
  category?: string;
}

/** The topics an element listens to: its `data-live`, split on whitespace. */
export function topicsOf(el: HTMLElement): string[] {
  return (el.dataset.live ?? "").split(/\s+/).filter(Boolean);
}

/** Whether `signal` concerns an element listening to `topics`. A resync concerns everyone. */
export function concerns(signal: LiveSignal, topics: string[]): boolean {
  return signal.topic === null || topics.includes(signal.topic);
}

export const LIVE_EVENT = "gahk:live";

// The server's close code for "you may not listen to this" (core/consumers.py). Retrying cannot
// change the answer, so it ends the attempt; the page keeps its poll.
const REFUSED = 4403;
const RETRY_MIN_MS = 1000;
const RETRY_MAX_MS = 30000;
const CONNECTED_ATTR = "data-live-connected";
const ON_ATTR = "data-live-on";

/** Mark the elements whose every topic was joined; unmark the rest. An empty list unmarks all. */
function markJoined(joined: string[]): void {
  const set = new Set(joined);
  document.querySelectorAll<HTMLElement>("[data-live]").forEach((el) => {
    const mine = topicsOf(el);
    el.toggleAttribute(ON_ATTR, mine.length > 0 && mine.every((t) => set.has(t)));
  });
}

const topics = [
  ...new Set(Array.from(document.querySelectorAll<HTMLElement>("[data-live]")).flatMap(topicsOf)),
];

function emit(signal: LiveSignal): void {
  document.dispatchEvent(new CustomEvent<LiveSignal>(LIVE_EVENT, { detail: signal }));
}

if (topics.length && "WebSocket" in window) {
  const query = new URLSearchParams(topics.map((t) => ["t", t]));
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live/?${query}`;
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
      if (hadConnection) emit({ topic: null, kind: "resync", pk: null });
      hadConnection = true;
    });

    ws.addEventListener("message", (event: MessageEvent) => {
      try {
        const message = JSON.parse(event.data as string) as LiveSignal | { kind: "subscribed"; topics: string[] };
        // The server's first word on every connection: which of the asked-for topics it joined.
        if (message.kind === "subscribed") markJoined(message.topics);
        else emit(message);
      } catch {
        // A malformed frame is dropped: the backstop poll and the next resync cover it.
      }
    });

    ws.addEventListener("close", (event: CloseEvent) => {
      if (socket !== ws) return; // an old socket closing after a deliberate replacement
      socket = null;
      document.documentElement.removeAttribute(CONNECTED_ATTR);
      markJoined([]);
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
    markJoined([]);
    ws?.close();
  });
  window.addEventListener("pageshow", (event: PageTransitionEvent) => {
    if (event.persisted) reconnectNow();
  });

  connect();
}
