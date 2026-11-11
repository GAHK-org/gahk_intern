// @mentions: typing "@an" in any [data-mentionable] field offers residents on the alumneliste and
// embedsgrupper, and picking one inserts the token `@[Anders Hansen](42)` or `@[Inspektionen](g55)`
// that the server renders and notifies on (core/mentions.py). Delegated from document, so
// htmx-swapped composers work without re-wiring.

type Resident = { id: number; name: string; avatar: string };
type Group = { id: number; name: string; members: number };
type Option = { ref: string; name: string; avatar: string; detail: string };
type Field = HTMLTextAreaElement | HTMLInputElement;

// The word (or two, for "@anders han") between an "@" and the caret.
const TRIGGER = /(?:^|\s)@([^\s@[\]()]{0,30}(?: [^\s@[\]()]{0,30})?)$/;

const lookupUrl = document.body.dataset.mentionLookup;

let field: Field | null = null;
let start = 0; // index of the "@" being completed
let options: Option[] = [];
let query = ""; // what `options` answers, for the empty state
let active = 0;
let timer = 0;
let request = 0;

const list = document.createElement("ul");
list.className = "mention-picker";
list.id = "mention-picker";
list.setAttribute("role", "listbox");
list.hidden = true;
document.body.append(list);

function isField(target: EventTarget | null): target is Field {
  return (target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement) && "mentionable" in target.dataset;
}

function close(): void {
  options = [];
  query = "";
  list.hidden = true;
  list.replaceChildren();
  field?.removeAttribute("aria-activedescendant");
  field = null;
}

function render(): void {
  list.replaceChildren();
  options.forEach((option, index) => {
    const item = document.createElement("li");
    item.id = `mention-option-${index}`;
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(index === active));
    if (option.avatar) {
      const img = document.createElement("img");
      img.src = option.avatar;
      img.alt = "";
      item.append(img);
    }
    item.append(option.name);
    if (option.detail) {
      const small = document.createElement("small");
      small.textContent = option.detail;
      item.append(small);
    }
    item.addEventListener("mousedown", (event) => {
      event.preventDefault(); // keep focus in the field
      pick(option);
    });
    list.append(item);
  });
  if (!field) {
    list.hidden = true;
    return;
  }
  if (!options.length) {
    // Said rather than vanishing: an empty list looked like the picker had broken, and since more
    // letters only narrow the search it never came back.
    if (!query) {
      list.hidden = true;
      return;
    }
    const empty = document.createElement("li");
    empty.className = "mention-empty";
    empty.setAttribute("aria-disabled", "true");
    empty.textContent = `Ingen beboere eller embedsgrupper matcher "${query}"`;
    list.append(empty);
  }
  list.hidden = false;
  place();
  if (options.length) field?.setAttribute("aria-activedescendant", `mention-option-${active}`);
  else field?.removeAttribute("aria-activedescendant");
}

const GAP = 4; // between the caret line and the list
const EDGE = 8; // kept clear at the edges of the visible area
const MAX_HEIGHT = 260;

// Styles a mirror must copy for its text to wrap exactly like the field's.
const MIRRORED = [
  "boxSizing", "width", "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
  "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "fontFamily", "fontSize", "fontWeight",
  "fontStyle", "letterSpacing", "lineHeight", "textTransform", "wordSpacing", "tabSize",
] as const;

/** The caret's line in viewport coordinates, measured with an off-screen copy of the field. */
function caretRect(target: Field): { left: number; top: number; bottom: number } {
  const style = getComputedStyle(target);
  const mirror = document.createElement("div");
  for (const key of MIRRORED) mirror.style[key] = style[key];
  mirror.style.position = "absolute";
  mirror.style.visibility = "hidden";
  mirror.style.top = "0";
  mirror.style.left = "-9999px";
  mirror.style.overflow = "hidden";
  const multiline = target instanceof HTMLTextAreaElement;
  mirror.style.whiteSpace = multiline ? "pre-wrap" : "pre";
  mirror.style.overflowWrap = multiline ? "break-word" : "normal";
  mirror.textContent = target.value.slice(0, start);
  const marker = document.createElement("span");
  marker.textContent = "@";
  mirror.append(marker);
  document.body.append(mirror);
  const lineHeight = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.3;
  const box = target.getBoundingClientRect();
  // offsetTop/Left count from inside the border; the field's rect counts from outside it.
  const top = box.top + target.clientTop + marker.offsetTop - target.scrollTop;
  const left = box.left + target.clientLeft + marker.offsetLeft - target.scrollLeft;
  mirror.remove();
  return { left, top, bottom: top + lineHeight };
}

type Box = { left: number; top: number; right: number; bottom: number };

/** The part of the page the reader can actually see, in DOCUMENT coordinates — smaller than the
 * window while a phone's keyboard is up. */
function visibleArea(): Box {
  const vv = window.visualViewport;
  if (!vv) {
    const { scrollX: x, scrollY: y } = window;
    return { left: x, top: y, right: x + window.innerWidth, bottom: y + window.innerHeight };
  }
  return { left: vv.pageLeft, top: vv.pageTop, right: vv.pageLeft + vv.width, bottom: vv.pageTop + vv.height };
}

/** Keeps the highlighted option in view by scrolling the list alone; scrollIntoView would also
 * scroll the page on iOS. */
