// A Danish date picker for every `<input type="date">` in the app. No-op on pages without one.
//
// The browser's own date box is awkward in ways no attribute fixes: on a desktop it is a row of
// segments you click into one by one, and both the box and its calendar follow the DEVICE's region,
// so a Mac or phone set to the US shows mm/dd/yyyy and starts the week on Sunday. Choosing a
// birthday means clicking back through decades a month at a time.
//
// So each one becomes a text box that shows "fre. 5. dec. 2026", takes a typed date ("5.12",
// "5/12/26", "24 dec"), and opens a calendar: Danish, Monday first, week numbers, a month and a year
// dropdown, "I dag" and "Ryd". The original input stays in the form as a hidden field holding
// YYYY-MM-DD, so the server, GET filters and anything listening for `change` see exactly what the
// native box sent. Without JavaScript the native box is simply left alone.
//
// On a phone the text box asks for no keyboard (inputmode=none): the calendar is quicker to tap
// than the date is to type, and the keyboard would cover it.

import { coarsePointer, float } from "./floating";

const MONTHS = [
  "januar", "februar", "marts", "april", "maj", "juni",
  "juli", "august", "september", "oktober", "november", "december",
];
const MONTHS_SHORT = ["jan.", "feb.", "mar.", "apr.", "maj", "jun.", "jul.", "aug.", "sep.", "okt.", "nov.", "dec."];
const WEEKDAYS = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"];
const WEEKDAYS_SHORT = ["man.", "tirs.", "ons.", "tors.", "fre.", "lør.", "søn."];
const WEEKDAY_HEADS = ["ma", "ti", "on", "to", "fr", "lø", "sø"];

// --- dates as UTC midnights, so no daylight-saving hour ever moves a day -------------------------

const DAY = 86_400_000;
const ymd = (y: number, m: number, d: number): Date => new Date(Date.UTC(y, m, d));
const today = (): Date => {
  const now = new Date();
  return ymd(now.getFullYear(), now.getMonth(), now.getDate());
};
const addDays = (date: Date, days: number): Date => new Date(date.getTime() + days * DAY);
const addMonths = (date: Date, months: number): Date => {
  const first = ymd(date.getUTCFullYear(), date.getUTCMonth() + months, 1);
  const last = ymd(first.getUTCFullYear(), first.getUTCMonth() + 1, 0).getUTCDate();
  return ymd(first.getUTCFullYear(), first.getUTCMonth(), Math.min(date.getUTCDate(), last));
};
const mondayIndex = (date: Date): number => (date.getUTCDay() + 6) % 7;
const sameDay = (a: Date | null, b: Date | null): boolean => !!a && !!b && a.getTime() === b.getTime();

export const toIso = (date: Date): string => date.toISOString().slice(0, 10);

