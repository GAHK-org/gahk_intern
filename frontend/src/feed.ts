// Den Hurtige's chat behaviour. No-op on every page without #js-feed.
//
// The feed polls every 5 seconds and the response is MORPHED into the DOM rather than replacing it
// (hx-swap="morph:innerHTML" on #js-feed, idiomorph). That choice is what deleted most of this
// file. The poll used to run every 20s and replace the list wholesale, so it threw away the
// reader's scroll position, collapsed open reply threads and deleted half-written replies — and
// roughly forty lines here existed to defend against its own refresh: cancel the swap while
// someone was typing, snapshot scrollTop, record which threads were open and re-open them after.
// Morphing patches the existing nodes in place instead, so none of that is needed: what did not
// change is not touched. The response is still the whole list, which is why deletions, expiry,
// reaction counts and new replies all keep working for free.
//
// Note the scroll container is #js-feed itself, not the window and not `.main`: the chat shell is
// exactly one viewport tall and only the message list scrolls (see .chat-page in styles.css).
//
// The site-wide htmx X-CSRFToken hook used to live here; it is now ./htmx-csrf, imported from
// main.ts, because opslagstavlen's hx-posts depend on it too.
//
// There are TWO pollers on this page. #js-feed polls the message list; a thread panel, once open,
// polls itself (see _thread.html). They are separate because the panel must NOT live inside the
// morphed region -- and merging them into one request with an out-of-band swap would put every
// reply back into the 5s payload, which is exactly what moving replies into the panel removed.
//
// Blocks, in order: morph configuration, scroll-following, the iOS zoom lockdown, picker dismissal,
// the thread panel's close/focus/history handling, the who-reacted gestures, the touch swipes, and
// the composer. Every one of them is delegated from `document` rather than bound to an element,
// because the feed is morphed every five seconds and anything bound directly would have to be
// re-armed after each poll.

import { Idiomorph } from "idiomorph/htmx";

const FEED_ID = "js-feed";
const THREAD_ID = "js-thread";
// Treat "within this many px of the bottom" as following the conversation.
const STICK_THRESHOLD = 120;
const TEXTAREA_MAX_ROWS = 5;

interface BeforeSwapDetail {
  target?: HTMLElement;
}

const feed = document.getElementById(FEED_ID);
// The list scrolls itself, so these are the same element — kept as two names because they mean
// different things: one is the region being swapped, the other the thing whose scrollTop we keep.
const scroller = feed;

function atBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD;
}

function toBottom(el: HTMLElement): void {
  el.scrollTop = el.scrollHeight;
}

// Attributes the CLIENT owns, which the server never sends and morphing would therefore strip:
//   open   a picker or reader panel the reader opened; losing it closes it under their thumb
const CLIENT_OWNED_ATTRS = new Set(["open"]);

Idiomorph.defaults.ignoreActiveValue = true; // never rewrite the field being typed into
Idiomorph.defaults.callbacks.beforeAttributeUpdated = (name: string, node: Element): boolean => {
  if (CLIENT_OWNED_ATTRS.has(name)) return false;
  // `style` is normally the server's to set — the avatar <img> carries one — but for the length of
  // a swipe the inline translate on the dragged message is ours, and a poll landing mid-gesture
  // would strip it out from under the thumb. Scoped to the element being dragged and its subtree
  // (the hint icons' opacity is set the same way) rather than blanket-listed above, so every other
  // message on the page keeps taking style updates normally.
  if (name === "style" && drag?.el.contains(node)) return false;
  return true;
};

// Subtrees the client owns OUTRIGHT: morphing must not enter them at all.
//
// Marked with data-morph-skip in _thread.html rather than listed by selector here, so the template
// that owns the form is the thing that says so.
Idiomorph.defaults.callbacks.beforeNodeMorphed = (node: Node): boolean =>
  !(node instanceof Element) || !node.hasAttribute("data-morph-skip");

if (feed && scroller) {
  // Start at the newest message, as a chat does.
  toBottom(scroller);
  window.addEventListener("load", () => toBottom(scroller));

  let wasAtBottom = true;

  document.body.addEventListener("htmx:beforeSwap", (event: Event) => {
    const detail = (event as CustomEvent<BeforeSwapDetail>).detail;
    if (detail?.target?.id !== FEED_ID) return; // a reaction swap, or some other region
    wasAtBottom = atBottom(scroller);
  });

  document.body.addEventListener("htmx:afterSwap", (event: Event) => {
    const target = (event as CustomEvent<{ target?: HTMLElement }>).detail?.target;
    if (target?.id !== FEED_ID) return;

    // Nothing to re-arm for image uploads any more: imageupload.ts listens on the document in the
    // capture phase, so it already covers forms that did not exist when it was registered.
    //
    // Follow the conversation only if they were already at the bottom. No scrollTop to restore
    // otherwise: morphing leaves the surrounding nodes alone, so the position does not move.
    if (wasAtBottom) toBottom(scroller);
  });
}

// ---- zoom lockdown (iOS) ---------------------------------------------------------------------
// Safari has ignored `user-scalable=no` since iOS 10, on purpose, so the viewport meta in feed.html
// only covers Android and desktop. Pinch-zoom on iOS is a Safari-specific gesture event, and
// preventing it is the one thing that actually stops it. Double-tap zoom is handled in CSS by
// `.no-zoom { touch-action: manipulation }`, and focus-zoom by keeping inputs at 16px.
//
// Scoped to this page: the rest of intern keeps pinch-to-zoom, which people need on the alumneliste
// and long CMS pages. Disabling it site-wide would be a real accessibility regression.
if (document.body.classList.contains("no-zoom")) {
  for (const type of ["gesturestart", "gesturechange", "gestureend"]) {
    document.addEventListener(type, (event: Event) => event.preventDefault(), { passive: false });
  }
}

