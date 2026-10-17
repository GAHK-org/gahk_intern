// Web Push: this device's subscription. No-op on every page without a [data-push] element.
//
// A device subscribes ONCE, for everything. What it is then told is the resident's notification
// settings (/intern/notifikationer/indstillinger), which are per person and live on the server.
// It used to be one button per feature, each recording a per-topic consent on the device, which
// made what a phone received depend on which pages its owner had happened to visit.
//
// Two modes, from data-mode:
//   * "full" (the settings page): always visible, reports every state, and can turn push off.
//   * "prompt" (dashboard, Den Hurtige): a call to action that is only shown while this device is
//     NOT subscribed, and disappears once it is. Nothing to say otherwise, so it says nothing.
//
// The initial state cannot come from getSubscription() alone: that says the BROWSER holds a
// subscription, not that the server still has it for this resident. An administrator may have
// cancelled it, the push service may have reported it gone, or someone else may have logged in on
// this browser since. So an existing subscription is checked with the server ("status") first.
//
// Written by hand rather than using django-webpush's {% webpush_button %}, which would not work
// here: its webpush.js registers a SECOND service worker under /webpush/ (so the push handler in
// templates/sw.js never runs), it never calls Notification.requestPermission() — which iOS Safari
// requires from a user gesture or subscribe() rejects — and its labels are hardcoded English.
//
// Failures are reported to the user by cause, not as one generic "something broke": every step here
// fails for a reason a resident or an admin can act on (permission blocked, key misconfigured,
// server rejected), and a dead-end message makes those indistinguishable.

const IOS = /iP(hone|ad|od)/.test(navigator.userAgent);

/** Brave ships an official detection hook; every other browser leaves `navigator.brave` undefined. */
interface BraveNavigator extends Navigator {
  brave?: { isBrave: () => Promise<boolean> };
}

async function isBrave(): Promise<boolean> {
  try {
    return (await (navigator as BraveNavigator).brave?.isBrave()) === true;
  } catch {
    return false;
  }
}

// An uncompressed P-256 point: 0x04 followed by the 32-byte X and Y coordinates.
const VAPID_KEY_BYTES = 65;
const UNCOMPRESSED_POINT_TAG = 0x04;

class ServerRejected extends Error {
  constructor(readonly status: number) {
    super(`server svarede ${status}`);
  }
}

class BadVapidKey extends Error {}

// A base64url VAPID key has to reach pushManager.subscribe as raw bytes. The buffer is allocated
// explicitly so the result is Uint8Array<ArrayBuffer>: BufferSource excludes SharedArrayBuffer, and
// Uint8Array.from() would widen it to ArrayBufferLike.
function urlB64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padded = (base64 + "=".repeat((4 - (base64.length % 4)) % 4))
    .replace(/-/g, "+")
    .replace(/_/g, "/");
  let raw: string;
  try {
    raw = atob(padded);
  } catch {
    // atob throws InvalidCharacterError on e.g. a PEM body pasted into VAPID_PUBLIC_KEY.
    throw new BadVapidKey("nøglen er ikke gyldig base64url");
  }
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  // Checked here rather than left to the browser: Chrome reports a wrong-shaped key as a generic
  // AbortError from subscribe(), which is indistinguishable from a push-service outage.
  if (bytes.length !== VAPID_KEY_BYTES || bytes[0] !== UNCOMPRESSED_POINT_TAG) {
    throw new BadVapidKey(`nøglen er ${bytes.length} bytes, forventede ${VAPID_KEY_BYTES}`);
  }
  return bytes;
}

function browserName(): string {
  const m = navigator.userAgent.match(/(firefox|edg|chrome|safari|trident|msie)/i);
  return m ? m[0].toLowerCase() : "other";
}

type StatusType = "subscribe" | "unsubscribe" | "status";

async function post(
  url: string,
  csrf: string,
  statusType: StatusType,
  subscription: PushSubscription,
): Promise<{ state?: "mine" | "other" | "none" }> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
    credentials: "same-origin",
    body: JSON.stringify({
      status_type: statusType,
      subscription: subscription.toJSON(),
      browser: browserName(),
      user_agent: navigator.userAgent,
    }),
  });
  if (!response.ok) throw new ServerRejected(response.status);
  // 201 (subscribe) and 202 (unsubscribe) have no body; status answers {"state": ...}.
  try {
    return await response.json();
  } catch {
    return {};
  }
}

async function subscribeTo(
  registration: ServiceWorkerRegistration,
  vapidKey: string,
): Promise<PushSubscription> {
  const options: PushSubscriptionOptionsInit = {
    userVisibleOnly: true,
    applicationServerKey: urlB64ToUint8Array(vapidKey),
  };
  try {
    return await registration.pushManager.subscribe(options);
  } catch (err) {
    // A subscription created under a DIFFERENT VAPID key blocks a new one with InvalidStateError,
    // and the stale one is undecryptable by the current server. This is the normal state of any
    // browser that subscribed before the key pair was set or changed, so recover instead of asking
    // the user to clear site data.
    if (err instanceof DOMException && err.name === "InvalidStateError") {
      const stale = await registration.pushManager.getSubscription();
      if (stale) await stale.unsubscribe();
      return await registration.pushManager.subscribe(options);
    }
    throw err;
  }
}

