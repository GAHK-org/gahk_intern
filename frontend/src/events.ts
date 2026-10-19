// Begivenheder's guest-list picker. No-op on every page without one.
//
// Three small jobs, none of which is worth Alpine:
//
//   1. Open the picker when "Kun inviterede" is chosen, and fold it away again when it is not.
//      The server renders the initial state (an event that is already invite-only opens expanded),
//      so this only has to follow changes.
//   2. Filter sixty names as you type. Client-side because the whole list is already in the DOM —
//      a round trip per keystroke to re-render a list we are holding would be slower and would
//      lose the checkboxes people had already ticked.
//   3. Keep the "N valgt" count honest, since it is the only feedback that the four-person minimum
//      has been met without submitting the form.
//
// Deliberately does NOT hide the checked ones when they fall out of a search. A ticked box that
// vanishes because you typed a different name reads as "it was unticked", and the count would be
// the only thing saying otherwise.

const picker = document.getElementById("js-invitees") as HTMLDetailsElement | null;

if (picker) {
  const search = document.getElementById("js-invite-search") as HTMLInputElement | null;
  const counter = picker.querySelector<HTMLElement>("[data-invite-count]");
  const boxes = [...picker.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];

  const rowOf = (box: HTMLInputElement): HTMLElement =>
    (box.closest("label") ?? box.parentElement) as HTMLElement;

  const updateCount = (): void => {
    if (counter) {
      const n = boxes.filter((b) => b.checked).length;
      counter.textContent = `${n} valgt`;
    }
  };

  // The radio lives outside the picker, so this listens on the enclosing form rather than on the
  // field. `.form` is not on HTMLDetailsElement — only form CONTROLS carry it — so this walks up.
  const form = picker.closest("form");

  form?.addEventListener("change", (event: Event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.name === "visibility") picker.open = target.value === "kun_inviterede";
    if (target.type === "checkbox") updateCount();
  });

  search?.addEventListener("input", () => {
    const needle = search.value.trim().toLowerCase();
    for (const box of boxes) {
      const row = rowOf(box);
      const matches = !needle || (row.textContent ?? "").toLowerCase().includes(needle);
      row.hidden = !matches && !box.checked;
    }
  });

  updateCount();
}