// ---- emoji picker ---------------------------------------------------------------------------
// <details> has no concept of "click away to dismiss", so a panel would otherwise stay open behind
// whatever you did next, and two could be open at once.
//
// The test is "outside the PANEL", not "outside the <details>". The reaction overlays put a
// full-screen backdrop *inside* their own <details> (it has to be a real element — a click on a
// ::before pseudo-element reports the originating element as its target), so a `details.contains`
// check would treat a tap on the backdrop as a tap inside the picker and never close it. Excluding
// the summary keeps the browser's own toggle working: without it, the tap that opens a panel would
// be seen as an outside click on the panel and shut it again immediately.
const DISMISSABLE = "details.pop[open], details.channel-picker[open]";
const PANELS = ":scope > .pop-panel, :scope > .channel-menu";

document.addEventListener("click", (event) => {
  const target = event.target as Node;
  for (const picker of document.querySelectorAll<HTMLDetailsElement>(DISMISSABLE)) {
    const summary = picker.querySelector(":scope > summary");
    if (summary?.contains(target)) continue;
    const panel = picker.querySelector(PANELS);
    if (panel?.contains(target)) continue;
    picker.open = false;
  }
});

// Escape closes the topmost open panel, which is what a modal-looking overlay is expected to do and
// the only way out for anyone not using a pointer.
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const open = document.querySelectorAll<HTMLDetailsElement>(DISMISSABLE);
  const last = open[open.length - 1];
  if (last) {
    last.open = false;
    last.querySelector<HTMLElement>(":scope > summary")?.focus();
    return;
  }
  closeThread();
});

// ---- thread panel -----------------------------------------------------------------------------
// The panel itself is server-rendered and htmx-driven: the "N svar" anchor in _message.html does
// hx-get into #js-thread, and the fragment that lands there brings its own poll. What is left for
// this file is the four things htmx has no opinion about: closing it, focus, Escape, and making the
// system back gesture close it instead of leaving the app.

const threadHost = document.getElementById(THREAD_ID);

// Where focus came from, so it can be handed back. Not derived from the panel's data-thread-pk at
// close time: that message may have expired out of the feed while the thread was open.
let threadOpener: HTMLElement | null = null;

function threadPanel(): HTMLElement | null {
  return threadHost?.querySelector(".thread-panel") ?? null;
}

function clearThread(): void {
  if (threadHost) threadHost.innerHTML = "";
  dropThreadParam(); // so a reload does not re-open a thread the reader closed
  const opener = threadOpener;
  threadOpener = null;
  // Back to the link that opened it, if it is still on screen; otherwise the feed, so focus never
  // ends up on <body> with nothing to arrow away from.
  if (opener?.isConnected) opener.focus();
  else feed?.focus();
}

// Whether THIS document pushed a history entry when the thread was opened. False when the page was
// loaded straight at ?traad=<pk> (a notification deep link): there is no earlier entry of ours to
// go back to, and calling history.back() would leave the page — in the installed PWA, the app.
let pushedThreadEntry = false;

function dropThreadParam(): void {
  const url = new URL(window.location.href);
  if (!url.searchParams.has("traad")) return;
  url.searchParams.delete("traad");
  history.replaceState(null, "", url);
}

function closeThread(): void {
  if (!threadPanel()) return;
  if (pushedThreadEntry) {
    // Unwind our own entry so the back stack does not fill with threads already dismissed. The
    // popstate handler below does the actual clearing.
    history.back();
    return;
  }
  // Deep-linked open: nothing of ours to go back to. Clear in place, and take ?traad with it, or a
  // reload would re-open the thread the reader just closed.
  dropThreadParam();
  clearThread();
}

document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  if (target.closest("[data-thread-close]")) {
    event.preventDefault();
    closeThread();
  } else {
    // Remember the opener BEFORE htmx swaps, while the click target still exists.
    const opener = target.closest<HTMLElement>(".msg-replies");
    if (opener) threadOpener = opener;
  }
});

document.body.addEventListener("htmx:afterSwap", (event: Event) => {
  const target = (event as CustomEvent<{ target?: HTMLElement }>).detail?.target;
  if (target?.id !== THREAD_ID) return;
  const panel = threadPanel();
  if (!panel) return;

  // Focus the PANEL, not the reply field. On a phone this is a full-screen view, and focusing the
  // input pops the keyboard over the replies the reader just came to read.
  panel.focus();

  // One history entry per thread, so Android's back gesture closes the panel rather than leaving
  // the installed PWA (the manifest is display:standalone, so there is no browser back button and
  // no other way out of a full-screen view).
  //
  // Hand-rolled rather than hx-push-url: htmx would also snapshot this page into localStorage for
  // its history cache and, on back, restore a DOM that has been polling for minutes. Turning that
  // off needs hx-history="false" on <body>, which makes every back press a full navigation.
  // DO NOT add hx-push-url to the "N svar" link.
  const pk = panel.getAttribute("data-thread-pk");
  if (!pk || history.state?.denHurtigeThread === pk) return;

  const url = new URL(window.location.href);
  const deepLinked = url.searchParams.get("traad") === pk && !threadOpener;
  url.searchParams.set("traad", pk);
  if (deepLinked) {
    // Arrived here with the thread already in the URL, so this is the entry the reader came from,
    // not one we created. Stamp our state onto it rather than stacking a duplicate.
    history.replaceState({ denHurtigeThread: pk }, "", url);
  } else {
    history.pushState({ denHurtigeThread: pk }, "", url);
    pushedThreadEntry = true;
  }
});

