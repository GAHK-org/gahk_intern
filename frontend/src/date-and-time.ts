// The date-and-time fields of core.date_and_time: a date (see ./date-picker) and a 24-hour time box
// (`input[data-clock]`). No-op on pages without one.
//
// Three jobs:
//
//   1. Tidy what was typed. A phone's number pad has no colon, so "1930" is a valid way to type half
//      past seven; once the box is left it reads "19:30", so what is on screen is what is saved.
//      Anything that is not a time is left for the server to reject with a message.
//   2. A list of the day's half-hours under the box, so a time is two taps rather than five
//      keystrokes. Typing still works for any other minute, and moves the list along.
//   3. Fields that belong together (`data-after` / `data-before` naming another field): an end
//      time typed with no end date lands on the start's day, or the next day when it is earlier
//      than the start (a party ending at 02:00); moving the start moves the end with it, keeping
//      the length; and the calendars cannot pick an end before the start or a svarfrist after it.

import { fromIso, setDate } from "./date-picker";
import { float } from "./floating";

const CLOCK = /^(\d{1,2})(?:[:.,]?(\d{2}))?$/;

export function tidyClock(text: string): string | null {
  const match = CLOCK.exec(text.trim().replace(/^kl\.\s*/i, ""));
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2] ?? 0);
  if (hour > 23 || minute > 59) return null;
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

