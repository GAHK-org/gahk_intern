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

export function csrfToken(): string {
  const cookie = document.cookie.split("; ").find((c) => c.startsWith("csrftoken="));
  if (cookie) return decodeURIComponent(cookie.slice("csrftoken=".length));
  // Fallback for CSRF_USE_SESSIONS or a missing cookie: any rendered {% csrf_token %} input.
  return document.querySelector<HTMLInputElement>('input[name="csrfmiddlewaretoken"]')?.value ?? "";
}

document.body.addEventListener("htmx:configRequest", (event: Event) => {
  const detail = (event as CustomEvent<{ headers: Record<string, string> }>).detail;
  detail.headers["X-CSRFToken"] = csrfToken();
});
