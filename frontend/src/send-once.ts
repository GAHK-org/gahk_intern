// One press, one send. Site-wide.
//
// A second press while the first send is still on its way posts the same thing twice: the message,
// reply, post or comment appears twice. It is easy to do, because a slow send (a phone photo, a weak
// signal) looks as if the first press did nothing.
//
//   ORDINARY FORMS   the browser does not stop it. A second submit replaces the page load the first
//                    one started, but not the first request, which the server usually has already.
//                    So a POST form, once sent, ignores further sends until the page is shown again
//                    (Back). A form whose answer is a file, so the page stays, is marked
//                    data-sends-again (Arkiv's downloads).
//
//   HTMX             a second request from the same element is QUEUED by default and sent when the
//                    first is done. So a press on an element whose write is still on its way is
//                    dropped. Only presses (a click, a submit): a write sent as you type, like the
//                    opslagstavle preview, must still send its latest text.
//
// The server checks too (core/resend.py): this cannot stop a retry by the browser, or a second tab.

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
});

window.addEventListener("pageshow", (event: PageTransitionEvent) => {
  // Back to a page kept whole: what it sent is done, and its forms may be used again.
  if (event.persisted) for (const form of document.forms) sent.delete(form);
});

// ---- htmx ----

type RequestDetail = { requestConfig: { elt: Element; verb: string } };
type ConfirmDetail = { elt: Element; verb: string; triggeringEvent?: Event | null };

const busy = new WeakSet<Element>();

// beforeSend, not beforeRequest: a request cancelled at beforeRequest never gets an afterRequest, and
// would leave its element busy for good.
document.addEventListener("htmx:beforeSend", (event: Event) => {
  const { elt, verb } = (event as CustomEvent<RequestDetail>).detail.requestConfig;
  if (verb !== "get") busy.add(elt);
});

// Fired on success, on an error answer, on a lost connection and on an abort alike.
document.addEventListener("htmx:afterRequest", (event: Event) => {
  busy.delete((event as CustomEvent<RequestDetail>).detail.requestConfig.elt);
});

// htmx:confirm comes first for every request, and a cancelled one is never sent or queued. Capture
// on the document, so it runs before the element's own confirm listeners: Den Hurtige's would open
// the duration sheet for a press that is going nowhere (feed.ts).
document.addEventListener(
  "htmx:confirm",
  (event: Event) => {
    const { elt, verb, triggeringEvent } = (event as CustomEvent<ConfirmDetail>).detail;
    if (verb === "get" || !busy.has(elt)) return;
    if (triggeringEvent?.type !== "submit" && triggeringEvent?.type !== "click") return;
    event.preventDefault();
    event.stopPropagation();
  },
  true,
);
