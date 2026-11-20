// Keeps a .findable-select's text in step with its <select> (alumneliste/next_month.html). The text
// is there because the browser's find never searches inside a select; the server renders it, and
// this only follows changes. No-op on pages without one.
//
// "pageshow" as well as "change": on reload or back, the browser puts the choices someone had made
// back into the selects without firing change, which would leave the text showing the saved value.

const SELECTS = ".findable-select select";

const sync = (select: HTMLSelectElement): void => {
  const label = select.parentElement?.querySelector<HTMLElement>(".findable-select-label");
  if (label) label.textContent = select.selectedOptions[0]?.text ?? "";
};

document.addEventListener("change", (event) => {
  if (event.target instanceof HTMLSelectElement && event.target.matches(SELECTS)) sync(event.target);
});

window.addEventListener("pageshow", () => {
  document.querySelectorAll<HTMLSelectElement>(SELECTS).forEach(sync);
});
