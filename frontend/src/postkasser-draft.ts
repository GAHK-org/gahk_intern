// Postkasser: autosave of a kladde (draft) while a mail is being written. No-op on every page without a
// [data-draft-form], i.e. everywhere but the compose/reply page.
//
// The draft lives in the intranet only (postkasser.models.MailDraft); nothing goes to one.com. The form
// also has a plain "Gem kladde" button, so this is a convenience, never the only way to keep the text.
//
// The first save has no pk: the server makes the draft and answers with its pk, which goes into the form's
// hidden `draft` field so every later save (and the final Send) refers to the same draft. That is why two
// saves are never in flight at once: the second would not know the pk the first is about to return, and
// the server would have to fall back on finding the draft by its sag. What was typed meanwhile is queued
// as ONE more save, of the latest text, when the first answers. A failed save is NOT retried on its own
// (it would loop on a 400 or a logged-out session); the next keystroke tries again, after a back-off.

import { csrfToken } from "./htmx-csrf";

const form = document.querySelector<HTMLFormElement>("[data-draft-form]");

if (form && form.dataset.draftUrl) {
  const url = form.dataset.draftUrl;
  const pkField = form.querySelector<HTMLInputElement>('input[name="draft"]');
  const status = form.querySelector<HTMLElement>("[data-draft-status]");

  const DEBOUNCE_MS = 2000;
  const MAX_DELAY_MS = 60_000;
  // A request body above this is not sent with `keepalive`: browsers cap keepalive bodies at 64 KiB and
  // reject a larger one outright, which would lose the save. Such a save is a normal fetch instead.
  const KEEPALIVE_MAX_BYTES = 60_000;

  let timer = 0;
  let saving = false; // a request is in flight
  let dirty = false; // there is text the server does not have
  let typedDuringFlight = false; // the reader typed while a request was out
  let failures = 0; // consecutive failed saves, for the back-off
  let flight: Promise<void> = Promise.resolve(); // settles when the request in flight has finished
  let endFlight: () => void = () => {};
  let inFlight: AbortController | null = null; // the request in flight, so Send can cancel it
  let submitted = false; // the form is being sent (or saved the plain way): the page is on its way out

  const say = (text: string): void => {
    if (status) status.textContent = text;
  };

  /** Normal wait after typing; doubled for each failed save in a row, up to a minute. */
  const delay = (): number => Math.min(DEBOUNCE_MS * 2 ** failures, MAX_DELAY_MS);

  const bodyBytes = (body: FormData): number => {
    const encoder = new TextEncoder();
    let size = 0;
    for (const [key, value] of body) size += encoder.encode(key + (typeof value === "string" ? value : "")).length;
    return size;
  };

  const schedule = (wait: number): void => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => void save(), wait);
  };

  /** `keepalive` lets the request outlive the page (for the save when the tab is hidden). */
  const save = async (keepalive = false): Promise<void> => {
    window.clearTimeout(timer);
    if (submitted || !dirty) return;
    if (saving) return; // the finished save looks at `typedDuringFlight`
    saving = true;
    dirty = false;
    typedDuringFlight = false;
    const body = new FormData(form);
    const controller = new AbortController();
    inFlight = controller;
    flight = new Promise<void>((resolve) => (endFlight = resolve));
    try {
      const response = await fetch(url, {
        method: "POST",
        body,
        signal: controller.signal,
        credentials: "same-origin",
        keepalive: keepalive && bodyBytes(body) < KEEPALIVE_MAX_BYTES,
        headers: {
          "X-CSRFToken": csrfToken(),
          "X-Requested-With": "XMLHttpRequest",
          Accept: "application/json",
        },
      });
      const data = (await response.json().catch(() => null)) as {
        ok?: boolean;
        draft?: number;
        saved_at?: string;
        error?: string;
      } | null;
      if (response.ok && data?.ok) {
        failures = 0;
        if (pkField && data.draft != null) pkField.value = String(data.draft);
        say(`Gemt kl. ${data.saved_at ?? ""}`.trim());
      } else {
        failures += 1;
        dirty = true; // kept for the next change; there is NO immediate retry
        if (data === null) {
          // Not JSON: a redirect to the login page, or a CSRF refusal.
          say("Kunne ikke gemme — er du logget ud?");
        } else if (response.status === 404 && pkField?.value) {
          // The draft is gone (sent or deleted in another tab): the next save makes a new one.
          pkField.value = "";
          say(data.error || "Kladden findes ikke længere");
        } else {
          say(data.error || "Kunne ikke gemme kladden");
        }
      }
    } catch {
      if (submitted) return; // aborted by Send: not a failure worth showing
      failures += 1;
      dirty = true; // network down: try again at the next change
      say("Kunne ikke gemme kladden");
    } finally {
      saving = false;
      inFlight = null;
      endFlight();
    }
    // Only what the reader typed meanwhile earns another save; a failure waits for the next keystroke.
    if (typedDuringFlight && dirty && !submitted && !document.hidden) schedule(delay());
  };

  form.addEventListener("input", () => {
    dirty = true;
    if (saving) typedDuringFlight = true;
    schedule(delay());
  });

  // Never save after Send (or "Gem kladde"): the server deletes the draft on send, and a save arriving
  // after that would bring it back.
  // A save still in flight is aborted too: it could otherwise reach the server after Send and, with no pk
  // yet, create the draft anew (the server refuses that as well, see views.save_draft).
  form.addEventListener("submit", () => {
    submitted = true;
    window.clearTimeout(timer);
    inFlight?.abort();
  });

  // A hidden tab may be closed without further notice, so save what is pending now. sendBeacon cannot
  // carry the CSRF header, hence fetch with keepalive.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && dirty) void save(true);
  });

  // The way back ("‹ Tilbage til sagen" / "‹ Til tavlen") is a plain link, but the last keystrokes may not
  // be saved yet: wait for the request in flight and send one more save — at most 1.5 s — then go.
  for (const link of form.querySelectorAll<HTMLAnchorElement>("[data-draft-back]")) {
    link.addEventListener("click", (event) => {
      if (submitted || (!dirty && !saving)) return;
      event.preventDefault();
      const flush = async (): Promise<void> => {
        while (saving) await flight;
        if (dirty) await save();
      };
      const patience = new Promise<void>((resolve) => window.setTimeout(resolve, 1500));
      void Promise.race([flush(), patience]).finally(() => {
        window.location.href = link.href;
      });
    });
  }
}
