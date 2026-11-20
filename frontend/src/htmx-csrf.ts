// Django's CSRF token on every htmx request. Imported for its side effect; no-op without htmx.
//
// htmx does not add Django's CSRF header by itself, and a POST without it is a 403. Registered
// globally on document.body rather than per element, so every hx-post on the site is covered —
// including ones added long after this was written.
//
// Extracted from feed.ts, where it originally lived at module scope beside Den Hurtige's poll
// logic. It was already global there, so this is a move rather than a behaviour change — but a
// second feature (opslagstavlen) now depends on it, and depending on a file named after the chat
// feature is a trap: trimming feed.ts to the chat page would silently break CSRF elsewhere.

// settings.CSRF_COOKIE_NAME in production and in development, first match wins (a test reads this
// file to hold them together). Renamed from Django's default "csrftoken" with the move to
// intern.gahk.dk, so a stale cookie of the old name can never be the one read here. Production's
// carries the __Host- prefix, which no other host can set, so it is preferred over the bare name.
const CSRF_COOKIES = ["__Host-intern_csrftoken", "intern_csrftoken"];

export function csrfToken(): string {
  const cookies = document.cookie.split("; ");
  for (const name of CSRF_COOKIES) {
    const cookie = cookies.find((c) => c.startsWith(`${name}=`));
    if (cookie) return decodeURIComponent(cookie.slice(name.length + 1));
  }
  // Fallback for CSRF_USE_SESSIONS or a missing cookie: any rendered {% csrf_token %} input.
  return document.querySelector<HTMLInputElement>('input[name="csrfmiddlewaretoken"]')?.value ?? "";
}

document.body.addEventListener("htmx:configRequest", (event: Event) => {
  const detail = (event as CustomEvent<{ headers: Record<string, string> }>).detail;
  detail.headers["X-CSRFToken"] = csrfToken();
});
