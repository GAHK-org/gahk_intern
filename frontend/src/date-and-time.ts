// The 24-hour time boxes of core.date_and_time (`input[data-clock]`). No-op on pages without one.
//
// A phone's number pad has no colon, so "1930" is a valid way to type half past seven. The server
// reads it either way; this only rewrites it as "19:30" once the box is left, so what is on screen
// is what will be saved. Anything that is not a time is left alone for the server to reject with
// a message, rather than being silently changed into something else.

const CLOCK = /^(\d{1,2})(?:[:.,]?(\d{2}))?$/;

export function tidyClock(text: string): string | null {
  const match = CLOCK.exec(text.trim().replace(/^kl\.\s*/i, ""));
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2] ?? 0);
  if (hour > 23 || minute > 59) return null;
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

document.addEventListener("change", (event) => {
  const box = event.target;
  if (!(box instanceof HTMLInputElement) || !box.matches("[data-clock]")) return;
  const tidy = tidyClock(box.value);
  if (tidy !== null && tidy !== box.value) box.value = tidy;
});
