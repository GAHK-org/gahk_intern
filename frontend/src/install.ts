// Installing the intranet as the home-screen app GAHK Intern: the /install page (install.html).
// No-op on every page without [data-install-page].

type Platform = "ios" | "android" | "desktop";

/** Chrome, Edge and Samsung Internet's install prompt. Not in TypeScript's DOM types. */
interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>;
  readonly userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

const UA = navigator.userAgent;
// iPadOS asks for the desktop site and reports itself as a Mac; the touch screen gives it away.
const IOS = /iPhone|iPad|iPod/.test(UA) || (/Macintosh/.test(UA) && navigator.maxTouchPoints > 1);
const ANDROID = /Android/.test(UA);

function platform(): Platform {
  if (IOS) return "ios";
  if (ANDROID) return "android";
  return "desktop";
}

/** Running as the installed app rather than in a browser tab. */
export function isInstalledApp(): boolean {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    window.matchMedia("(display-mode: fullscreen)").matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  );
}

function initPage(page: HTMLElement): void {
  if (isInstalledApp()) {
    page.querySelector<HTMLElement>("[data-install-installed]")!.hidden = false;
    page.querySelector<HTMLElement>("[data-install-steps]")!.hidden = true;
    return;
  }

  // This device's section first and open; the others stay below, closed, for anyone helping a
  // neighbour with a different phone.
  const mine = page.querySelector<HTMLDetailsElement>(`[data-install-platform="${platform()}"]`);
  if (mine) {
    mine.open = true;
    mine.parentElement!.prepend(mine);
  }

  // Where the browser offers its own install prompt, a button beats the menu steps. It fires only
  // while the app is not installed and the browser judges it installable, and may never fire at all.
  let deferred: BeforeInstallPromptEvent | null = null;
  const offers = page.querySelectorAll<HTMLElement>("[data-install-offer]");
  const done = page.querySelector<HTMLElement>("[data-install-done]")!;

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault(); // no mini-infobar from the browser: the button asks instead
    deferred = event as BeforeInstallPromptEvent;
    offers.forEach((offer) => (offer.hidden = false));
  });
  window.addEventListener("appinstalled", () => {
    deferred = null;
    offers.forEach((offer) => (offer.hidden = true));
    done.hidden = false;
  });
  page.querySelectorAll<HTMLButtonElement>("[data-install-button]").forEach((button) =>
    button.addEventListener("click", async () => {
      if (!deferred) return;
      const prompt = deferred;
      deferred = null; // a prompt can be shown once
      await prompt.prompt();
      // Spent either way. The browser may offer it again later, and then the button comes back.
      offers.forEach((offer) => (offer.hidden = true));
      if ((await prompt.userChoice).outcome === "accepted") done.hidden = false;
    }),
  );
}

const page = document.querySelector<HTMLElement>("[data-install-page]");
if (page) initPage(page);