export function fromIso(text: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  return match ? valid(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : null;
}

function valid(y: number, m: number, d: number): Date | null {
  const date = ymd(y, m, d);
  return date.getUTCFullYear() === y && date.getUTCMonth() === m && date.getUTCDate() === d ? date : null;
}

// ISO 8601 week number, the one printed in Danish calendars.
function weekNumber(date: Date): number {
  const thursday = addDays(date, 3 - mondayIndex(date));
  const firstThursday = ymd(thursday.getUTCFullYear(), 0, 4);
  return 1 + Math.round((thursday.getTime() - addDays(firstThursday, 3 - mondayIndex(firstThursday)).getTime()) / (7 * DAY));
}

export const formatDate = (date: Date): string =>
  `${WEEKDAYS_SHORT[mondayIndex(date)]} ${date.getUTCDate()}. ${MONTHS_SHORT[date.getUTCMonth()]} ${date.getUTCFullYear()}`;

const longDate = (date: Date): string =>
  `${WEEKDAYS[mondayIndex(date)]} ${date.getUTCDate()}. ${MONTHS[date.getUTCMonth()]} ${date.getUTCFullYear()}`;

// --- reading what was typed ----------------------------------------------------------------------

function yearOf(text: string | undefined, month: number, day: number, base: Date): number | null {
  if (text === undefined) {
    // No year: whichever puts the date nearest the base (normally today), so "5.1" typed in December
    // is next January and "20.11" typed in January for a filter is the November just gone.
    const y = base.getUTCFullYear();
    const candidates = [y - 1, y, y + 1].filter((year) => valid(year, month, day));
    if (candidates.length === 0) return null;
    return candidates.reduce((best, year) =>
      Math.abs(ymd(year, month, day).getTime() - base.getTime()) <
      Math.abs(ymd(best, month, day).getTime() - base.getTime())
        ? year
        : best,
    );
  }
  if (text.length === 4) return Number(text);
  // Two digits: this century unless that is more than ten years ahead ("98" is a birthday).
  const year = 2000 + Number(text);
  return year > base.getUTCFullYear() + 10 ? year - 100 : year;
}

function monthFromName(name: string): number {
  const word = name.replace(/\.$/, "");
  if (word.length < 3) return -1;
  return MONTHS.findIndex((month) => month.startsWith(word));
}

export function parseDate(typed: string, base: Date = today()): Date | null {
  // A leading weekday is what this box itself displays ("fre. 5. dec. 2026"), so it must read back.
  const text = typed
    .trim()
    .toLowerCase()
    .replace(/^[a-zæøå]+\.?,?\s+(?=\d)/, "");
  let match = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(text);
  if (match) return valid(Number(match[1]), Number(match[2]) - 1, Number(match[3]));

  let day: number;
  let month: number;
  let year: string | undefined;
  if ((match = /^(\d{1,2})\s*[./\- ]\s*(\d{1,2})(?:\s*[./\- ]\s*(\d{2}|\d{4}))?\.?$/.exec(text))) {
    [day, month, year] = [Number(match[1]), Number(match[2]) - 1, match[3]];
  } else if ((match = /^(\d{2})(\d{2})(\d{2}|\d{4})?$/.exec(text))) {
    [day, month, year] = [Number(match[1]), Number(match[2]) - 1, match[3]];
  } else if ((match = /^(\d{1,2})\.?\s*([a-zæøå]+\.?)(?:\s*(\d{2}|\d{4}))?$/.exec(text))) {
    [day, month, year] = [Number(match[1]), monthFromName(match[2]), match[3]];
  } else {
    return null;
  }
  if (month < 0 || month > 11) return null;
  const y = yearOf(year, month, day, base);
  return y === null ? null : valid(y, month, day);
}

// --- the picker ----------------------------------------------------------------------------------

const pickers = new WeakMap<HTMLInputElement, DatePicker>();

/** Set a picker's date from code (core.date_and_time's linked fields), as if it had been chosen. */
export function setDate(field: HTMLInputElement, date: Date | null): void {
  const picker = pickers.get(field);
  if (picker) picker.commit(date);
  else {
    field.value = date ? toIso(date) : "";
    field.dispatchEvent(new Event("change", { bubbles: true }));
  }
}

let nextId = 0;

class DatePicker {
  readonly box: HTMLInputElement;
  private close: (() => void) | null = null;
  private panel: HTMLElement | null = null;
  private shown: Date = today(); // the first of the month on display
  private focused: Date = today(); // the day the arrow keys move

  constructor(readonly field: HTMLInputElement) {
    const box = document.createElement("input");
    box.type = "text";
    box.className = `${field.className} date-picker-box`.trim();
    if (field.getAttribute("style")) box.setAttribute("style", field.getAttribute("style") ?? "");
    // The <label for> points at the id, so the visible box takes it over.
    if (field.id) {
      box.id = field.id;
      field.removeAttribute("id");
    }
    for (const attr of ["aria-label", "aria-describedby", "aria-invalid", "title"]) {
      const value = field.getAttribute(attr);
      if (value !== null) box.setAttribute(attr, value);
    }
    box.required = field.required;
    box.disabled = field.disabled;
    box.readOnly = field.readOnly;
    box.autocomplete = "off";
    box.placeholder = "dd.mm.åååå";
    box.setAttribute("role", "combobox");
    box.setAttribute("aria-haspopup", "dialog");
    box.setAttribute("aria-expanded", "false");
    if (coarsePointer()) box.inputMode = "none";

    // Hidden, the original keeps its name and value but no longer validates: `required` moves to
    // the box, where the browser can point at it.
    field.required = false;
    field.type = "hidden";
    field.before(box);
    this.box = box;
    this.showValue();
    pickers.set(field, this);

    box.addEventListener("click", () => this.open());
    box.addEventListener("keydown", (event) => this.onBoxKey(event));
    box.addEventListener("input", () => this.onTyping());
    box.addEventListener("change", () => this.readTyped());
    box.addEventListener("focus", () => box.select());
    box.addEventListener("focusout", (event) => this.onFocusOut(event));
  }

  private get value(): Date | null {
    return fromIso(this.field.value);
  }

  private get min(): Date | null {
    return fromIso(this.field.getAttribute("min") ?? "");
  }

  private get max(): Date | null {
    return fromIso(this.field.getAttribute("max") ?? "");
  }

  // What a date typed without a year is read near: the date already there, else the day the field
  // is limited by (the start, for an end or a svarfrist), else today.
  private get anchor(): Date {
    return this.value ?? this.min ?? this.max ?? today();
  }

  private allowed(date: Date): boolean {
    const { min, max } = this;
    return !(min && date < min) && !(max && date > max);
  }

  private showValue(): void {
    const value = this.value;
    this.box.value = value ? formatDate(value) : this.field.value ? this.field.value : "";
    this.box.setCustomValidity("");
  }

  commit(date: Date | null): void {
    const before = this.field.value;
    this.field.value = date ? toIso(date) : "";
    this.showValue();
    if (this.field.value !== before) {
      this.field.dispatchEvent(new Event("input", { bubbles: true }));
      this.field.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  private readTyped(): void {
    const text = this.box.value.trim();
    if (!text) return this.commit(null);
    const date = parseDate(text, this.anchor);
    if (!date) {
      this.box.setCustomValidity("Skriv datoen som fx 24.12.2026.");
    } else if (!this.allowed(date)) {
      const { min, max } = this;
      this.box.setCustomValidity(
        min && date < min ? `Vælg ${formatDate(min)} eller senere.` : `Vælg ${formatDate(max as Date)} eller før.`,
      );
    } else {
      return this.commit(date);
    }
    // The old date must not go on being sent under text that says something else: a form with
    // `novalidate` (Hjemmeside's) would submit it, and the server then reports the date as missing.
    if (this.field.value) {
      this.field.value = "";
      this.field.dispatchEvent(new Event("change", { bubbles: true }));
    }
    this.box.reportValidity();
  }

  private onTyping(): void {
    this.box.setCustomValidity("");
    const date = parseDate(this.box.value, this.anchor);
    if (date && this.panel) {
      this.focused = date;
      this.show(date);
    }
  }

  private onBoxKey(event: KeyboardEvent): void {
    if (event.key === "ArrowDown" || (event.key === "Enter" && !this.panel && !this.box.value.trim())) {
      event.preventDefault();
      this.open();
      this.focusDay();
    } else if (event.key === "Escape" && this.panel) {
      event.preventDefault();
      this.hide();
    } else if (event.key === "Enter" && this.panel) {
      // Enter in the box takes what was typed rather than submitting the form under the calendar.
      event.preventDefault();
      this.readTyped();
      this.hide();
    } else if (event.key === "Enter") {
      // Submitting with Enter: read the typed date first, so the form sends it.
      this.readTyped();
    }
  }

  private onFocusOut(event: FocusEvent): void {
    const next = event.relatedTarget;
    if (next instanceof Node && (this.box.contains(next) || this.panel?.contains(next))) return;
    // A tap inside the panel on Safari blurs with no relatedTarget; the pointerdown handler below
    // decides those, so only a real move elsewhere closes it here.
    if (next !== null) this.hide();
  }

  open(): void {
    if (this.panel || this.box.disabled || this.box.readOnly) return;
    const start = this.value ?? parseDate(this.box.value, this.anchor) ?? this.near();
    this.focused = start;
    this.panel = this.build();
    this.show(start);
    this.close = float(this.panel, this.box);
    this.box.setAttribute("aria-expanded", "true");
    document.addEventListener("pointerdown", this.onOutside, true);
  }

  // Where an empty calendar opens: at the day a field must come before (a svarfrist opens on the
  // event's month, since that is where it will be), else today, moved up to the day it must come
  // after (an end opens on the start).
  private near(): Date {
    const { min, max } = this;
    if (max) return max;
    const now = today();
    return min && now < min ? min : now;
  }

  hide(): void {
    if (!this.panel) return;
    this.close?.();
    this.close = null;
    this.panel = null;
    this.box.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", this.onOutside, true);
  }

  private onOutside = (event: PointerEvent): void => {
    const target = event.target as Node;
    if (target !== this.box && !this.panel?.contains(target)) this.hide();
  };

  private build(): HTMLElement {
    const panel = document.createElement("div");
    panel.className = "date-picker";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Vælg dato");
    panel.id = `date-picker-${++nextId}`;
    this.box.setAttribute("aria-controls", panel.id);

    const now = today().getUTCFullYear();
    const first = this.min?.getUTCFullYear() ?? Math.min(now - 100, this.value?.getUTCFullYear() ?? now);
    const last = this.max?.getUTCFullYear() ?? Math.max(now + 10, this.value?.getUTCFullYear() ?? now);
    const years = Array.from({ length: last - first + 1 }, (_, i) => first + i);

    panel.innerHTML = `
      <div class="date-picker-head">
        <button type="button" class="date-picker-step" data-step="-1" aria-label="Forrige måned">‹</button>
        <select class="date-picker-month" aria-label="Måned">
          ${MONTHS.map((name, i) => `<option value="${i}">${name}</option>`).join("")}
        </select>
        <select class="date-picker-year" aria-label="År">
          ${years.map((year) => `<option value="${year}">${year}</option>`).join("")}
        </select>
        <button type="button" class="date-picker-step" data-step="1" aria-label="Næste måned">›</button>
      </div>
      <table class="date-picker-grid" role="grid">
        <thead><tr><th class="date-picker-week" scope="col" title="Uge">uge</th>
          ${WEEKDAY_HEADS.map((head, i) => `<th scope="col" abbr="${WEEKDAYS[i]}">${head}</th>`).join("")}
        </tr></thead>
        <tbody></tbody>
      </table>
      <div class="date-picker-foot">
        <button type="button" class="date-picker-today">I dag</button>
        ${this.box.required ? "" : '<button type="button" class="date-picker-clear">Ryd</button>'}
      </div>`;

    panel.addEventListener("click", (event) => {
      const button = (event.target as Element).closest("button");
      if (!button) return;
      if (button.dataset.date) {
        this.commit(fromIso(button.dataset.date));
        this.hide();
        this.box.focus();
      } else if (button.dataset.step) {
        this.show(addMonths(this.shown, Number(button.dataset.step)));
      } else if (button.classList.contains("date-picker-today")) {
        const now = today();
        if (this.allowed(now)) {
          this.commit(now);
          this.hide();
          this.box.focus();
        } else this.show(now);
      } else if (button.classList.contains("date-picker-clear")) {
        this.commit(null);
        this.hide();
        this.box.focus();
      }
    });
    panel.addEventListener("change", () => {
      const month = panel.querySelector<HTMLSelectElement>(".date-picker-month");
      const year = panel.querySelector<HTMLSelectElement>(".date-picker-year");
      if (month && year) this.show(ymd(Number(year.value), Number(month.value), 1));
    });
    panel.addEventListener("keydown", (event) => this.onPanelKey(event));
    panel.addEventListener("focusout", (event) => this.onFocusOut(event));
    return panel;
  }

  private show(date: Date): void {
    const panel = this.panel;
    if (!panel) return;
    this.shown = ymd(date.getUTCFullYear(), date.getUTCMonth(), 1);
    const month = panel.querySelector<HTMLSelectElement>(".date-picker-month");
    const year = panel.querySelector<HTMLSelectElement>(".date-picker-year");
    if (month) month.value = String(this.shown.getUTCMonth());
    if (year) {
      if (![...year.options].some((o) => o.value === String(this.shown.getUTCFullYear()))) {
        year.add(new Option(String(this.shown.getUTCFullYear())));
      }
      year.value = String(this.shown.getUTCFullYear());
    }
    for (const step of panel.querySelectorAll<HTMLButtonElement>(".date-picker-step")) {
      const target = addMonths(this.shown, Number(step.dataset.step));
      const { min, max } = this;
      step.disabled =
        (!!min && Number(step.dataset.step) < 0 && ymd(target.getUTCFullYear(), target.getUTCMonth() + 1, 0) < min) ||
        (!!max && Number(step.dataset.step) > 0 && target > max);
    }

    const value = this.value;
    const now = today();
    if (this.focused.getUTCMonth() !== this.shown.getUTCMonth() || this.focused.getUTCFullYear() !== this.shown.getUTCFullYear()) {
      const last = ymd(this.shown.getUTCFullYear(), this.shown.getUTCMonth() + 1, 0).getUTCDate();
      this.focused = ymd(this.shown.getUTCFullYear(), this.shown.getUTCMonth(), Math.min(this.focused.getUTCDate(), last));
    }
    let day = addDays(this.shown, -mondayIndex(this.shown));
    const rows: string[] = [];
    for (let week = 0; week < 6; week++) {
      const cells = [`<th class="date-picker-week" scope="row">${weekNumber(day)}</th>`];
      for (let i = 0; i < 7; i++, day = addDays(day, 1)) {
        const classes = [
          day.getUTCMonth() !== this.shown.getUTCMonth() ? "is-outside" : "",
          sameDay(day, now) ? "is-today" : "",
        ].join(" ").trim();
        cells.push(
          `<td><button type="button" class="${classes}" data-date="${toIso(day)}"` +
            ` tabindex="${sameDay(day, this.focused) ? 0 : -1}"` +
            ` aria-label="${longDate(day)}"` +
            (sameDay(day, value) ? ' aria-pressed="true"' : "") +
            (sameDay(day, now) ? ' aria-current="date"' : "") +
            (this.allowed(day) ? "" : " disabled") +
            `>${day.getUTCDate()}</button></td>`,
        );
      }
      rows.push(`<tr>${cells.join("")}</tr>`);
    }
    const body = panel.querySelector("tbody");
    if (body) body.innerHTML = rows.join("");
  }

  private focusDay(): void {
    this.panel?.querySelector<HTMLButtonElement>(`[data-date="${toIso(this.focused)}"]`)?.focus();
  }

  private onPanelKey(event: KeyboardEvent): void {
    if (event.key === "Escape") {
      event.preventDefault();
      this.hide();
      this.box.focus();
      return;
    }
    const onDay = (event.target as Element).matches("[data-date]");
    if (!onDay) return;
    const moves: Record<string, () => Date> = {
      ArrowLeft: () => addDays(this.focused, -1),
      ArrowRight: () => addDays(this.focused, 1),
      ArrowUp: () => addDays(this.focused, -7),
      ArrowDown: () => addDays(this.focused, 7),
      Home: () => addDays(this.focused, -mondayIndex(this.focused)),
      End: () => addDays(this.focused, 6 - mondayIndex(this.focused)),
      PageUp: () => addMonths(this.focused, event.shiftKey ? -12 : -1),
      PageDown: () => addMonths(this.focused, event.shiftKey ? 12 : 1),
    };
    const move = moves[event.key];
    if (!move) return;
    event.preventDefault();
    this.focused = move();
    this.show(this.focused);
    this.focusDay();
  }
}

function enhance(root: ParentNode): void {
  for (const field of root.querySelectorAll<HTMLInputElement>('input[type="date"]')) {
    if (!pickers.has(field)) new DatePicker(field);
  }
}

enhance(document);
// Forms htmx swaps in later get the same picker.
document.addEventListener("htmx:load", (event) => enhance(event.target as ParentNode));
