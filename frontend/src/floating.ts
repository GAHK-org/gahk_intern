// A pop-up panel pinned under (or, without room, over) the field that opened it: the date picker's
// calendar and the time boxes' list of times.
//
// Fixed to the viewport and re-placed on every scroll and resize while open, rather than absolutely
// positioned inside the form: several of those forms sit in cards that clip their overflow, and the
// Hjemmeside editor's side column is sticky, so a panel inside them would be cut off or left behind.
// It is appended to the nearest <dialog> if there is one, so a modal's top layer does not hide it.

export function float(panel: HTMLElement, anchor: HTMLElement): () => void {
  (anchor.closest("dialog") ?? document.body).append(panel);
  panel.style.position = "fixed";

  const place = (): void => {
    const box = anchor.getBoundingClientRect();
    const gap = 6;
    const margin = 8;
    const { offsetWidth: width, offsetHeight: height } = panel;
    const below = window.innerHeight - box.bottom - gap - margin;
    const above = box.top - gap - margin;
    const top = height <= below || below >= above ? box.bottom + gap : box.top - gap - height;
    const left = Math.min(Math.max(margin, box.left), window.innerWidth - width - margin);
    panel.style.top = `${Math.max(margin, top)}px`;
    panel.style.left = `${Math.max(margin, left)}px`;
  };

  let frame = 0;
  const schedule = (): void => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(place);
  };
  place();
  window.addEventListener("scroll", schedule, { capture: true, passive: true });
  window.addEventListener("resize", schedule, { passive: true });

  return () => {
    cancelAnimationFrame(frame);
    window.removeEventListener("scroll", schedule, { capture: true });
    window.removeEventListener("resize", schedule);
    panel.remove();
  };
}

// A phone or tablet, where a text box summons the on-screen keyboard over the panel it opened.
export const coarsePointer = (): boolean => window.matchMedia("(pointer: coarse)").matches;