function explain(err: unknown, brave: boolean): string {
  if (err instanceof BadVapidKey) {
    return `Serverens notifikationsnøgle er forkert konfigureret (${err.message}). Kontakt en administrator.`;
  }
  if (err instanceof ServerRejected) {
    return err.status === 403
      ? "Din session er udløbet. Genindlæs siden og prøv igen."
      : `Serveren afviste abonnementet (${err.status}). Prøv igen senere.`;
  }
  if (err instanceof DOMException) {
    if (err.name === "NotAllowedError") return "Browseren blokerer notifikationer for denne side.";
    if (err.name === "AbortError") {
      // Chrome's wording is "Registration failed - push service error": the browser could not reach
      // its own push backend (FCM for Chrome/Edge/Brave). Two realistic causes, and the fix differs
      // completely, so they get separate messages.
      //
      // Brave ships with Google push messaging OFF by default, which makes this the *normal* first
      // experience for every Brave user — worth naming the exact setting rather than making them
      // guess. Verified against brave://gcm-internals: the GCM client initialises but never checks
      // in (empty Android Id, "Connection Client Created: false").
      return brave
        ? "Brave blokerer notifikationer som standard. Slå 'Use Google services for push messaging' " +
            "til under brave://settings/privacy og genstart Brave."
        : "Browseren kunne ikke tilmelde sig sin notifikationstjeneste. Det skyldes typisk " +
            "netværket — prøv et andet netværk, eller en anden browser.";
    }
    return `Browseren afviste abonnementet (${err.name}).`;
  }
  return `Noget gik galt: ${err instanceof Error ? err.message : String(err)}`;
}

async function init(root: HTMLElement): Promise<void> {
  const button = root.querySelector<HTMLButtonElement>("[data-push-button]");
  const message = root.querySelector<HTMLElement>("[data-push-message]");
  const csrf = root.querySelector<HTMLInputElement>('input[name="csrfmiddlewaretoken"]')?.value ?? "";
  const saveUrl = root.dataset.url ?? "";
  const vapidKey = root.dataset.vapidKey ?? "";
  const prompt = root.dataset.mode === "prompt";
  // Danish copy lives in the template, so all user-facing text stays where the rest of it is.
  const onLabel = root.dataset.onLabel ?? "Slå notifikationer til";
  const offLabel = root.dataset.offLabel ?? "Slå fra på denne enhed";
  const successText = root.dataset.successText ?? "Denne enhed får nu notifikationer.";
  const onText = root.dataset.onText ?? "Denne enhed får notifikationer.";
  const offText = root.dataset.offText ?? "Denne enhed får ikke notifikationer.";
  if (!button || !message) return;

  const say = (text: string): void => {
    message.textContent = text;
    message.hidden = false;
  };
  // A prompt that cannot help (no support, blocked, not configured) stays hidden: it sits on the
  // dashboard and in the chat header, where an unactionable sentence is clutter. The settings page
  // ("full") says why instead.
  const unavailable = (text: string): void => {
    if (prompt) {
      root.hidden = true;
      return;
    }
    root.hidden = false;
    say(text);
  };

  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    // On iOS this is the normal state in a browser tab: push only exists in the installed PWA. That
    // one IS actionable, so the prompt shows it too.
    if (IOS) {
      root.hidden = false;
      say("Tilføj siden til hjemmeskærmen (Del → Føj til hjemmeskærm) for at kunne få notifikationer.");
      return;
    }
    unavailable("Din browser understøtter ikke notifikationer.");
    return;
  }
  if (!vapidKey) {
    unavailable("Notifikationer er ikke sat op på serveren endnu.");
    return;
  }
  if (Notification.permission === "denied") {
    unavailable("Notifikationer er blokeret i browserens indstillinger for denne side.");
    return;
  }

  const brave = await isBrave();
  const registration = await navigator.serviceWorker.ready; // the root-scoped /sw.js from base.html
  let subscription = await registration.pushManager.getSubscription();
  let enabled = false;
  if (subscription) {
    try {
      enabled = (await post(saveUrl, csrf, "status", subscription)).state === "mine";
    } catch (err) {
      console.error("push status:", err);
    }
  }

  const render = (): void => {
    if (prompt && enabled) {
      root.hidden = true;
      return;
    }
    root.hidden = false;
    button.textContent = enabled ? offLabel : onLabel;
    button.classList.toggle("is-quiet", enabled);
    button.disabled = false;
    button.hidden = false;
  };
  if (!prompt) say(enabled ? onText : offText);
  render();

  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      if (enabled && subscription) {
        // Server first, then the browser: turning notifications off in the OS instead would leave
        // the endpoint alive, and the server would keep encrypting and sending to it forever.
        await post(saveUrl, csrf, "unsubscribe", subscription);
        await subscription.unsubscribe();
        subscription = null;
        enabled = false;
        say(offText);
      } else {
        // Must come before subscribe() and must be inside the click handler — Safari/iOS rejects
        // a subscribe() whose permission prompt is not tied to a user gesture.
        const permission = await Notification.requestPermission();
        if (permission !== "granted") {
          say("Du afviste notifikationer. Slå dem til i browserens indstillinger for at fortryde.");
          button.disabled = false;
          return;
        }
        // Reuse the browser's existing subscription when it has one ("other"/"none" above): the
        // server upserts on the endpoint, so subscribing moves it to this resident.
        const fresh = subscription ?? (await subscribeTo(registration, vapidKey));
        try {
          await post(saveUrl, csrf, "subscribe", fresh);
        } catch (err) {
          // Never leave the browser holding a subscription the server does not know about: the
          // button would read "on" while no notification could ever arrive.
          if (!subscription) await fresh.unsubscribe();
          throw err;
        }
        subscription = fresh;
        enabled = true;
        say(successText);
        if (prompt) {
          // Leave the confirmation on screen for a moment before the prompt goes away.
          button.hidden = true;
          window.setTimeout(render, 4000);
          return;
        }
      }
    } catch (err) {
      console.error("push:", err);
      say(explain(err, brave));
    }
    render();
  });
}

for (const root of Array.from(document.querySelectorAll<HTMLElement>("[data-push]"))) {
  init(root).catch((err) => console.error("push init:", err));
}