const minutesOf = (clock: string): number | null => {
  const tidy = tidyClock(clock);
  return tidy === null ? null : Number(tidy.slice(0, 2)) * 60 + Number(tidy.slice(3));
};
const clockOf = (minutes: number): string =>
  `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;

// --- 2. the list of times -------------------------------------------------------------------------

const TIMES = Array.from({ length: 48 }, (_, i) => clockOf(i * 30));
let nextId = 0;

class TimeList {
  private panel: HTMLElement | null = null;
  private close: (() => void) | null = null;
  private active = -1;

  constructor(private readonly box: HTMLInputElement) {
    box.setAttribute("role", "combobox");
    box.setAttribute("aria-autocomplete", "list");
    box.setAttribute("aria-expanded", "false");
    box.addEventListener("click", () => this.open());
    box.addEventListener("input", () => this.follow());
    box.addEventListener("keydown", (event) => this.onKey(event));
    box.addEventListener("focusout", (event) => {
      const next = event.relatedTarget;
      if (next !== null && !(next instanceof Node && this.panel?.contains(next))) this.hide();
    });
  }

  private open(): void {
    if (this.panel || this.box.disabled || this.box.readOnly) return;
    const panel = document.createElement("div");
    panel.className = "time-list";
    panel.id = `time-list-${++nextId}`;
    panel.setAttribute("role", "listbox");
    panel.setAttribute("aria-label", "Klokkeslæt");
    panel.innerHTML = TIMES.map(
      (time, i) => `<div role="option" id="${panel.id}-${i}" data-time="${time}" aria-selected="false">${time}</div>`,
    ).join("");
    // pointerdown, not click: picking must not first blur the box (and close the list under it).
    panel.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      const option = (event.target as Element).closest<HTMLElement>("[data-time]");
      if (option?.dataset.time) this.pick(option.dataset.time);
    });
    this.panel = panel;
    this.box.setAttribute("aria-controls", panel.id);
    this.box.setAttribute("aria-expanded", "true");
    this.close = float(panel, this.box);
    document.addEventListener("pointerdown", this.onOutside, true);

    // Start at what is in the box, or at the time it is now — somewhere useful rather than midnight.
    const typed = minutesOf(this.box.value);
    const now = new Date();
    const around = typed ?? now.getHours() * 60 + now.getMinutes();
    this.highlight(typed === null ? -1 : Math.round(typed / 30) % 48, Math.floor(around / 30));
  }

  private hide(): void {
    if (!this.panel) return;
    this.close?.();
    this.close = null;
    this.panel = null;
    this.active = -1;
    this.box.setAttribute("aria-expanded", "false");
    this.box.removeAttribute("aria-activedescendant");
    document.removeEventListener("pointerdown", this.onOutside, true);
  }

  private onOutside = (event: PointerEvent): void => {
    if (event.target !== this.box && !this.panel?.contains(event.target as Node)) this.hide();
  };

  // `index` is the option to mark (-1 for none); `scrollTo` the one to bring into view.
  private highlight(index: number, scrollTo = index): void {
    const panel = this.panel;
    if (!panel) return;
    this.active = index;
    panel.querySelectorAll<HTMLElement>("[data-time]").forEach((option, i) => {
      option.setAttribute("aria-selected", String(i === index));
    });
    if (index >= 0) this.box.setAttribute("aria-activedescendant", `${panel.id}-${index}`);
    else this.box.removeAttribute("aria-activedescendant");
    const target = panel.children[Math.max(0, scrollTo)] as HTMLElement | undefined;
    if (target) panel.scrollTop = target.offsetTop - panel.clientHeight / 2 + target.offsetHeight / 2;
  }

  // Typing moves the list to the first time that starts with the digits so far: "19" -> 19:00,
  // "193" -> 19:30, "7" -> 07:00.
  private follow(): void {
    if (!this.panel) this.open();
    let digits = this.box.value.replace(/\D/g, "");
    if (!digits) return this.highlight(-1);
    if (digits.length === 1 && Number(digits) > 2) digits = `0${digits}`;
    if (digits.length === 3 && Number(digits.slice(0, 2)) > 23) digits = `0${digits}`;
    this.highlight(TIMES.findIndex((time) => time.replace(":", "").startsWith(digits)));
  }

  private onKey(event: KeyboardEvent): void {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!this.panel) return this.open();
      const step = event.key === "ArrowDown" ? 1 : -1;
      const typed = minutesOf(this.box.value);
      const from = this.active >= 0 ? this.active : typed === null ? (step > 0 ? -1 : 48) : Math.floor(typed / 30);
      this.highlight((from + step + 48) % 48);
    } else if (event.key === "Enter" && this.panel) {
      // Enter takes the highlighted time instead of submitting the form under the list.
      event.preventDefault();
      if (this.active >= 0) this.pick(TIMES[this.active]);
      else {
        this.hide();
        this.box.dispatchEvent(new Event("change", { bubbles: true }));
      }
    } else if (event.key === "Escape" && this.panel) {
      event.preventDefault();
      this.hide();
    }
  }

  private pick(time: string): void {
    this.box.value = time;
    this.hide();
    this.box.dispatchEvent(new Event("change", { bubbles: true }));
  }
}

// --- 3. fields that belong together -----------------------------------------------------------------

interface Group {
  date: HTMLInputElement; // the YYYY-MM-DD field the date picker keeps
  clock: HTMLInputElement;
}

function groupIn(root: HTMLElement): Group | null {
  const name = CSS.escape(`${root.dataset.dateAndTime ?? ""}_date`);
  const date = root.querySelector<HTMLInputElement>(`input[name="${name}"]`);
  const clock = root.querySelector<HTMLInputElement>("[data-clock]");
  return date && clock ? { date, clock } : null;
}

function groupNamed(scope: ParentNode, name: string): Group | null {
  const root = scope.querySelector<HTMLElement>(`[data-date-and-time="${CSS.escape(name)}"]`);
  return root && groupIn(root);
}

// A moment as wall-clock minutes since the epoch (no time zone): only ever compared and shifted.
function momentOf(group: Group): number | null {
  const day = fromIso(group.date.value);
  const minutes = minutesOf(group.clock.value);
  return day && minutes !== null ? day.getTime() / 60_000 + minutes : null;
}

function setMoment(group: Group, moment: number): void {
  const minutes = ((moment % 1440) + 1440) % 1440;
  group.clock.value = clockOf(minutes);
  setDate(group.date, new Date((moment - minutes) * 60_000));
}

const lastMoment = new WeakMap<HTMLElement, number | null>();

function followLeader(leaderRoot: HTMLElement): void {
  const leader = groupIn(leaderRoot);
  const name = leaderRoot.dataset.dateAndTime ?? "";
  if (!leader || !name) return;
  const now = momentOf(leader);
  const before = lastMoment.get(leaderRoot) ?? null;
  lastMoment.set(leaderRoot, now);

  const scope = leaderRoot.closest("form") ?? document;
  const selector = `[data-after="${CSS.escape(name)}"], [data-before="${CSS.escape(name)}"]`;
  for (const root of scope.querySelectorAll<HTMLElement>(selector)) {
    const follower = groupIn(root);
    if (!follower) continue;
    const after = root.dataset.after === name;
    // The calendar's limit follows the leader's day.
    const limit = after ? "min" : "max";
    if (leader.date.value) follower.date.setAttribute(limit, leader.date.value);
    else follower.date.removeAttribute(limit);
    // An end moves with its start, keeping the length. A svarfrist stays put: it is a date of its
    // own, and the server says so if the start is moved past it.
    const theirs = momentOf(follower);
    if (after && before !== null && now !== null && theirs !== null && now !== before) {
      setMoment(follower, theirs + (now - before));
    }
  }
}

function fillEndDay(root: HTMLElement): void {
  const end = groupIn(root);
  const start = groupNamed(root.closest("form") ?? document, root.dataset.after ?? "");
  if (!end || !start || end.date.value) return;
  const day = fromIso(start.date.value);
  const endMinutes = minutesOf(end.clock.value);
  if (!day || endMinutes === null) return;
  const startMinutes = minutesOf(start.clock.value);
  const nextDay = startMinutes !== null && endMinutes <= startMinutes;
  setDate(end.date, nextDay ? new Date(day.getTime() + 86_400_000) : day);
}

// --- wiring ----------------------------------------------------------------------------------------

document.addEventListener("change", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement)) return;
  if (target.matches("[data-clock]")) {
    const tidy = tidyClock(target.value);
    if (tidy !== null && tidy !== target.value) target.value = tidy;
  }
  const root = target.closest<HTMLElement>("[data-date-and-time]");
  if (!root) return;
  if (root.dataset.after && target.matches("[data-clock]")) fillEndDay(root);
  followLeader(root);
});

for (const box of document.querySelectorAll<HTMLInputElement>("input[data-clock]")) new TimeList(box);
for (const root of document.querySelectorAll<HTMLElement>("[data-date-and-time]")) followLeader(root);
