// How a picture sits in a page's text: a placement and a size, stored as two classes on the <img>
// (`img-left img-medium`). gahk.dk's stylesheet gives them their meaning (gahk_web styles/site.css),
// and the intranet's copy of it (.prose-gahk in styles.css) makes the editor show the same thing.
//
// Classes, not inline sizes: a size is a share of the text column, so a picture follows the column
// down to a phone, where gahk.dk stacks floated pictures under each other instead of beside a sliver
// of text. The old pages' pictures keep their inline pixel sizes until somebody picks a layout for
// them; then those go, and the classes take over.
//
// Shared by the image picker (cms.ts) and the visual editor's picture bar (cms-editor.ts). Imports
// nothing from the main bundle (see cms-loader.ts).

export type Placement = "left" | "center" | "right";
export type ImageSize = "small" | "medium" | "large" | "full";

export interface ImageLayout {
  placement: Placement;
  size: ImageSize;
}

export const DEFAULT_LAYOUT: ImageLayout = { placement: "center", size: "large" };

export const PLACEMENTS: { value: Placement; label: string; title: string; icon: string }[] = [
  {
    value: "left",
    label: "Venstre",
    title: "Til venstre, med teksten ved siden af",
    icon: pictogram('<rect x="1" y="3" width="7" height="6" rx="1"/>', [
      [10, 15, 3.5],
      [10, 15, 6],
      [10, 15, 8.5],
      [1, 15, 11.5],
      [1, 15, 14],
    ]),
  },
  {
    value: "center",
    label: "Midt",
    title: "På sin egen linje, midt i teksten",
    icon: pictogram('<rect x="4" y="4.5" width="8" height="7" rx="1"/>', [
      [1, 15, 2],
      [1, 15, 14],
    ]),
  },
  {
    value: "right",
    label: "Højre",
    title: "Til højre, med teksten ved siden af",
    icon: pictogram('<rect x="8" y="3" width="7" height="6" rx="1"/>', [
      [1, 6, 3.5],
      [1, 6, 6],
      [1, 6, 8.5],
      [1, 15, 11.5],
      [1, 15, 14],
    ]),
  },
];

export const SIZES: { value: ImageSize; label: string; title: string }[] = [
  { value: "small", label: "Lille", title: "En tredjedel af tekstens bredde" },
  { value: "medium", label: "Mellem", title: "Halvdelen af tekstens bredde" },
  { value: "large", label: "Stor", title: "Tre fjerdedele af tekstens bredde" },
  { value: "full", label: "Fuld bredde", title: "Hele tekstens bredde" },
];

/** Beside the text a picture is at most half the column, or the text next to it is a sliver. */
const BESIDE_TEXT: ImageSize[] = ["small", "medium"];

/** `layout` made possible after `changed` was picked: a big picture moved to the side becomes
 * medium, and a side picture made big moves to the middle. */
export function fitLayout(layout: ImageLayout, changed: "placement" | "size"): ImageLayout {
  if (layout.placement === "center" || BESIDE_TEXT.includes(layout.size)) return layout;
  return changed === "placement" ? { ...layout, size: "medium" } : { ...layout, placement: "center" };
}

const LAYOUT_CLASS = /^img-(left|center|right|small|medium|large|full)$/;
// What an old page's inline style did that the classes now do. Anything else in it stays.
const OLD_LAYOUT_STYLE = /^(float|width|height|max-width|min-width|margin(-(top|right|bottom|left))?|display|vertical-align)$/;

export interface ImageAttributes {
  class?: string | null;
  style?: string | null;
}

/** The attributes that give a picture `layout`: its own classes plus the two layout classes, its
 * style without the old sizes and floats, and no fixed width or height. */
export function layoutAttributes(
  current: ImageAttributes,
  layout: ImageLayout,
): { class: string; style: string | null; width: null; height: null } {
  const classes = (current.class ?? "").split(/\s+/).filter((name) => name && !LAYOUT_CLASS.test(name));
  classes.push(`img-${layout.placement}`, `img-${layout.size}`);
  const style = (current.style ?? "")
    .split(";")
    .map((declaration) => declaration.trim())
    .filter((declaration) => {
      const property = declaration.slice(0, declaration.indexOf(":")).trim().toLowerCase();
      return declaration.includes(":") && !OLD_LAYOUT_STYLE.test(property);
    })
    .map((declaration) => `${declaration};`)
    .join(" ");
  return { class: classes.join(" "), style: style || null, width: null, height: null };
}

/** The layout a picture has: from its classes, or for an old picture only the side its inline
 * float puts it on (its pixel size is none of the four sizes). */
export function layoutOf(current: ImageAttributes): Partial<ImageLayout> {
  const classes = new Set((current.class ?? "").split(/\s+/));
  const float = /(?:^|;)\s*float\s*:\s*(left|right)/i.exec(current.style ?? "")?.[1]?.toLowerCase();
  return {
    placement: PLACEMENTS.find((p) => classes.has(`img-${p.value}`))?.value ?? (float as Placement | undefined),
    size: SIZES.find((s) => classes.has(`img-${s.value}`))?.value,
  };
}

/** A 16×16 drawing of a picture (filled) among lines of text. */
function pictogram(picture: string, lines: [number, number, number][]): string {
  const strokes = lines.map(([from, to, y]) => `<path d="M${from} ${y}H${to}"/>`).join("");
  return (
    '<svg viewBox="0 0 16 16" aria-hidden="true">' +
    `<g fill="currentColor">${picture}</g>` +
    `<g stroke="currentColor" stroke-width="1.4" stroke-linecap="round" opacity=".45">${strokes}</g></svg>`
  );
}
