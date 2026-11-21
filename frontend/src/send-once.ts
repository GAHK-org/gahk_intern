// One press, one send. Site-wide.
//
// A second press while the first send is still on its way would post the same thing twice: the
// message, reply, post or comment appears twice. It is easy to do, because a slow send (a phone
// photo, a weak signal) looks as if the first press did nothing. So:
//
//   SHOWN        the pressed button turns into a busy ring until the send is done (data-sending,
//                styles.css), so the press is seen to have been heard.
//
//   ORDINARY     Safari sends a form again on every press, and the server usually already has the
//   FORMS        first. A POST form, once sent, ignores further sends until Back brings its page
//                back. A form whose answer is a file, so the page stays, is marked data-sends-again
//                (Arkiv's downloads).
//
//   HTMX         a second request from the same element is QUEUED by default and sent when the
//                first is done. A press on an element whose write is still on its way is dropped.
//                Only presses (a click, a submit): a write sent as you type, like the opslagstavle
//                preview, must still send its latest text.
//
//   TOKENS       the guarantee is the server's (core/send_once.py): a form's send token is saved
//                with what it sent, and a token it has seen is the same send again. So the token
//                is renewed whenever the form may send something NEW: after an htmx send that
//                leaves the form on the page, and when a page comes back from history.

const sent = new WeakSet<HTMLFormElement>();

/** The form an ordinary (not htmx, not cancelled) POST submit sends, if this event is one. Its write
 *  lands after the page is gone. */
export function plainPost(event: SubmitEvent): HTMLFormElement | null {
  const form = event.target;
  if (event.defaultPrevented || !(form instanceof HTMLFormElement)) return null;
  const method = (event.submitter?.getAttribute("formmethod") ?? form.method).toLowerCase();
  if (method !== "post" || form.target === "_blank") return null;
  return form;
}

function newToken(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // randomUUID is missing outside a secure context (a phone testing a dev server over plain http).
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const hex = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function renewTokens(root: ParentNode): void {
  for (const input of root.querySelectorAll<HTMLInputElement>("input[data-send-token]")) input.value = newToken();
}

/** The button the press was on: the submitter, or the form's own for Ctrl+Enter and requestSubmit(). */
function pressed(form: HTMLFormElement, submitter: HTMLElement | null | undefined): HTMLElement | null {
  return submitter ?? form.querySelector<HTMLElement>('button[type="submit"], button:not([type]), input[type="submit"]');
}

function busy(button: HTMLElement | null, on: boolean): void {
  if (!button) return;
  button.toggleAttribute("data-sending", on);
  if (on) button.setAttribute("aria-busy", "true");
  else button.removeAttribute("aria-busy");
}

// ---- ordinary forms ----

// Registered before back-navigation.ts's listener (main.ts), which then sees a dropped send as
// cancelled and leaves it alone.
window.addEventListener("submit", (event: SubmitEvent) => {
  const form = plainPost(event);
  if (!form || form.hasAttribute("data-sends-again")) return;
  if (sent.has(form)) {
    event.preventDefault();
    return;
  }
  sent.add(form);
  busy(pressed(form, event.submitter), true);
});

// A page shown again from history: what it sent is done, and its forms may send something new.
// From the back/forward cache (the live page) the forms are unlocked; from the HTTP cache this
// script runs afresh on the old HTML, whose tokens may have been used.
window.addEventListener("pageshow", (event: PageTransitionEvent) => {
  if (!event.persisted) return;
  for (const form of document.forms) sent.delete(form);
  for (const button of document.querySelectorAll<HTMLElement>("[data-sending]")) busy(button, false);
  renewTokens(document);
});
const navigation = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
if (navigation?.type === "back_forward") renewTokens(document);

// ---- htmx ----

type RequestConfig = { elt: Element; verb: string; triggeringEvent?: Event | null };
type RequestDetail = { requestConfig: RequestConfig; successful?: boolean };
type ConfirmDetail = { elt: Element; verb: string; triggeringEvent?: Event | null };

const inFlight = new WeakMap<Element, HTMLElement | null>();

// beforeSend, not beforeRequest: a request cancelled at beforeRequest never gets an afterRequest, and
// would leave its element busy for good.
document.addEventListener("htmx:beforeSend", (event: Event) => {
  const { elt, verb, triggeringEvent } = (event as CustomEvent<RequestDetail>).detail.requestConfig;
  if (verb === "get") return;
  const button = elt instanceof HTMLFormElement ? pressed(elt, (triggeringEvent as SubmitEvent | null)?.submitter) : null;
  inFlight.set(elt, button);
  busy(button, true);
});

// Fired on success, on an error answer, on a lost connection and on an abort alike.
document.addEventListener("htmx:afterRequest", (event: Event) => {
  const { requestConfig, successful } = (event as CustomEvent<RequestDetail>).detail;
  const { elt } = requestConfig;
  if (!inFlight.has(elt)) return;
  busy(inFlight.get(elt) ?? null, false);
  inFlight.delete(elt);
  // Sent, and the form stays for the next one (Den Hurtige's composer and reply box). A failed send
  // keeps its token: sending it again is the same send, which the server may have saved after all.
  if (successful) renewTokens(elt);
});

// htmx:confirm comes first for every request, and a cancelled one is never sent or queued. Capture
// on the document, so it runs before the element's own confirm listeners: Den Hurtige's would open
// the duration sheet for a press that is going nowhere (feed.ts).
document.addEventListener(
  "htmx:confirm",
  (event: Event) => {
    const { elt, verb, triggeringEvent } = (event as CustomEvent<ConfirmDetail>).detail;
    if (verb === "get" || !inFlight.has(elt)) return;
    if (triggeringEvent?.type !== "submit" && triggeringEvent?.type !== "click") return;
    event.preventDefault();
    event.stopPropagation();
  },
  true,
);