function revealActive(): void {
  const item = list.querySelector<HTMLElement>('[aria-selected="true"]');
  if (!item) return;
  if (item.offsetTop < list.scrollTop) list.scrollTop = item.offsetTop;
  else if (item.offsetTop + item.offsetHeight > list.scrollTop + list.clientHeight)
    list.scrollTop = item.offsetTop + item.offsetHeight - list.clientHeight;
}

/** Under the caret's line if it fits, else above it (Den Hurtige's composer sits at the very
 * bottom), always clamped inside the visible area.
 *
 * Positioned absolutely in document coordinates, not position:fixed. With the keyboard up, iOS
 * scrolls even Den Hurtige's locked page (scrollY ≈ 400), and a fixed element then stays put
 * while getBoundingClientRect follows the scroll — the list landed that far above the field,
 * off screen. Document coordinates agree on every browser. */
function place(): void {
  if (!field || list.hidden) return;
  const area = visibleArea();
  const { scrollX: sx, scrollY: sy } = window;
  const rect = field.getBoundingClientRect();
  const box = { left: rect.left + sx, top: rect.top + sy, right: rect.right + sx, bottom: rect.bottom + sy, width: rect.width };
  if (box.bottom < area.top || box.top > area.bottom) {
    close(); // the field itself scrolled away
    return;
  }
  const caretView = caretRect(field);
  const caret = { left: caretView.left + sx, top: caretView.top + sy, bottom: caretView.bottom + sy };
  // The caret line may be scrolled out of a tall field; anchor to the field's visible part then.
  const lineTop = Math.min(Math.max(caret.top, box.top), box.bottom);
  const lineBottom = Math.min(Math.max(caret.bottom, box.top), box.bottom);

  const width = Math.min(Math.max(box.width, 220), 320, area.right - area.left - 2 * EDGE);
  const left = Math.min(Math.max(caret.left, area.left + EDGE), area.right - width - EDGE);

  const below = area.bottom - lineBottom - GAP - EDGE;
  const above = lineTop - area.top - GAP - EDGE;
  list.style.width = `${width}px`;
  list.style.maxHeight = `${MAX_HEIGHT}px`;
  const wanted = Math.min(list.scrollHeight + 2, MAX_HEIGHT);
  let top: number;
  if (below >= wanted || below >= above) {
    list.style.maxHeight = `${Math.min(Math.max(below, 0), MAX_HEIGHT)}px`;
    top = lineBottom + GAP;
  } else {
    list.style.maxHeight = `${Math.min(above, MAX_HEIGHT)}px`;
    top = lineTop - GAP - Math.min(wanted, above);
  }
  // Wherever the list's containing block starts, measured rather than assumed.
  list.style.left = "0px";
  list.style.top = "0px";
  const origin = list.getBoundingClientRect();
  list.style.left = `${left - (origin.left + sx)}px`;
  list.style.top = `${top - (origin.top + sy)}px`;
  revealActive();
}

async function search(target: Field, q: string): Promise<void> {
  const mine = ++request;
  const response = await fetch(`${lookupUrl}?q=${encodeURIComponent(q)}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok || mine !== request || field !== target) return;
  const data = (await response.json()) as { residents: Resident[]; groups: Group[] };
  options = [
    ...data.residents.map((r) => ({ ref: String(r.id), name: r.name, avatar: r.avatar, detail: "" })),
    ...data.groups.map((g) => ({ ref: `g${g.id}`, name: g.name, avatar: "", detail: `Embedsgruppe · ${g.members}` })),
  ];
  query = q;
  active = 0;
  render();
}

function pick(option: Option): void {
  if (!field) return;
  const target = field;
  const caret = target.selectionStart ?? target.value.length;
  // Brackets would end the token early; the server only needs the pk to be right.
  const token = `@[${option.name.replace(/[[\]\n]/g, "")}](${option.ref}) `;
  target.setRangeText(token, start, caret, "end");
  close();
  target.dispatchEvent(new Event("input", { bubbles: true }));
  target.focus();
}

document.addEventListener("input", (event) => {
  if (!lookupUrl || !isField(event.target)) return;
  const target = event.target;
  const caret = target.selectionStart ?? target.value.length;
  const match = TRIGGER.exec(target.value.slice(0, caret));
  if (!match) {
    if (field === target) close();
    return;
  }
  field = target;
  start = caret - match[1].length - 1;
  target.setAttribute("aria-controls", list.id);
  window.clearTimeout(timer);
  timer = window.setTimeout(() => void search(target, match[1]), 150);
});

// Capture phase, so an open picker gets Enter/arrows before the composer's own send-on-Enter.
document.addEventListener(
  "keydown",
  (event) => {
    if (!field || event.target !== field || list.hidden) return;
    if (!options.length && event.key !== "Escape") return; // only the empty-state row is showing
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      active = (active + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
      render();
    } else if (event.key === "Enter" || event.key === "Tab") {
      pick(options[active]);
    } else if (event.key === "Escape") {
      close();
    } else {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
  },
  true,
);

document.addEventListener("focusout", (event) => {
  if (event.target === field) window.setTimeout(close, 150);
});
// Follow the field rather than closing: Den Hurtige's feed scrolls itself as messages arrive.
window.addEventListener("scroll", (event) => event.target === list || place(), true);
window.addEventListener("resize", place);
window.visualViewport?.addEventListener("resize", place);
window.visualViewport?.addEventListener("scroll", place);
