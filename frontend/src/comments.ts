// Comment threads on opslagstavlen and begivenheder: "Svar" under a comment opens the reply box at
// the end of its thread (templates/opslagstavle/_reply_form.html, _comment_actions.html). No-op on
// pages without one.
//
// Delegated from document, so threads that a live re-draw adds work without re-wiring. The box
// carries data-morph-skip, so a re-draw leaves it open and keeps what is typed in it.
//
// Replies are one level deep (core.comment_threads), so "Svar" on a REPLY opens the same box and
// starts it with an @-mention of the person answered (data-reply-mention, a core.mentions token).
// That mention is what tells them, and what tells a reader of the thread who is being answered.

function openReply(button: HTMLElement): void {
  const form = document.getElementById(button.dataset.replyTo ?? "");
  if (!(form instanceof HTMLFormElement)) return;
  form.hidden = false;
  const area = form.querySelector("textarea");
  if (!area) return;
  const mention = button.dataset.replyMention;
  // Prepended once: pressing "Svar" on the same reply twice must not name them twice.
  if (mention && !area.value.includes(mention.trim())) area.value = mention + area.value;
  area.focus();
  area.setSelectionRange(area.value.length, area.value.length);
  form.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function closeReply(form: HTMLFormElement): void {
  // reset() also clears the attached photo; feed.ts hides the file note on the form's reset event.
  form.reset();
  form.hidden = true;
  document.querySelector<HTMLElement>(`[data-reply-to="${CSS.escape(form.id)}"]`)?.focus();
}

document.addEventListener("click", (event: MouseEvent) => {
  const target = event.target instanceof Element ? event.target : null;
  const button = target?.closest<HTMLElement>("[data-reply-to]");
  if (button) {
    openReply(button);
    return;
  }
  const form = target?.closest("[data-reply-cancel]")?.closest("form");
  if (form) closeReply(form);
});

// Escape in an empty reply box closes it; with text in it, Escape does nothing, so a stray key
// cannot throw away an answer.
document.addEventListener("keydown", (event: KeyboardEvent) => {
  if (event.key !== "Escape" || !(event.target instanceof HTMLTextAreaElement)) return;
  const form = event.target.closest<HTMLFormElement>(".comment-reply-form");
  if (form && !event.target.value.trim()) closeReply(form);
});