window.addEventListener("popstate", (event) => {
  const state = event.state as { denHurtigeThread?: string } | null;
  if (!state?.denHurtigeThread) {
    pushedThreadEntry = false;
    clearThread();
  }
});

// ---- who reacted ------------------------------------------------------------------------------
// Holding a reaction pill (touch), hovering it (mouse), right-clicking it or pressing Shift+Enter
// answers "who used THAT emoji". It replaced a 👥 pill that listed everyone: see the comment at the
// top of _reactions.html for why per-emoji, and why the pill went.
//
// Two presentations, because one would be wrong for one of the inputs:
//
//   touch / right-click / keyboard -> the <details class="pop"> sheet already rendered in the row.
//   mouse hover                    -> a lightweight tooltip appended to <body>.
//
// Hover must NOT open the .pop. A .pop draws a full-screen `.pop-backdrop`, so the moment it opened
// the backdrop would slide under the cursor, fire pointerout on the pill, close the panel, and
// re-open on the next pointerover — a flicker loop. Hover also has no business summoning a centred
// modal sheet. The tooltip reads its names straight out of that same panel's DOM, so there is still
// exactly one rendering of who reacted and it cannot drift.
//
// The tooltip is a direct child of <body> and position:fixed, so nothing can clip it and no
// ancestor can trap it — the same reasoning as the .pop panels themselves (see styles.css).
//
// All of it is delegated from `document`, and keyed on `.reaction[data-who]` rather than on
// anything Den Hurtige owns. Two reasons, both load-bearing: the feed's pills are morphed every few
// seconds, so anything bound to a pill directly would have to be re-armed after every poll — and
// opslagstavlen renders the same markup on a page this module knows nothing about, which is why
// giving the noticeboard these gestures needed no code here beyond the window-scroll line below.
const WHO_HOLD_MS = 450; // long-press
const WHO_HOVER_MS = 400;
const WHO_MOVE_SLOP = 10; // px of finger drift that still counts as a hold, not a scroll
const canHover = window.matchMedia("(hover: hover)").matches;

let holdTimer: number | undefined;
let hoverTimer: number | undefined;
let holdOrigin: { x: number; y: number } | null = null;
// A long-press ends in a click, and that click would otherwise toggle the reaction AND be read as
// an outside click by the dismissal handler above — opening the panel and closing it in one go.
let swallowClick = false;
let tip: HTMLDivElement | null = null;

function pillPanel(pill: Element): HTMLDetailsElement | null {
  const id = pill.getAttribute("data-who");
  return id ? (document.getElementById(id) as HTMLDetailsElement | null) : null;
}

function whoNames(pill: Element): string[] {
  const panel = pillPanel(pill);
  if (!panel) return [];
  return [...panel.querySelectorAll(".who-names")]
    .map((n) => n.textContent?.trim() ?? "")
    .filter(Boolean);
}

function openWhoPanel(pill: Element): void {
  const panel = pillPanel(pill);
  if (!panel || !whoNames(pill).length) return;
  // Only one overlay at a time, matching what the click-away handler enforces for the rest.
  for (const other of document.querySelectorAll<HTMLDetailsElement>("details.pop[open]")) {
    if (other !== panel) other.open = false;
  }
  panel.open = true;
}

function hideTip(): void {
  if (tip) tip.hidden = true;
}

function showTip(pill: HTMLElement): void {
  const names = whoNames(pill);
  if (!names.length) return;
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "who-tip";
    tip.setAttribute("role", "tooltip");
    document.body.appendChild(tip);
  }
  tip.textContent = names.join(", ");
  tip.hidden = false;

  // Measured after it is visible and filled, because both change its size. Prefers above the pill
  // and flips below when there is no room; clamped horizontally so a pill near either edge still
  // shows the whole list.
  const pillBox = pill.getBoundingClientRect();
  const tipBox = tip.getBoundingClientRect();
  const gap = 6;
  const above = pillBox.top - tipBox.height - gap;
  tip.style.top = `${above >= 4 ? above : pillBox.bottom + gap}px`;
  const wanted = pillBox.left + pillBox.width / 2 - tipBox.width / 2;
  tip.style.left = `${Math.max(6, Math.min(wanted, window.innerWidth - tipBox.width - 6))}px`;
}

function pillFrom(target: EventTarget | null): HTMLElement | null {
  return target instanceof Element ? target.closest<HTMLElement>(".reaction[data-who]") : null;
}

if (canHover) {
  document.addEventListener("pointerover", (event: PointerEvent) => {
    if (event.pointerType === "touch") return;
    const pill = pillFrom(event.target);
    if (!pill) return;
    window.clearTimeout(hoverTimer);
    hoverTimer = window.setTimeout(() => showTip(pill), WHO_HOVER_MS);
  });

  document.addEventListener("pointerout", (event: PointerEvent) => {
    if (!pillFrom(event.target)) return;
    window.clearTimeout(hoverTimer);
    hideTip();
  });
}

// The tooltip is positioned against the viewport, so anything that moves the pill must retire it
// rather than leave it floating somewhere the pill no longer is. Both scrollers are covered because
// the two callers scroll differently: Den Hurtige's chat shell scrolls #js-feed itself (see the
// note at the top of this file), while opslagstavlen is an ordinary page that scrolls the window.
// Listening only to the first left a tooltip hanging mid-screen as the noticeboard scrolled under it.
feed?.addEventListener("scroll", hideTip, { passive: true });
window.addEventListener("scroll", hideTip, { passive: true });
window.addEventListener("resize", hideTip);

