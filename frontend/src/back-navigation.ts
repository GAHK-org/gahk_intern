// What a page does when the browser's Back (or Forward) button brings it back. Site-wide.
//
// Back does not load the page again: the browser shows the copy it kept, either the live page from
// its back/forward cache or the HTML from its HTTP cache. That copy is the page as it was BEFORE
// whatever was done next, so:
//
//   OLD DATA     the comment just written is missing, the item just moved is in its old column, the
//                button just pressed looks unpressed (and pressing it again does it twice).
//   SENT TEXT    Chrome fills the text you sent back into the form (#365), as if it were a draft.
//   OPEN MENUS   a menu whose link you followed is still open.
//
// OLD DATA: the page reloads when something was written since it was made. "Since" compares two times
// from the server's clock (core/back_navigation.py): the page's own request time, in
// <meta name="gahk-rendered">, and the time of the last successful write from this browser, in a
// cookie, so a write in another tab counts too. A page nothing has changed since is not reloaded, so
// an ordinary Back stays instant.
//
// The page's OWN changes are not a reason to reload it: what it changed with htmx or fetch it already
// shows. So when it is left by a link, everything written so far counts as shown. When it is left by
// SUBMITTING A FORM it does not, because that write is exactly what the kept copy is missing.

// The name carries __Host- in production only (settings.LAST_WRITE_COOKIE_NAME).
const LAST_WRITE = /(?:^|;\s*)(?:__Host-)?intern_last_write=(\d+)/;

// The menus that close when you click elsewhere (feed.ts's DISMISSABLE, plus the photo album's). The
// Alpine menus in base.html and the bell close themselves on the same event (@pageshow.window).
const OPEN_MENUS = "details.pop[open], details.channel-picker[open], details.album-menu[open]";

function lastWrite(): number {
  const match = document.cookie.match(LAST_WRITE);
  return match ? Number(match[1]) : 0;
}

const rendered = document.querySelector<HTMLMetaElement>('meta[name="gahk-rendered"]');

if (rendered) {
  // The newest write this page already reflects.
  let shown = Number(rendered.content) || 0;
  let leftBySubmit = false;

  const reloadIfStale = (): void => {
    if (lastWrite() > shown) window.location.reload();
  };

  // From the HTTP cache this script runs again, on the old HTML. A reload is not checked: it is a
  // fresh copy, and checking it could only ever loop.
  const navigation = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
  if (navigation?.type === "back_forward") reloadIfStale();

  // A form posted the ordinary way, not by htmx (which cancels the event before it gets here) and not
  // a search (GET). Its write lands after this page is gone, and this copy will not show it.
  //
  // autocomplete="off" from now on stops the browser keeping what was typed: Chrome otherwise types
  // the sent text back in when this page is shown again, even into a freshly loaded copy. Not on a
  // form with a password, where the attribute could confuse a password manager.
  window.addEventListener("submit", (event: SubmitEvent) => {
    const form = event.target;
    if (event.defaultPrevented || !(form instanceof HTMLFormElement)) return;
    const method = (event.submitter?.getAttribute("formmethod") ?? form.method).toLowerCase();
    if (method !== "post" || form.target === "_blank") return;
    leftBySubmit = true;
    if (!form.querySelector('input[type="password"]')) form.setAttribute("autocomplete", "off");
  });

  window.addEventListener("pagehide", () => {
    if (!leftBySubmit) shown = Math.max(shown, lastWrite());
  });

  window.addEventListener("pageshow", (event: PageTransitionEvent) => {
    if (!event.persisted) return;
    leftBySubmit = false;
    for (const menu of document.querySelectorAll<HTMLDetailsElement>(OPEN_MENUS)) menu.open = false;
    reloadIfStale();
  });
}
