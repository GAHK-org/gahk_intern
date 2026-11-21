// Copy buttons: <button data-copy="<id of a field>"> copies that field's value. No-op without one.
// A [data-select-on-focus] field selects all of itself when focused, so a long address can be copied
// by hand where the clipboard API is unavailable (it needs a secure context).

for (const field of document.querySelectorAll<HTMLInputElement>("[data-select-on-focus]")) {
  field.addEventListener("focus", () => field.select());
}

for (const button of document.querySelectorAll<HTMLButtonElement>("[data-copy]")) {
  const label = button.textContent ?? "";
  let timer: number | undefined;

  button.addEventListener("click", async () => {
    const field = document.getElementById(button.dataset.copy ?? "") as HTMLInputElement | null;
    if (!field) return;
    let done = "Kopieret";
    try {
      await navigator.clipboard.writeText(field.value);
    } catch {
      field.focus();
      field.select();
      done = "Markeret – kopiér selv";
    }
    button.textContent = done;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      button.textContent = label;
    }, 2500);
  });
}