document.addEventListener("pointerdown", (event: PointerEvent) => {
  const pill = pillFrom(event.target);
  if (!pill || event.pointerType === "mouse") return; // mouse gets hover and right-click instead
  holdOrigin = { x: event.clientX, y: event.clientY };
  window.clearTimeout(holdTimer);
  holdTimer = window.setTimeout(() => {
    swallowClick = true;
    openWhoPanel(pill);
  }, WHO_HOLD_MS);
});

function cancelHold(): void {
  window.clearTimeout(holdTimer);
  holdOrigin = null;
}

document.addEventListener("pointerup", cancelHold);
document.addEventListener("pointercancel", cancelHold);
document.addEventListener("pointermove", (event: PointerEvent) => {
  // A finger that has travelled is scrolling the feed, not holding a pill.
  if (!holdOrigin) return;
  if (
    Math.abs(event.clientX - holdOrigin.x) > WHO_MOVE_SLOP ||
    Math.abs(event.clientY - holdOrigin.y) > WHO_MOVE_SLOP
  ) {
    cancelHold();
  }
});

// Capture phase, so it runs before both htmx's handler on the pill and the click-away handler above.
document.addEventListener(
  "click",
  (event: MouseEvent) => {
    if (!swallowClick) return;
    swallowClick = false;
    event.preventDefault();
    event.stopPropagation();
  },
  true,
);

document.addEventListener("contextmenu", (event: MouseEvent) => {
  const pill = pillFrom(event.target);
  if (!pill || !whoNames(pill).length) return;
  event.preventDefault();
  hideTip();
  openWhoPanel(pill);
});

// Enter and Space must keep toggling the reaction — that is the pill's primary job and the only
// keyboard way to react. Shift+Enter is the second gesture, mirroring hold and right-click.
document.addEventListener("keydown", (event: KeyboardEvent) => {
  if (event.key !== "Enter" || !event.shiftKey) return;
  const pill = pillFrom(event.target);
  if (!pill) return;
  event.preventDefault();
  openWhoPanel(pill);
});

// ---- Android system navigation bar (installed PWA) --------------------------------------------
// Residents on Android reported the composer clipped along its bottom edge -- always, not only with
// the keyboard up -- and only from the home-screen app. iPhones were fine.
//
// The cause is that the shell is exactly 100dvh with html and body overflow:hidden (see .chat-page
// in styles.css), and in an installed standalone window Android draws the page EDGE TO EDGE: the
// viewport runs underneath the system navigation bar, so 100dvh includes a strip that the gesture
// pill or the three buttons sit on top of. The composer is the last flex child of that shell, so it
// is the thing under the bar.
//
// `viewport-fit=cover` plus env(safe-area-inset-bottom) is the supported answer to this, and it is
// already in place -- it is what clears the home indicator on iOS. The problem is that some Android
// configurations report the inset as 0 while still painting the bar, which is the same thing that
// drove the duration picker above the input once already (see the .composer rules in styles.css).
// A 10px floor was the guard, and a navigation bar is 24dp for the gesture pill or 48dp for three
// buttons, so the floor was never going to be enough.
//
// WHY THIS IS MEASURED RATHER THAN JUST WIDENED. Raising the floor for every phone would push the
// composer up by that much on every device whose inset ALREADY works -- including iPhones, where
// env() correctly reports the home indicator and the layout is right today. So the floor is raised
// only where all three of these hold, which is exactly the broken configuration and nothing else:
//
//   * the window is a standalone PWA          -- a browser tab is inset by Chrome's own UI
//   * the platform is Android                 -- iOS reports its insets correctly; a 0 there is an
//                                                honest "this device has no home indicator"
//   * env(safe-area-inset-bottom) reads 0     -- if the platform gives a number, it is the truth
//                                                and is used as-is, gesture pill or buttons alike
//
// Read once at startup, which is enough: the manifest pins orientation to portrait-primary, so the
// inset cannot change underneath us.
const ANDROID_NAV_BAR_FALLBACK = 48; // dp of a three-button bar; the pill is 24 and fits inside it

// env() is only readable through a real element, so borrow one for a frame. Sized rather than
// positioned so the fallback in env(..., 0px) covers browsers that do not know the variable at all.
function safeAreaBottom(): number {
  const probe = document.createElement("div");
  probe.style.cssText =
    "position:fixed;bottom:0;left:-9999px;width:0;height:env(safe-area-inset-bottom, 0px);";
  document.body.appendChild(probe);
  const inset = probe.getBoundingClientRect().height;
  probe.remove();
  return inset;
}

if (document.body.classList.contains("chat-page")) {
  const standalone = window.matchMedia("(display-mode: standalone)").matches;
  // A deliberate platform check, not feature detection dressed up as one: this is a workaround for
  // one platform's reporting, so keying it to that platform is the honest way to scope it.
  const android = /Android/.test(navigator.userAgent);
  if (standalone && android && safeAreaBottom() === 0) {
    document.documentElement.style.setProperty(
      "--chat-system-bar",
      `${ANDROID_NAV_BAR_FALLBACK}px`,
    );
  }
}

