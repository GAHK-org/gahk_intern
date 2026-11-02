// Søndagsrengøring: Inspektionen's checklist (templates/rengoering/inspect.html). No-op elsewhere.
//
// Every tick and every pause in typing a note is saved straight away, the way the prototype the
// round on 1/11/2026 was walked with did it: the inspector is on a staircase with a phone, the
// screen locks, the page gets reloaded — nothing ticked may be lost. The server is one endpoint per
// area (rengoering.views.inspect_save); this posts the whole form with action=gem, so a save always
// carries the complete state and saves arriving out of order cannot leave a half-ticked list.
// Godkend / Mangler stay ordinary form submits, and "Gem" is the fallback without this script.
//
// "Kopiér mangler" builds the message to the alumne from what is NOT ticked, grouped by zone, plus
// the note and the deadline — what Inspektionen sends on Den Hurtige or by SMS until the site
// notifies the alumne itself.

import { csrfToken } from "./htmx-csrf";

const SAVE_DELAY_MS = 600;

function copyText(form: HTMLFormElement): string {
  const d = form.dataset;
  const lines = [`Hej ${d.name ?? ""}`.trim() + ",", `Rengøringen af ${d.area} (${d.date}) mangler:`];
  const zones = new Set(Array.from(form.querySelectorAll<HTMLElement>("label[data-zone]")).map((l) => l.dataset.zone));
  let lastZone: string | undefined;
  let missing = 0;
  form.querySelectorAll<HTMLInputElement>('input[name="task"]').forEach((box) => {
    if (box.checked) return;
    const label = box.closest<HTMLElement>("label");
    const zone = label?.dataset.zone;
    if (zones.size > 1 && zone !== lastZone) {
      lines.push("", `${zone}:`);
      lastZone = zone;
    }
    lines.push(`- ${(label?.textContent ?? "").replace(/^\s*\*\s*/, "").trim()}`);
    missing++;
  });
  if (!missing) lines.push("- (ingen afkrydsede mangler)");
  const note = form.querySelector<HTMLTextAreaElement>('textarea[name="note"]')?.value.trim();
  if (note) lines.push("", note);
  const deadline = form.querySelector<HTMLInputElement>('input[name="deadline"]')?.value;
  if (deadline) {
    const when = new Date(deadline);
    const day = when.toLocaleDateString("da-DK", { weekday: "long", day: "numeric", month: "numeric" });
    const time = when.toLocaleTimeString("da-DK", { hour: "2-digit", minute: "2-digit" }).replace(":", ".");
    lines.push("", `Skal udbedres inden ${day} kl. ${time}.`);
  }
  return lines.join("\n");
}

document.querySelectorAll<HTMLFormElement>("form[data-rg-check]").forEach((form) => {
  const card = form.closest("details");
  const bar = card?.querySelector<HTMLElement>("[data-bar]");
  const saved = form.querySelector<HTMLElement>("[data-saved]");
  const copy = form.querySelector<HTMLButtonElement>("[data-copy]");
  const fallback = form.querySelector<HTMLTextAreaElement>("[data-copy-fallback]");
  const total = Number(form.dataset.total ?? 0);
  const boxes = Array.from(form.querySelectorAll<HTMLInputElement>('input[name="task"]'));
  let timer: number | undefined;
  let queue: Promise<void> = Promise.resolve();

  form.querySelectorAll<HTMLElement>("[data-nojs]").forEach((el) => (el.hidden = true));
  if (copy) copy.hidden = false;

  const say = (text: string): void => {
    if (saved) saved.textContent = text;
  };

  const progress = (): void => {
    const done = boxes.filter((b) => b.checked).length;
    if (bar) bar.style.width = `${total ? (done / total) * 100 : 0}%`;
    boxes.forEach((b) => b.closest("label")?.classList.toggle("done", b.checked));
  };

  const save = (): void => {
    const body = new FormData(form);
    body.set("action", "gem");
    say("Gemmer…");
    // One save at a time, in order: the last one to land is always the newest state.
    queue = queue.then(() =>
      fetch(form.action, {
        method: "POST",
        body,
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrfToken() },
      })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
        .then((data: { ok: boolean; error?: string }) => say(data.ok ? "Gemt" : (data.error ?? "Ikke gemt")))
        .catch(() => say("Ikke gemt – tjek forbindelsen og prøv igen")),
    );
  };

  const schedule = (): void => {
    window.clearTimeout(timer);
    timer = window.setTimeout(save, SAVE_DELAY_MS);
  };

  form.addEventListener("change", (event) => {
    if ((event.target as HTMLElement).matches('input[name="task"]')) {
      progress();
      schedule();
    }
  });
  form.querySelector('textarea[name="note"]')?.addEventListener("input", schedule);
  // A decision posts the same fields itself, so a pending autosave would only race it.
  form.addEventListener("submit", () => window.clearTimeout(timer));

  copy?.addEventListener("click", () => {
    const text = copyText(form);
    const showFallback = (): void => {
      if (!fallback) return;
      fallback.value = text;
      fallback.hidden = false;
      fallback.select();
    };
    if (!navigator.clipboard) return showFallback();
    navigator.clipboard.writeText(text).then(() => say("Kopieret"), showFallback);
  });

  progress();
});
