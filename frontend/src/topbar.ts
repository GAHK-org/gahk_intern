// Phone only: slide the sticky top bar away while scrolling down, back as soon as you scroll up.
// The class goes on <html> so other sticky bars (the photo album's) can close the gap it leaves.
const bar = document.querySelector<HTMLElement>(".topbar");
const narrow = window.matchMedia("(max-width:960px)");
const html = document.documentElement;

// Ignore tiny movements so a resting finger or iOS rubber-banding doesn't make it flicker.
const THRESHOLD = 8;

if (bar && !document.body.classList.contains("chat-page")) {
  let lastY = window.scrollY;
  let queued = false;

  const update = () => {
    queued = false;
    const y = window.scrollY;
    const delta = y - lastY;
    if (Math.abs(delta) < THRESHOLD) return;
    // Keep it in view near the top, and while keyboard focus is inside it (Log ud, profile link).
    const hide = narrow.matches && delta > 0 && y > bar.offsetHeight && !bar.contains(document.activeElement);
    html.classList.toggle("topbar-hidden", hide);
    lastY = y;
  };

  window.addEventListener(
    "scroll",
    () => {
      if (!queued) {
        queued = true;
        requestAnimationFrame(update);
      }
    },
    { passive: true },
  );
  narrow.addEventListener("change", () => html.classList.remove("topbar-hidden"));
  bar.addEventListener("focusin", () => html.classList.remove("topbar-hidden"));
}