// ---- swipe gestures (touch only) --------------------------------------------------------------
// Three gestures, all of them shortcuts to controls that already exist rather than new powers:
//
//   message, swipe right      -> open its thread (clicks the "N svar" link)
//   own message, swipe left   -> delete it (submits the .msg-del form, confirm() and all)
//   thread panel, swipe right -> back to the feed (the ← button's closeThread)
//
// Firing the existing control rather than issuing a request is the whole trick. The thread gesture
// goes through the anchor, so the htmx swap, the pushed history entry and the Android back handling
// are reached by exactly one path and cannot drift from the tap; the delete gesture goes through
// the form, so the confirm() still stands between a stray drag and a permanently deleted message.
//
// TOUCH ONLY, deliberately. A mouse drag across a message is how you select its text, and a
// trackpad's horizontal scroll would be indistinguishable from a swipe. Desktop keeps the buttons.
//
// THE TRANSFORM IS TEMPORARY, AND THAT IS LOAD-BEARING. `translate` makes a stacking context, and
// neither .msg nor .thread-panel may hold one at rest: both are ancestors of the `.pop` pickers,
// which break the moment they are trapped in an ancestor's layer (see the .pop block in
// styles.css, and the note on @keyframes thread-in for the same rule applied to the panel's
// entrance animation). Two things enforce it. A drag never STARTS while a picker is open, and
// settle() strips the inline translate once the spring-back has finished, so nothing is left
// behind. The same reasoning as "a finished animation leaves nothing behind", one gesture later.
//
// Axis handling is CSS's job, not this file's: `touch-action:pan-y` on both elements leaves the
// vertical axis to the browser (so the feed still scrolls with its native momentum) and hands us
// the horizontal one. A flick that the browser claims as a scroll arrives here as pointercancel.
const SWIPE_SLOP = 12; // px of HORIZONTAL travel before the axis can be called
// px of travel PAST the axis call that commits the gesture. Two of them, because the two gestures
// are not equally reversible: opening a thread is a view change the back arrow undoes, deleting is
// permanent. The destructive one is the longer haul; the one people reach for twenty times an
// evening is the shorter.
const SWIPE_TRIGGER = 48;
const SWIPE_TRIGGER_DELETE = 62;
const SWIPE_MAX = 96; // px the element will actually move, however far the thumb goes
// A FLICK COMMITS WITHOUT EVER REACHING THE TRIGGER: px/ms of horizontal speed at release, and the
// least travel that speed may commit from. Distance alone made every open a deliberate HAUL at
// whatever pace the thumb happened to be moving, which is most of what "hard to control" was -- a
// quick confident flick across a message did nothing whatever, because the finger left the screen
// at 40px. Speed is read from the last few frames only (SWIPE_FLICK_STALE), so a drag that comes
// to rest under the thumb and then lifts is a drag, not a flick.
const SWIPE_FLICK_SPEED = 0.4;
const SWIPE_FLICK_MIN = 24;
const SWIPE_FLICK_STALE = 90; // ms since the last move past which the recorded speed means nothing
// px the MESSAGE must travel before the icon under it starts fading in: the point at which the
// icon is no longer half-covered by whatever is sliding off it. The hints paint BEHIND the bubble
// (see the .msg-hint block in styles.css) and their band runs 6px..38px from the edge, so on an
// incoming message it is the AVATAR (10px..44px) that has to clear x=38, and on your own it is the
// bubble, whose edge sits at the 10px padding. Both come out at 28.
const SWIPE_HINT_FROM = 28;
// Vertical still wins a tie, and then some: this is a scrolling list first and a gesture surface
// second. But losing the tie no longer ENDS the gesture -- see the axis block in pointermove.
const SWIPE_X_BIAS = 1.3;

type SwipeAction = "thread" | "delete" | "close";

interface Drag {
  pointerId: number;
  el: HTMLElement;
  startX: number;
  startY: number;
  // Where the ELEMENT's travel is measured from, which is NOT where the finger landed: it is the
  // finger's position at the moment the axis was called. Measured from startX instead, the message
  // JUMPED the whole slop the instant it was granted -- 12px at best, and further still when the
  // bias took a few frames to be satisfied, so a careful swipe began by leaping out from under the
  // thumb. From here the bubble tracks the finger 1:1 and starts from where it was let go.
  anchorX: number;
  lastX: number;
  lastT: number;
  speed: number; // px/ms, signed, smoothed across moves -- see SWIPE_FLICK_SPEED
  along: number; // px the element has travelled in the committed direction, as of the last move
  decided: boolean;
  action: SwipeAction | null;
  armed: boolean; // past the trigger, so the buzz fires once rather than every pointermove
}

let drag: Drag | null = null;

// A picker or channel menu is open, so its backdrop owns the screen and a transform on an ancestor
// would trap the panel. Same selector the dismissal handler uses, for the same reason.
function overlayOpen(): boolean {
  return document.querySelector(DISMISSABLE) !== null;
}

function actionFor(el: HTMLElement, dx: number): SwipeAction | null {
  if (el.classList.contains("thread-panel")) return dx > 0 ? "close" : null;
  if (dx > 0) return el.querySelector(".msg-replies") ? "thread" : null;
  // Left is delete, and only where the delete control exists — which is the server's answer to
  // "may this resident delete this message", not one this file should try to reproduce.
  return el.querySelector(".msg-del") ? "delete" : null;
}

function dirOf(action: SwipeAction): number {
  return action === "delete" ? -1 : 1;
}

function triggerFor(action: SwipeAction): number {
  return action === "delete" ? SWIPE_TRIGGER_DELETE : SWIPE_TRIGGER;
}

// Distance OR speed, never both at once. Past the trigger it is committed however slowly the thumb
// got there; short of it, a flick still counts -- a thread opened by mistake costs one tap on the
// back arrow, and a delete still has to get past its confirm(), so neither is a decision this is
// forbidden to make quickly.
function shouldCommit(active: Drag, now: number): boolean {
  if (!active.action) return false;
  if (active.armed) return true;
  if (now - active.lastT > SWIPE_FLICK_STALE) return false; // the finger had already come to rest
  return active.along >= SWIPE_FLICK_MIN && active.speed * dirOf(active.action) >= SWIPE_FLICK_SPEED;
}

