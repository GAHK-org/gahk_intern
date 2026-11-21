// The top bar's site-wide search (base.html, [data-site-search]). htmx fetches the suggestions into
// #search-suggestions as you type; this opens and closes that panel and moves through its options
// with the arrow keys, the way a combobox does. Enter on a highlighted option opens it; Enter with
// none submits the form, which opens every result (/search/?q=…). "/" anywhere else focuses the field.

const form = document.querySelector<HTMLFormElement>("[data-site-search]");
const input = form?.querySelector<HTMLInputElement>("input[name=q]");
const panel = form?.querySelector<HTMLElement>("#search-suggestions");

if (form && input && panel) {
  let active = -1;

  const options = (): HTMLAnchorElement[] => [...panel.querySelectorAll<HTMLAnchorElement>("[role=option]")];

  const setActive = (index: number): void => {
    const all = options();
    active = all.length ? (index + all.length) % all.length : -1;
    all.forEach((option, i) => option.setAttribute("aria-selected", String(i === active)));
    const current = all[active];
    if (current) {
      input.setAttribute("aria-activedescendant", current.id);
      current.scrollIntoView({ block: "nearest" });
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  };

  const open = (): void => {
    const has = panel.childElementCount > 0 && input.value.trim().length > 0;
    panel.hidden = !has;
    input.setAttribute("aria-expanded", String(has));
  };

  const close = (): void => {
    panel.hidden = true;
    input.setAttribute("aria-expanded", "false");
    setActive(-1);
  };

  // A fresh set of suggestions: number the options for aria-activedescendant, start with none picked.
  panel.addEventListener("htmx:afterSwap", () => {
    options().forEach((option, i) => (option.id = `search-option-${i}`));
    active = -1;
    input.removeAttribute("aria-activedescendant");
    if (document.activeElement === input) open();
  });

  input.addEventListener("focus", open);
  input.addEventListener("input", () => {
    if (!input.value.trim()) {
      panel.replaceChildren();
      close();
    }
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (panel.hidden) open();
      if (panel.hidden) return;
      event.preventDefault();
      setActive(active + (event.key === "ArrowDown" ? 1 : -1));
    } else if (event.key === "Enter" && !panel.hidden && active >= 0) {
      event.preventDefault();
      options()[active]?.click();
    } else if (event.key === "Escape" && !panel.hidden) {
      event.preventDefault();
      close();
    }
  });

  document.addEventListener("click", (event) => {
    if (!form.contains(event.target as Node)) close();
  });
  form.addEventListener("focusout", (event) => {
    if (!form.contains(event.relatedTarget as Node | null)) close();
  });

  // "/" focuses the search, as on GitHub, unless you are typing somewhere already.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
    const target = event.target as HTMLElement | null;
    if (target?.closest("input, textarea, select, [contenteditable=''], [contenteditable=true]")) return;
    event.preventDefault();
    input.focus();
    input.select();
  });
}
