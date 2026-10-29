// Opslagstavlen's Markdown compose toolbar and image upload. No-op on every page without
// #js-md-editor.
//
// Vanilla TS rather than Alpine on purpose: this is imperative selectionStart/selectionEnd
// arithmetic on a textarea, which is the one job Alpine's declarative model makes harder rather than
// easier. The tab switching next to it *is* declarative, and is done with two radios and CSS — no JS
// at all (see templates/opslagstavle/form.html).
//
// The preview is deliberately NOT rendered here. It is an htmx POST to the server, so it goes
// through the same core.markdown call the reader's page uses and cannot disagree with it. A
// client-side Markdown renderer would be a second implementation with a second sanitiser allowlist.

import { downscaleImage } from "./imageupload";

const root = document.getElementById("js-md-editor");

/** Insert `text`, replacing the selection, and leave the caret after it.
 *
 * setRangeText rather than splicing `.value`: it preserves the browser's native undo stack, so
 * Ctrl+Z after clicking a toolbar button does what the author expects. (The CMS admin's
 * insert_image.js splices, because it predates this and cannot import from the bundle — that file
 * and this one are deliberate duplicates; see the note there.) */
function insert(area: HTMLTextAreaElement, text: string, caretOffset = text.length): void {
  const start = area.selectionStart;
  if (typeof area.setRangeText === "function") {
    area.setRangeText(text, start, area.selectionEnd, "end");
  } else {
    area.value = area.value.slice(0, start) + text + area.value.slice(area.selectionEnd);
  }
  area.selectionStart = area.selectionEnd = start + caretOffset;
  area.focus();
  // The form is not re-rendered, so nothing else would notice the value changed.
  area.dispatchEvent(new Event("input", { bubbles: true }));
}

/** Wrap the selection in `before`/`after`, or drop in a placeholder and select it. */
function wrap(area: HTMLTextAreaElement, before: string, after: string, placeholder: string): void {
  const selected = area.value.slice(area.selectionStart, area.selectionEnd);
  const body = selected || placeholder;
  const start = area.selectionStart;
  insert(area, before + body + after);
  if (!selected) {
    // Select the placeholder so typing replaces it — otherwise the author has to delete it first.
    area.selectionStart = start + before.length;
    area.selectionEnd = start + before.length + body.length;
    area.focus();
  }
}

/** Prefix every line the selection touches, for the block-level buttons. */
function prefixLines(area: HTMLTextAreaElement, prefix: string): void {
  const value = area.value;
  const lineStart = value.lastIndexOf("\n", area.selectionStart - 1) + 1;
  const lineEnd = value.indexOf("\n", area.selectionEnd);
  const end = lineEnd === -1 ? value.length : lineEnd;
  const block = value.slice(lineStart, end) || "";
  const prefixed = block
    .split("\n")
    .map((line) => (line.startsWith(prefix) ? line : prefix + line))
    .join("\n");
  area.selectionStart = lineStart;
  area.selectionEnd = end;
  insert(area, prefixed);
}

const ACTIONS: Record<string, (area: HTMLTextAreaElement) => void> = {
  bold: (a) => wrap(a, "**", "**", "fed tekst"),
  italic: (a) => wrap(a, "*", "*", "kursiv"),
  code: (a) => wrap(a, "`", "`", "kode"),
  // No h1: the page owns its single <h1> (the title field), and core.markdown demotes one anyway.
  heading: (a) => prefixLines(a, "## "),
  list: (a) => prefixLines(a, "- "),
  quote: (a) => prefixLines(a, "> "),
  link: (a) => {
    const selected = a.value.slice(a.selectionStart, a.selectionEnd);
    if (selected) wrap(a, "[", "](https://)", selected);
    else insert(a, "[tekst](https://)", 1);
  },
};

function init(form: HTMLElement): void {
  const area = form.querySelector<HTMLTextAreaElement>("textarea[name='body']");
  const status = form.querySelector<HTMLElement>("[data-md-status]");
  const fileInput = form.querySelector<HTMLInputElement>("[data-md-image]");
  const uploadUrl = form.dataset.uploadUrl ?? "";
  if (!area) return;

  const say = (text: string): void => {
    if (status) status.textContent = text;
  };

  for (const button of Array.from(form.querySelectorAll<HTMLButtonElement>("[data-md]"))) {
    const action = ACTIONS[button.dataset.md ?? ""];
    if (action) button.addEventListener("click", () => action(area));
  }

  // Ctrl/Cmd+B and +I, because everyone tries them.
  area.addEventListener("keydown", (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
    const key = event.key.toLowerCase();
    if (key !== "b" && key !== "i") return;
    event.preventDefault();
    ACTIONS[key === "b" ? "bold" : "italic"](area);
  });

  if (!fileInput || !uploadUrl) return;

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    fileInput.value = ""; // so picking the same file twice fires `change` again
    say("Uploader…");

    try {
      // Same downscale (1600px / JPEG q0.82) the room-inspection and Den Hurtige forms use, imported
      // rather than reimplemented — the server cap is a backstop, not the first line of defence.
      const shrunk = await downscaleImage(file);
      const data = new FormData();
      data.append("file", shrunk);
      const response = await fetch(uploadUrl, {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken() },
        credentials: "same-origin",
        body: data,
      });
      const payload = (await response.json()) as { url?: string; alt?: string; error?: string };
      if (!response.ok || !payload.url) {
        // The server's message is Danish and specific ("filendelsen passer ikke til et billede…"),
        // so show it rather than a generic failure.
        say(payload.error || `Upload mislykkedes (${response.status}).`);
        return;
      }
      insert(area, `\n![${payload.alt || ""}](${payload.url})\n`);
      say("Billedet er indsat.");
    } catch (err) {
      console.error("opslagstavle upload:", err);
      say("Upload mislykkedes. Prøv igen.");
    }
  });
}

