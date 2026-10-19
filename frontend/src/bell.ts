// The bell in the top bar, live. No-op where there is no bell (logged out, public pages).
//
// A signal on this resident's inbox topic (app/notifications/live.py) means the unseen count may
// have moved, either way: something arrived, or something was read in another tab. The badge is
// re-read on its own (notifications:badge); the panel only if it is open — it is fetched on every
// click anyway (_bell.html), so a closed panel has nothing that could go stale.

import { LIVE_EVENT, concerns, topicsOf, type LiveSignal } from "./live";
import { fetchFragment, serialised } from "./live-region";

const bell = document.querySelector<HTMLElement>(".notif-bell[data-live]");

if (bell) {
  const topics = topicsOf(bell);
  const badgeUrl = bell.dataset.badgeUrl ?? "";
  const htmx = (): { ajax: (verb: string, url: string, target: string) => unknown } | undefined =>
    (window as unknown as { htmx?: { ajax: (verb: string, url: string, target: string) => unknown } }).htmx;

  const refresh = serialised<true>(async () => {
    const html = await fetchFragment(badgeUrl);
    if (!html) return;
    const answer = document.createElement("template");
    answer.innerHTML = html;
    const fresh = answer.content.getElementById("js-notif-badge");
    if (fresh) document.getElementById("js-notif-badge")?.replaceWith(fresh);

    const panel = document.getElementById("notif-panel");
    const button = bell.querySelector<HTMLElement>("[hx-get]");
    const url = button?.getAttribute("hx-get");
    // x-show hides it with display:none, so "is it open" is "does it take up any room".
    if (panel && url && panel.getClientRects().length > 0) htmx()?.ajax("GET", url, "#notif-panel");
  });

  document.addEventListener(LIVE_EVENT, (event: Event) => {
    if (concerns((event as CustomEvent<LiveSignal>).detail, topics)) refresh(true);
  });
}
