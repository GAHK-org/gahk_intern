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
const SWIPE_TRIGGER = 68; // px that commits the gesture
const SWIPE_MAX = 96; // px the element will actually move, however far the thumb goes
// px of travel before the icon under the message starts fading in. It is the far edge of the hint
// band itself -- `left:6px` plus its 32px width -- because the icons paint BEHIND the bubble (see
// the .msg-hint block in styles.css): until the message has cleared that much, fading one in only
// lights up the sliver of it that is still sticking out past the bubble's corner.
const SWIPE_HINT_FROM = 38;
// Vertical still wins a tie, and then some: this is a scrolling list first and a gesture surface
// second. But losing the tie no longer ENDS the gesture -- see the axis block in pointermove.
const SWIPE_X_BIAS = 1.3;
// px of vertical travel that ends a gesture outright, on its own, when the finger has not gone
// SWIPE_SLOP sideways in all that distance. That is a scroll however the browser has read it.
const SWIPE_SCROLL_SLOP = 30;

type SwipeAction = "thread" | "delete" | "close";

interface Drag {
  pointerId: number;
  el: HTMLElement;
  startX: number;
  startY: number;
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

function endDrag(commit: boolean): void {
  const active = drag;
  drag = null;
  if (!active) return;
  const { el, action } = active;
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
    if (target.closest("a, button, input, textarea, select, label, .reaction")) return;
    const el = target.closest<HTMLElement>("[data-msg-swipe], .thread-panel");
    if (!el) return;
    drag = {
      pointerId: event.pointerId,
      el,
      startX: event.clientX,
      startY: event.clientY,
      decided: false,
      action: null,
      armed: false,
    };
  });

  document.addEventListener("pointermove", (event: PointerEvent) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;

    if (!drag.decided) {
      // A finger that has gone a long way UP OR DOWN without going anywhere sideways is scrolling,
      // and the browser is already handling it. This is the only thing that ends a gesture from in
      // here; everything below is "not yet", not "no".
      if (Math.abs(dy) >= SWIPE_SCROLL_SLOP && Math.abs(dx) < SWIPE_SLOP) {
        drag = null;
        return;
      }
      // Not enough sideways travel to call the axis on yet.
      if (Math.abs(dx) < SWIPE_SLOP) return;
      // Vertical is still ahead, so KEEP WATCHING rather than giving up. THIS IS THE LINE, and
      // what it replaced is why a right-swipe on someone else's message reportedly worked about
      // one time in five while the same gesture on your own worked every time.
      //
      // The old version answered "is this a swipe?" once and for all on the first pointermove past
      // the slop, and answered NO by throwing the whole gesture away -- so a start the bias did
      // not like meant the message would not budge again however far the thumb travelled after
      // that, because `decided` was never reached and `drag` was already null. Only lifting and
      // starting again could recover it. A thumb swipe is an arc rather than a line, and how much
      // vertical is in its first 12-15px depends on where on the screen it starts and which hand
      // is holding the phone; own messages and other people's sit against opposite edges, so they
      // do not get the same start. Whatever the exact geometry, sentencing a 70px gesture on its
      // first 12px is the bug. Deferring lets the same swipe qualify a few frames later.
      //
      // Nothing is lost by waiting. The bias is checked against TOTAL travel, not the last frame,
      // so a real scroll never out-runs it at any point in the gesture; the branch above catches a
      // straight vertical flick; and `touch-action:pan-y` means the browser fires pointercancel
      // the moment it claims the touch for the scroller.
      if (Math.abs(dx) <= Math.abs(dy) * SWIPE_X_BIAS) return;
      const action = actionFor(drag.el, dx);
      if (!action) {
        drag = null; // nothing lives in that direction, so the bubble must not budge
        return;
      }
      drag.decided = true;
      drag.action = action;
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
    const dir = action === "delete" ? -1 : 1;
    const along = Math.max(0, dx * dir);
    // Past the trigger the bubble keeps moving, but at a quarter speed. That resistance is the
    // feedback: it says the gesture has caught without needing the element to stop dead.
    const eased =
      along <= SWIPE_TRIGGER ? along : SWIPE_TRIGGER + (along - SWIPE_TRIGGER) * 0.25;
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
        Math.max(0, (along - SWIPE_HINT_FROM) / (SWIPE_TRIGGER - SWIPE_HINT_FROM)),
      );
      hint.style.opacity = String(progress);
      hint.style.scale = String(0.7 + progress * 0.3);
    }

    const past = along >= SWIPE_TRIGGER;
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
    endDrag(drag.armed);
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

// ---- reply attachments ------------------------------------------------------------------------
// The same confirmation for the thread panel's reply box, which never had one. It could get away
// without while a reply also required text — you could see you had typed something — but a reply
// may now be a PHOTO ON ITS OWN (see _thread.html and views.create_comment), and then the filename
// is the only evidence on screen that there is anything to send.
//
// Delegated from `document` rather than bound to the input, because the panel does not exist when
// this module runs and is replaced wholesale every time a different thread is opened. `change` does
// not bubble on all legacy engines but does in every browser this PWA supports, and the reply form
// carries data-morph-skip so the panel's own 5s poll cannot wipe the note back out.
document.addEventListener("change", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || input.type !== "file") return;
  const form = input.closest("form.reply-form");
  const note = form?.querySelector<HTMLElement>("[data-reply-file]");
  if (!note) return;
  const name = input.files?.[0]?.name;
  note.textContent = name ? `📎 ${name}` : "";
  note.hidden = !name;
});

// The form resets itself on a successful post (hx-on::after-request in _thread.html), but a reset
// clears only the FIELDS — this note is an ordinary element, so it would keep displaying the
// filename of a photo that has already been sent, and the next reply would look pre-loaded.
document.addEventListener("reset", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  const note = form.querySelector<HTMLElement>("[data-reply-file]");
  if (!note) return;
  note.textContent = "";
  note.hidden = true;
});