/** Duplicated from htmx-csrf.ts, which reads the token for htmx requests rather than fetch ones.
 * Two callers, four lines; sharing it would mean a module whose only job is one cookie lookup. */
function csrfToken(): string {
  const cookie = document.cookie.split("; ").find((c) => c.startsWith("csrftoken="));
  if (cookie) return decodeURIComponent(cookie.slice("csrftoken=".length));
  return document.querySelector<HTMLInputElement>('input[name="csrfmiddlewaretoken"]')?.value ?? "";
}

/** Put an embed (`![Julefrokost 2026](album:12)`) in the body at the cursor, on a paragraph of its
 * own: an embed is drawn as a widget after the paragraph it is in, so one alone is drawn exactly
 * where it was put. */
function insertEmbed(area: HTMLTextAreaElement, markdown: string): void {
  const before = area.value.slice(0, area.selectionStart);
  const after = area.value.slice(area.selectionEnd);
  const lead = before === "" || before.endsWith("\n\n") ? "" : before.endsWith("\n") ? "\n" : "\n\n";
  const trail = after.startsWith("\n\n") ? "" : after.startsWith("\n") ? "\n" : "\n\n";
  insert(area, `${lead}${markdown}${trail}`, lead.length + markdown.length);
}

/** Take every embed of `reference` (`album:12`) out of the body, with the blank line it leaves. */
function removeEmbed(area: HTMLTextAreaElement, reference: string): void {
  const escaped = reference.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const embed = new RegExp(`!\\[(?:\\\\.|[^\\]\\\\])*\\]\\(${escaped}\\)`, "g");
  area.value = area.value.replace(embed, "").replace(/\n{3,}/g, "\n\n").trim();
  area.dispatchEvent(new Event("input", { bubbles: true }));
}

/** The composer's pickers for an album and a film (form.html's <dialog>s), and "Fjern" on the
 * widgets under the textarea (_embedded.html).
 *
 * Opening a picker empties its search and fires `embed-open`, which its hx-trigger answers with the
 * newest albums or films. Picking one embeds it in the body, where the cursor was, and the widgets
 * under the textarea re-draw on the `input` that fires. Clicking the backdrop closes a picker, as
 * Escape does by itself.
 */
function initEmbeds(area: HTMLTextAreaElement): void {
  document.addEventListener("click", (event) => {
    const target = event.target as Element;
    const opener = target.closest<HTMLElement>("[data-embed-open]");
    if (opener) {
      const dialog = document.getElementById(`js-embed-${opener.dataset.embedOpen}`);
      if (!(dialog instanceof HTMLDialogElement)) return;
      const search = dialog.querySelector<HTMLInputElement>("input[type='search']");
      dialog.showModal();
      if (search) {
        search.value = "";
        search.focus();
        search.dispatchEvent(new Event("embed-open"));
      }
      return;
    }
    const pick = target.closest<HTMLElement>("[data-embed-pick]");
    if (pick) {
      pick.closest("dialog")?.close();
      insertEmbed(area, pick.dataset.embed ?? "");
      return;
    }
    const remove = target.closest<HTMLElement>("[data-embed-remove]");
    if (remove) {
      removeEmbed(area, remove.dataset.embedRemove ?? "");
      return;
    }
    if (target.closest("[data-embed-close]")) {
      target.closest("dialog")?.close();
      return;
    }
    // A click on the backdrop lands on the dialog itself, outside its box.
    if (target instanceof HTMLDialogElement && target.matches(".board-picker")) {
      const box = target.getBoundingClientRect();
      const { clientX: x, clientY: y } = event as MouseEvent;
      if (x < box.left || x > box.right || y < box.top || y > box.bottom) target.close();
    }
  });
}

if (root) {
  init(root);
  const area = root.querySelector<HTMLTextAreaElement>("textarea[name='body']");
  if (area) initEmbeds(area);
}
