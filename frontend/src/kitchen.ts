// Køkkenvagter: ask before taking a shift. Any form with data-duty-confirm opens the page's shared
// <dialog data-duty-dialog>; delegated on document so rows htmx loads later are covered too.

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.dataset.dutyConfirm) return;
  const dialog = document.querySelector<HTMLDialogElement>("[data-duty-dialog]");
  if (!dialog || typeof dialog.showModal !== "function") return; // no dialog support: submit as-is

  event.preventDefault();
  const text = dialog.querySelector<HTMLElement>("[data-duty-dialog-text]");
  if (text) text.textContent = form.dataset.dutyConfirm;
  const spots = form.querySelector<HTMLInputElement>("input[name=spots]");
  const count = dialog.querySelector<HTMLElement>("[data-duty-dialog-spots]");
  if (count) {
    const n = Number(spots?.value ?? "1");
    count.textContent = n > 1 ? `Du tager ${n} pladser.` : "";
  }

  dialog.returnValue = "";
  dialog.showModal();
  dialog.addEventListener(
    "close",
    () => {
      if (dialog.returnValue === "confirm") form.submit();
    },
    { once: true },
  );
});
