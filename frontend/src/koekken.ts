// Køkkenvagter: the kitchen tablet's mark-done button (app/templates/koekken/idag.html). No-op on
// every other page -- the root selector below (#idag-vagter) exists only there.
//
// The tablet is a touchscreen with no login and no per-user session state guarding a second tap, so
// the one thing worth guarding against here is a double-tap firing two POSTs for the same shift
// before htmx's swap has replaced the button. This disables every "Jeg har gjort det" button in the
// row's form the instant it is pressed -- purely cosmetic (the server is still the authority: a
// second POST that lands anyway is answered by `koekken.services.can_mark_done` refusing gracefully,
// see koekken.views.marker_udfoert), but it is what stops a finger that lands twice from ever
// sending the second request in the first place.
//
// htmx replaces #idag-vagter's INNER content after every mark-done POST (innerHTML, not outerHTML
// -- see koekken/idag.html and _idag_vagter.html's own comments on why), so the #idag-vagter node
// itself is stable across every swap and a freshly rendered button is never stuck disabled from a
// previous tap. This listens on that stable container (event delegation), not on each button, so
// it keeps working after every swap without ever re-binding.

const root = document.getElementById("idag-vagter");

if (root) {
  root.addEventListener("submit", (event: Event) => {
    const form = event.target as HTMLFormElement | null;
    const button = form?.querySelector<HTMLButtonElement>(".idag-done-btn");
    if (button) button.disabled = true;
  });
}