function hintFor(el: HTMLElement, action: SwipeAction): HTMLElement | null {
  const cls = action === "thread" ? ".msg-hint-thread" : ".msg-hint-del";
  return action === "close" ? null : el.querySelector<HTMLElement>(cls);
}

function clearHints(el: HTMLElement): void {
  for (const hint of el.querySelectorAll<HTMLElement>(".msg-hint")) {
    hint.style.opacity = "";
    hint.style.scale = "";
    hint.style.translate = "";
  }
}

// Spring back to rest, then remove every trace. The class carries the transition only while it is
// needed: left on, it would also animate the next drag's first pointermove.
function settle(el: HTMLElement, cls: string): void {
  el.classList.add(cls);
  el.style.translate = "";
  const done = (): void => {
    el.classList.remove(cls);
    el.removeEventListener("transitionend", done);
  };
  el.addEventListener("transitionend", done);
  // transitionend does not fire when there was nothing to animate — a drag that never moved, or
  // prefers-reduced-motion, which turns the transition off entirely. Without this fallback the
  // class would stay on the element for good.
  window.setTimeout(done, 260);
}

function fireSwipe(el: HTMLElement, action: SwipeAction): void {
  if (action === "close") {
    closeThread();
  } else if (action === "thread") {
    // Clicking the anchor rather than calling htmx directly: the click handler above records it as
    // threadOpener on the way past, which is what focus returns to when the thread is closed.
    el.querySelector<HTMLElement>(".msg-replies")?.click();
  } else {
    el.querySelector<HTMLFormElement>(".msg-del")?.requestSubmit();
  }
}

// A gesture ends in a compatibility click on whatever the finger left the screen over, and inside
// a bubble that can be the photo -- so a swipe-to-open-thread would open the thread AND the picture
// full size. Swallowed for one click, and only the BROWSER's: fireSwipe dispatches its own on the
// "N svar" anchor, and a synthetic click is not trusted, so the gesture's own effect goes through.
let swallowIn: HTMLElement | null = null;
let swallowUntil = 0;

document.addEventListener(
  "click",
  (event) => {
    if (!swallowIn || !event.isTrusted || performance.now() > swallowUntil) return;
    const target = event.target;
    if (!(target instanceof Node) || !swallowIn.contains(target)) return;
    swallowIn = null;
    event.preventDefault();
    event.stopPropagation();
  },
  true,
);

function endDrag(commit: boolean): void {
  const active = drag;
  drag = null;
  if (!active) return;
  const { el, action } = active;
  if (active.decided) {
    swallowIn = el;
    swallowUntil = performance.now() + 350; // a compatibility click lands well inside this
  }
  clearHints(el);
  settle(el, el.classList.contains("thread-panel") ? "thread-releasing" : "msg-releasing");
  // Act AFTER handing the element back its resting position, so the inline translate is already on
  // its way out when the thread swap or the confirm() dialog arrives.
  if (commit && action) fireSwipe(el, action);
}

