// Desktop only: the menu scrolls along with the page until its last item is on screen, then stays; a
// scroll back up brings its first items back the same way, wherever on the page you are. A menu that
// fits on screen simply stays under the top bar. The CSS (.sidebar under min-width:961px) makes it
// sticky; this moves its sticking point (--sidebar-top), held between "top edge under the top bar"
// and "bottom edge at the bottom of the window".
const sidebar = document.querySelector<HTMLElement>(".sidebar");
const topbar = document.querySelector<HTMLElement>(".topbar");
const wide = window.matchMedia("(min-width:961px)");

if (sidebar && topbar) {
  let lastY = window.scrollY;
  // Where the menu's top edge was drawn after the last step, in window coordinates.
  let drawn = sidebar.getBoundingClientRect().top;
  let queued = false;

  // Phones (the drawer) and Den Hurtige (a page that never scrolls) don't use it.
  const active = () => wide.matches && getComputedStyle(sidebar).position === "sticky";

  // Put the top edge at `top`, within its limits. Near the top of the page the browser still draws
  // it lower, where the page puts it (below a banner, or not yet scrolled that far), so read back
  // where it really is rather than trusting the value set.
  const place = (top: number) => {
    const high = topbar.offsetHeight;
    const low = Math.min(high, document.documentElement.clientHeight - sidebar.offsetHeight);
    sidebar.style.setProperty("--sidebar-top", `${Math.min(high, Math.max(low, top))}px`);
    drawn = sidebar.getBoundingClientRect().top;
  };

  const update = () => {
    queued = false;
    const y = window.scrollY;
    const delta = y - lastY;
    lastY = y;
    // Move it as far as the page moved, from where it was: down the page, it goes up with it.
    if (active()) place(drawn - delta);
  };

  const queue = () => {
    if (!queued) {
      queued = true;
      requestAnimationFrame(update);
    }
  };

  window.addEventListener("scroll", queue, { passive: true });
  window.addEventListener("resize", queue);
  new ResizeObserver(queue).observe(sidebar);

  // Tabbing through the menu: bring the focused link on screen, clear of the top bar. The browser
  // scrolls the page for a link below the window, but not for one hidden under the top bar.
  sidebar.addEventListener("focusin", (e) => {
    if (!active()) return;
    const r = (e.target as HTMLElement).getBoundingClientRect();
    const view = document.documentElement.clientHeight;
    const high = topbar.offsetHeight;
    const top = sidebar.getBoundingClientRect().top;
    if (r.bottom > view) place(top + view - r.bottom - 12);
    else if (r.top < high) place(top + high - r.top + 12);
  });
}
