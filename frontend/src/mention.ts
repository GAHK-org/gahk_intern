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
  if (!field || !options.length) {
    list.hidden = true;
    return;
  }
  list.hidden = false;
  place();
  field?.setAttribute("aria-activedescendant", `mention-option-${active}`);
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

/** The part of the viewport the reader can actually see — smaller than the window while a phone's
 * keyboard is up. */
function visibleArea(): { left: number; top: number; right: number; bottom: number } {
  const vv = window.visualViewport;
  if (!vv) return { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight };
  return { left: vv.offsetLeft, top: vv.offsetTop, right: vv.offsetLeft + vv.width, bottom: vv.offsetTop + vv.height };
}

/** Under the caret's line if it fits, else above it (Den Hurtige's composer sits at the very
 * bottom), always clamped inside the visible area. */
function place(): void {
  if (!field || list.hidden) return;
  const area = visibleArea();
  const box = field.getBoundingClientRect();
  if (box.bottom < area.top || box.top > area.bottom) {
    close(); // the field itself scrolled away
    return;
  }
  const caret = caretRect(field);
  // The caret line may be scrolled out of a tall field; anchor to the field's visible part then.
  const lineTop = Math.min(Math.max(caret.top, box.top), box.bottom);
  const lineBottom = Math.min(Math.max(caret.bottom, box.top), box.bottom);

  const width = Math.min(Math.max(box.width, 220), 320, area.right - area.left - 2 * EDGE);
  list.style.width = `${width}px`;
  list.style.left = `${Math.min(Math.max(caret.left, area.left + EDGE), area.right - width - EDGE)}px`;

  const below = area.bottom - lineBottom - GAP - EDGE;
  const above = lineTop - area.top - GAP - EDGE;
  list.style.maxHeight = `${MAX_HEIGHT}px`;
  const wanted = Math.min(list.scrollHeight + 2, MAX_HEIGHT);
  if (below >= wanted || below >= above) {
    list.style.maxHeight = `${Math.max(below, 0)}px`;
    list.style.top = `${lineBottom + GAP}px`;
  } else {
    list.style.maxHeight = `${above}px`;
    list.style.top = `${lineTop - GAP - Math.min(wanted, above)}px`;
  }
  list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
}

async function search(target: Field, query: string): Promise<void> {
  const mine = ++request;
  const response = await fetch(`${lookupUrl}?q=${encodeURIComponent(query)}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok || mine !== request || field !== target) return;
  const data = (await response.json()) as { residents: Resident[]; groups: Group[] };
  options = [
    ...data.residents.map((r) => ({ ref: String(r.id), name: r.name, avatar: r.avatar, detail: "" })),
    ...data.groups.map((g) => ({ ref: `g${g.id}`, name: g.name, avatar: "", detail: `Embedsgruppe · ${g.members}` })),
  ];
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