if (feed) {
  // Tells the stylesheet the gestures are live, which is what lets the delete ✕ hide itself on
  // touch (see .msg-del in styles.css). Set from here rather than assumed in CSS so a phone that
  // never ran this bundle — blocked, cached broken, an old service worker — keeps the button it
  // needs to delete a message at all.
  document.body.classList.add("swipe-ready");

  document.addEventListener("pointerdown", (event: PointerEvent) => {
    if (event.pointerType !== "touch" || drag || overlayOpen()) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    // Anything that already answers a touch keeps it. `.reaction` is named on top of the element
    // list because a pill IS a button, and losing that gesture would take the who-reacted panel
    // with it — the hold timer above starts on the same pointerdown.
    //
    // THE PHOTO IS THE EXCEPTION, and it is an <a> only so that a TAP can open it full size. It is
    // also the largest thing a bubble ever contains — on a message that is just an image it is the
    // whole bubble — so treating it like a control meant the swipe quietly did nothing on exactly
    // the messages whose thread you most want to read. The tap survives: the drag only swallows
    // the click that follows it once it has actually been decided (see endDrag).
    const control = target.closest("a, button, input, textarea, select, label, .reaction");
    if (control && !control.classList.contains("msg-image")) return;
    const el = target.closest<HTMLElement>("[data-msg-swipe], .thread-panel");
    if (!el) return;
    drag = {
      pointerId: event.pointerId,
      el,
      startX: event.clientX,
      startY: event.clientY,
      anchorX: event.clientX,
      lastX: event.clientX,
      lastT: event.timeStamp,
      speed: 0,
      along: 0,
      decided: false,
      action: null,
      armed: false,
    };
  });

  document.addEventListener("pointermove", (event: PointerEvent) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;

    // Speed of the last few frames, for the flick test at release. Smoothed rather than taken
    // from the final frame alone: the last pointermove before a lift is often a stub of one or two
    // px over a millisecond, which on its own reads as either a standstill or a rocket.
    const dt = event.timeStamp - drag.lastT;
    if (dt > 0) {
      drag.speed = drag.speed * 0.4 + ((event.clientX - drag.lastX) / dt) * 0.6;
      drag.lastX = event.clientX;
      drag.lastT = event.timeStamp;
    }

    if (!drag.decided) {
      // NOTHING IN HERE ENDS A GESTURE ANY MORE. Every test below is "not yet", never "no", and
      // the two that used to answer "no" are why this still felt broken after the last round:
      // both of them threw the whole drag away on the strength of its first 12-30px, and once
      // `drag` was null the message would not budge again however far the thumb travelled. Only
      // lifting and starting over could recover it, which is exactly what "very hard to control"
      // feels like from the outside.
      //
      //   * A vertical abort at 30px killed any swipe whose opening arc dipped before it turned.
      //     It bought nothing: an undecided drag translates NOTHING, so a scroll costs a null
      //     check per move and no pixels, and the bias below is measured against TOTAL travel
      //     from touch-down, so a finger 200px down the list can never out-run it afterwards.
      //   * `actionFor` returning null killed it too, and that one was asymmetric. The thumb
      //     often pulls back a few px before it pushes off, and on SOMEONE ELSE'S message there
      //     is no left-hand action to find, so that flinch was a death sentence -- while on your
      //     own message the same flinch simply became the delete gesture. Right-swipe-to-open
      //     therefore failed on precisely the messages it is most needed on.
      //
      // `touch-action:pan-y` is what makes all of this safe: the browser fires pointercancel the
      // moment it claims the touch for the scroller, which is the authoritative answer to "was
      // that a scroll?" and the only one this file needs to listen to.
      // Not enough sideways travel to call the axis on yet.
      if (Math.abs(dx) < SWIPE_SLOP) return;
      // Vertical is still ahead, so keep watching. A thumb swipe is an ARC rather than a line, and
      // how much vertical is in its first 12-15px depends on where on the screen it starts and
      // which hand is holding the phone -- own messages and other people's sit against opposite
      // edges, so they never get the same start. Sentencing a 70px gesture on its first 12px was
      // the bug; deferring lets the same swipe qualify a few frames later. Waiting costs nothing,
      // because the bias is measured against TOTAL travel and a real scroll only widens the gap.
      if (Math.abs(dx) <= Math.abs(dy) * SWIPE_X_BIAS) return;
      const action = actionFor(drag.el, dx);
      if (!action) return; // nothing lives in that direction YET; the bubble stays put and waits
      drag.decided = true;
      drag.action = action;
      // From HERE, not from where the finger landed. See `anchorX` on Drag.
      drag.anchorX = event.clientX;
      // Keep receiving moves even if the finger leaves the element — a swipe that starts near the
      // bottom of a short bubble is otherwise lost the moment it drifts out of it.
      drag.el.setPointerCapture?.(event.pointerId);
    }

    // Narrowed once here rather than read off `drag` at each use. The field is nullable only for
    // the span between pointerdown and the axis being called, and by this line that span is over —
    // either it was decided on an earlier move or on this one, and both paths set it.
    const action = drag.action;
    if (!action) return;

    // Only movement in the committed direction counts. Reversing mid-drag winds the bubble back to
    // rest rather than re-deciding, which would let one gesture turn into the other under the thumb.
    const dir = dirOf(action);
    const trigger = triggerFor(action);
    const along = Math.max(0, (event.clientX - drag.anchorX) * dir);
    drag.along = along;
    // Past the trigger the bubble keeps moving, but at a quarter speed. That resistance is the
    // feedback: it says the gesture has caught without needing the element to stop dead.
    const eased = along <= trigger ? along : trigger + (along - trigger) * 0.25;
    const offset = Math.min(eased, SWIPE_MAX) * dir;
    drag.el.style.translate = `${offset}px 0`;

    const hint = hintFor(drag.el, action);
    if (hint) {
      // THE HINTS MUST BE HELD STILL WHILE THE MESSAGE SLIDES OFF THEM, and that is what this
      // line does. They are children of the element being translated, so without it they ride
      // along with the bubble and are never uncovered by it -- the drag moves the message and its
      // icons together, as one piece, and a swipe reveals nothing at all. Cancelling the parent's
      // transform on the way back down is what turns the gesture into what it looks like: the
      // message sliding aside to show what is underneath.
      //
      // It only LOOKED right on your own messages, which is why this survived. Those are
      // right-aligned, so the 20% gutter the bubble cannot reach leaves the thread icon standing
      // in empty space at `left:6px` -- it faded in on cue without ever needing to be revealed.
      // On someone else's message the same 6px is underneath the AVATAR (34px wide, at the 10px
      // padding edge), so a green icon was being drawn on top of a brass circle and dragged along
      // with it. And the trash can, at `right:6px`, is underneath the bubble on your own messages
      // in exactly the same way -- it was the sliver past the bubble's corner that showed.
      hint.style.translate = `${-offset}px 0`;
      // Held at 0 until the message has cleared the icon's own band, so no half-covered icon ever
      // fades in: the hints paint BEHIND the bubble (see the .msg-hint block in styles.css).
      const progress = Math.min(
        1,
        Math.max(0, (along - SWIPE_HINT_FROM) / Math.max(1, trigger - SWIPE_HINT_FROM)),
      );
      hint.style.opacity = String(progress);
      hint.style.scale = String(0.7 + progress * 0.3);
    }

    const past = along >= trigger;
    if (past && !drag.armed) {
      drag.armed = true;
      // Android only — iOS has no Vibration API and ignores it. Optional either way, so it is
      // called through a guard rather than assumed.
      navigator.vibrate?.(8);
    } else if (!past) {
      drag.armed = false;
    }
  });

  document.addEventListener("pointerup", (event: PointerEvent) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    endDrag(shouldCommit(drag, event.timeStamp));
  });

  // The browser took the gesture over as a scroll, or the system interrupted it.
  //
  // A cancel BEFORE the trigger is the scroll case and must not commit -- that is the contract
  // `touch-action:pan-y` buys. Past it, commit: nothing the browser does to the vertical axis
  // moves a finger 68px sideways first, so a cancel that arrives here is the platform taking the
  // touch away mid-gesture (its own edge-swipe, a notification shade, the app losing focus) after
  // the resident has already done everything the gesture asks of them. Dropping a finished swipe
  // on the floor is indistinguishable, from the outside, from the gesture not working -- you drag
  // the message clear across, it springs back, and nothing happens.
  //
  // DISTANCE ONLY HERE, never shouldCommit's flick: the commonest thing that cancels a fast
  // right-swipe is Android's own edge-back gesture, which IS a fast right-swipe. Honouring the
  // flick would open the thread while the system was already navigating back out of it.
  document.addEventListener("pointercancel", (event: PointerEvent) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    endDrag(drag.armed);
  });
}

// ---- composer --------------------------------------------------------------------------------
const composer = document.getElementById("js-composer");
if (composer instanceof HTMLFormElement) {
  const textarea = composer.querySelector("textarea");
  const fileInput = composer.querySelector<HTMLInputElement>('input[type="file"]');
  const fileNote = document.getElementById("js-composer-file");

  if (textarea) {
    const grow = (): void => {
      const max = parseFloat(getComputedStyle(textarea).lineHeight || "20") * TEXTAREA_MAX_ROWS;
      textarea.style.height = "auto";
      textarea.style.height = `${Math.min(textarea.scrollHeight, max)}px`;
    };
    textarea.addEventListener("input", grow);
    grow();

    // Plain Enter must stay a newline: on a phone it is the only way to write a second line.
    textarea.addEventListener("keydown", (event: KeyboardEvent) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        composer.requestSubmit();
      }
    });
  }

  // Confirm the attachment landed — a file input styled as a paperclip gives no other feedback.
  if (fileInput && fileNote) {
    fileInput.addEventListener("change", () => {
      const name = fileInput.files?.[0]?.name;
      fileNote.textContent = name ? `📎 ${name}` : "";
      fileNote.hidden = !name;
    });
  }
}

// ---- attachment notes -------------------------------------------------------------------------
// What is on screen after you pick a photo, for every form in the project that takes one behind a
// paperclip: Den Hurtige's reply box, and the comment forms on opslagstavlen and begivenheder.
//
// A hidden <input type=file> behind a label has NO native feedback at all. That was survivable
// while every one of these also required text — you could see you had typed something — but a
// comment may now be a PHOTO ON ITS OWN, and then the note is the only evidence on screen that
// there is anything to send. It shows three things, and each answers a question the paperclip
// leaves open:
//
//   a THUMBNAIL      is it the right picture? A filename does not answer that, and picking the
//                    wrong one out of a camera roll of "IMG_4821.jpg" is the common mistake.
//                    Made with createObjectURL, which reads nothing and decodes in the browser -
//                    no canvas, no library, no upload until the form is submitted.
//   the FILENAME     which file, and the size, so an obviously-huge one is visible before the
//                    server rejects it.
//   a REMOVE button  there was previously no way to un-attach a photo short of reloading the page
//                    and losing the typed text with it.
//
// OPTED INTO BY THE NOTE ELEMENT, not by a list of form classes. Any form containing
// [data-file-note] gets this; a form without one is untouched. That is what let all three features
// share it without this file having to know their class names, and it is why adding a fourth needs
// no change here.
//
// Delegated from `document` rather than bound per input: the thread panel does not exist when this
// module runs and is replaced wholesale every time a different thread is opened. `change` does not
// bubble on some legacy engines but does in every browser this PWA supports, and the reply form
// carries data-morph-skip so the panel's own 5s poll cannot wipe a note back out.
const NOTE = "[data-file-note]";

/** Drop the preview and let the browser reclaim the decoded image. */
function clearNote(note: HTMLElement): void {
  const thumb = note.querySelector<HTMLImageElement>("[data-file-note-thumb]");
  if (thumb?.src) {
    // Revoked, not merely reassigned: an object URL pins its Blob in memory until it is released,
    // and a resident picking several photos in a row would otherwise leak every one of them.
    URL.revokeObjectURL(thumb.src);
    thumb.removeAttribute("src");
    thumb.hidden = true;
  }
  const name = note.querySelector<HTMLElement>("[data-file-note-name]");
  if (name) name.textContent = "";
  note.hidden = true;
}

function fileSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

document.addEventListener("change", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || input.type !== "file") return;
  const note = input.closest("form")?.querySelector<HTMLElement>(NOTE);
  if (!note) return;

  clearNote(note);
  const file = input.files?.[0];
  if (!file) return;

  const name = note.querySelector<HTMLElement>("[data-file-note-name]");
  if (name) name.textContent = `${file.name} · ${fileSize(file.size)}`;

  // Only for something the browser will actually render. `accept="image/*"` steers the picker but
  // does not bind it, and a broken-image icon is worse feedback than none - the filename still
  // shows either way, and the server has the final say on what it will store (core.uploads).
  const thumb = note.querySelector<HTMLImageElement>("[data-file-note-thumb]");
  if (thumb && file.type.startsWith("image/")) {
    thumb.src = URL.createObjectURL(file);
    thumb.hidden = false;
  }
  note.hidden = false;
});

// Take it back off. Clearing `value` is what actually detaches the file - hiding the note alone
// would leave the form still carrying the photo, which is the worst of both.
document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  if (!target.closest("[data-file-note-clear]")) return;
  const form = target.closest("form");
  const note = form?.querySelector<HTMLElement>(NOTE);
  if (!form || !note) return;
  for (const input of form.querySelectorAll<HTMLInputElement>('input[type="file"]')) {
    input.value = "";
  }
  clearNote(note);
});

// The reply form resets itself on a successful post (hx-on::after-request in _thread.html), and a
// reset clears only the FIELDS - the note is an ordinary element, so it would go on showing the
// filename of a photo already sent and the next reply would look pre-loaded.
document.addEventListener("reset", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  const note = form.querySelector<HTMLElement>(NOTE);
  if (note) clearNote(note);
});
