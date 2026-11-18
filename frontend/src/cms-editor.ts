// The visual body editor for gahk.dk's pages, news and events (Tiptap), loaded only where a
// `textarea[data-cms-editor]` is (cms-loader.ts).
//
// The textarea stays the form field: the editor writes its HTML into it after each change, and the
// "HTML" button shows it for editing by hand. Without JavaScript it is simply a textarea of HTML. The
// server sanitizes whatever arrives (cms.sanitize), so nothing here is a security boundary.
//
// Two things matter for the pages that already exist, which came from the old site's editor:
//
//   * Images keep their size and float. Those pages place pictures with inline styles
//     (`style="width: 420px; height: 280px; float: right"`), which Tiptap's own image node would
//     drop; the node here keeps width, height, style (layout properties only), class and title.
//   * The textarea is written only when the editor changes something, so opening a page and saving
//     it untouched sends back exactly what was stored. And a body with structure the editor cannot
//     hold (tables and the like) opens as HTML, saying why, instead of losing it on the first keystroke.
import { Editor, type Attributes } from "@tiptap/core";
import Image from "@tiptap/extension-image";
import Link from "@tiptap/extension-link";
import StarterKit from "@tiptap/starter-kit";

import type { Placement, PickedImage } from "./cms";

export interface EditorOptions {
  pickImage: () => Promise<PickedImage | null>;
}

// CSS properties an image may keep: its size and how it sits in the text. Everything else in an old
// style attribute is leftover from copy-pasting (colours, fonts) and means nothing on an image.
const IMAGE_STYLE_PROPERTIES = /^(float|width|height|max-width|margin(-(top|right|bottom|left))?|display|vertical-align)$/;

function styleMap(style: string | null): Map<string, string> {
  const map = new Map<string, string>();
  for (const declaration of (style ?? "").split(";")) {
    const colon = declaration.indexOf(":");
    if (colon < 0) continue;
    const property = declaration.slice(0, colon).trim().toLowerCase();
    const value = declaration.slice(colon + 1).trim();
    if (property && value) map.set(property, value);
  }
  return map;
}

function styleString(map: Map<string, string>): string | null {
  const text = Array.from(map, ([property, value]) => `${property}: ${value};`).join(" ");
  return text || null;
}

export function cleanImageStyle(style: string | null): string | null {
  const kept = new Map(Array.from(styleMap(style)).filter(([property]) => IMAGE_STYLE_PROPERTIES.test(property)));
  return styleString(kept);
}

const PLACEMENT_STYLES: Record<Placement, [string, string][]> = {
  inline: [],
  left: [
    ["float", "left"],
    ["margin", "0 1em 1em 0"],
    ["max-width", "50%"],
  ],
  right: [
    ["float", "right"],
    ["margin", "0 0 1em 1em"],
    ["max-width", "50%"],
  ],
};

/** `style` with its float/margin replaced by `placement`'s. A fixed height goes too, so a picture
 * narrowed by max-width keeps its proportions. */
export function placeStyle(style: string | null, placement: Placement): string | null {
  const map = styleMap(style);
  for (const property of Array.from(map.keys())) {
    if (/^(float|margin|max-width|height)/.test(property)) map.delete(property);
  }
  for (const [property, value] of PLACEMENT_STYLES[placement]) map.set(property, value);
  return styleString(map);
}

function placementOf(style: string | null): Placement {
  const float = styleMap(style).get("float");
  return float === "left" || float === "right" ? float : "inline";
}

function keep(name: string, clean: (value: string | null) => string | null = (value) => value): Attributes[string] {
  return {
    default: null,
    parseHTML: (element: HTMLElement) => clean(element.getAttribute(name)),
    renderHTML: (attributes: Record<string, unknown>) => {
      const value = attributes[name];
      return value === null || value === undefined || value === "" ? {} : { [name]: String(value) };
    },
  };
}

// Inline, so a picture inside a paragraph stays there (the old pages float pictures beside the text
// of the paragraph they are in).
const PageImage = Image.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      width: keep("width"),
      height: keep("height"),
      title: keep("title"),
      class: keep("class"),
      style: keep("style", cleanImageStyle),
    };
  },
}).configure({ inline: true, allowBase64: false });

// Elements the editor has no place for. A body holding any of them opens as HTML.
const UNSUPPORTED = "table, figure, dl, iframe, video, audio, object, embed, sub, sup, mark, small, abbr, cite, form";
const UNSUPPORTED_NAMES: Record<string, string> = {
  TABLE: "tabeller",
  FIGURE: "figurer",
  DL: "definitionslister",
  IFRAME: "indlejret indhold",
  VIDEO: "video",
  AUDIO: "lyd",
};

export function unsupportedContent(html: string): string[] {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const found = new Set<string>();
  doc.body.querySelectorAll(UNSUPPORTED).forEach((element) => {
    found.add(UNSUPPORTED_NAMES[element.tagName] ?? `<${element.tagName.toLowerCase()}>`);
  });
  return Array.from(found);
}

/** Pasted HTML without what Word, Google Docs and web pages bring along: styles, classes, ids,
 * fonts, comments. What is left is structure (headings, lists, links, bold) the editor keeps. */
export function cleanPastedHtml(html: string): string {
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.querySelectorAll("style, meta, link, script, title, xml").forEach((node) => node.remove());
  doc.body.querySelectorAll("*").forEach((element) => {
    for (const name of ["style", "class", "id", "lang", "dir", "width", "height", "align", "color", "face", "size"]) {
      element.removeAttribute(name);
    }
  });
  return doc.body.innerHTML;
}

const UNDO_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>';
const REDO_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 14 5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/></svg>';

interface ToolButton {
  label: string;
  icon?: string;
  title: string;
  run: (editor: Editor) => void;
  active?: (editor: Editor) => boolean;
  imageOnly?: boolean;
}

export function mountEditor(textarea: HTMLTextAreaElement, options: EditorOptions): void {
  const wrapper = document.createElement("div");
  wrapper.className = "cms-editor";
  const toolbar = document.createElement("div");
  toolbar.className = "cms-toolbar";
  toolbar.setAttribute("role", "toolbar");
  toolbar.setAttribute("aria-label", "Formatering");
  const surface = document.createElement("div");
  surface.className = "cms-editor-surface";
  // Shown under the toolbar while a picture is selected: where it sits, and its alt text.
  const imageBar = document.createElement("div");
  imageBar.className = "cms-image-bar";
  imageBar.hidden = true;
  const imageBarLabel = document.createElement("span");
  imageBarLabel.className = "cms-image-bar-label";
  imageBarLabel.textContent = "Billedet:";
  imageBar.append(imageBarLabel);
  const notice = document.createElement("p");
  notice.className = "cms-note cms-note-warn";
  notice.hidden = true;
  textarea.before(wrapper);
  wrapper.append(toolbar, imageBar, notice, surface, textarea);
  textarea.classList.add("cms-source");

  const label = textarea.id ? document.querySelector(`label[for="${textarea.id}"]`)?.textContent?.trim() : "";
  const lossy = unsupportedContent(textarea.value);
  let changed = false;

  const editor = new Editor({
    element: surface,
    content: textarea.value,
    extensions: [
      StarterKit.configure({ link: false }),
      Link.configure({
        openOnClick: false,
        autolink: true,
        defaultProtocol: "https",
        HTMLAttributes: { target: null, rel: null },
      }),
      PageImage,
    ],
    editorProps: {
      attributes: {
        class: "prose-gahk cms-editor-content",
        "aria-label": label || "Tekst",
        "aria-multiline": "true",
        role: "textbox",
      },
      transformPastedHTML: cleanPastedHtml,
    },
    onUpdate: ({ editor: current }) => {
      changed = true;
      textarea.value = serialize(current);
    },
  });

  // ------------------------------------------------------------------------------------ modes
  let sourceMode = false;
  const sourceButton = document.createElement("button");
  sourceButton.type = "button";
  sourceButton.className = "cms-tool cms-tool-source";
  sourceButton.textContent = "HTML";
  sourceButton.title = "Vis og ret teksten som HTML";

  function setSourceMode(on: boolean): void {
    sourceMode = on;
    wrapper.classList.toggle("is-source", on);
    sourceButton.setAttribute("aria-pressed", String(on));
    toolbar.querySelectorAll<HTMLButtonElement>("button:not(.cms-tool-source)").forEach((button) => {
      button.disabled = on;
    });
    if (on) {
      imageBar.hidden = true;
      textarea.focus();
    } else {
      updateButtons();
    }
  }

  sourceButton.addEventListener("click", () => {
    if (!sourceMode) {
      setSourceMode(true);
      return;
    }
    const remaining = unsupportedContent(textarea.value);
    if (
      remaining.length &&
      !window.confirm(
        `Teksten indeholder ${remaining.join(", ")}, som den visuelle editor ikke kan vise. ` +
          "Skifter du, forsvinder det, når du retter i teksten. Skift alligevel?",
      )
    ) {
      return;
    }
    // What was typed as HTML is what the form sends until the editor changes it again.
    editor.commands.setContent(textarea.value, { emitUpdate: false });
    notice.hidden = true;
    setSourceMode(false);
    editor.commands.focus();
  });

  // ----------------------------------------------------------------------------------- tools
  const tools: (ToolButton | "|")[] = [
    {
      label: "H2",
      title: "Overskrift",
      run: (e) => e.chain().focus().toggleHeading({ level: 2 }).run(),
      active: (e) => e.isActive("heading", { level: 2 }),
    },
    {
      label: "H3",
      title: "Mellemrubrik",
      run: (e) => e.chain().focus().toggleHeading({ level: 3 }).run(),
      active: (e) => e.isActive("heading", { level: 3 }),
    },
    "|",
    { label: "F", title: "Fed (Ctrl+B)", run: (e) => e.chain().focus().toggleBold().run(), active: (e) => e.isActive("bold") },
    {
      label: "K",
      title: "Kursiv (Ctrl+I)",
      run: (e) => e.chain().focus().toggleItalic().run(),
      active: (e) => e.isActive("italic"),
    },
    "|",
    {
      label: "• Liste",
      title: "Punktliste",
      run: (e) => e.chain().focus().toggleBulletList().run(),
      active: (e) => e.isActive("bulletList"),
    },
    {
      label: "1. Liste",
      title: "Nummereret liste",
      run: (e) => e.chain().focus().toggleOrderedList().run(),
      active: (e) => e.isActive("orderedList"),
    },
    {
      label: "Citat",
      title: "Citat",
      run: (e) => e.chain().focus().toggleBlockquote().run(),
      active: (e) => e.isActive("blockquote"),
    },
    "|",
    { label: "Link", title: "Indsæt eller ret et link", run: editLink, active: (e) => e.isActive("link") },
    {
      label: "Billede",
      title: "Indsæt et billede fra billedbiblioteket",
      run: (e) => void insertImage(e, options),
    },
    "|",
    { label: "Fortryd", icon: UNDO_ICON, title: "Fortryd (Ctrl+Z)", run: (e) => e.chain().focus().undo().run() },
    { label: "Gentag", icon: REDO_ICON, title: "Gentag (Ctrl+Shift+Z)", run: (e) => e.chain().focus().redo().run() },
    { label: "Til venstre", title: "Billedet til venstre for teksten", run: (e) => placeImage(e, "left"), imageOnly: true, active: (e) => isPlaced(e, "left") },
    { label: "I teksten", title: "Billedet i teksten", run: (e) => placeImage(e, "inline"), imageOnly: true, active: (e) => isPlaced(e, "inline") },
    { label: "Til højre", title: "Billedet til højre for teksten", run: (e) => placeImage(e, "right"), imageOnly: true, active: (e) => isPlaced(e, "right") },
    { label: "Alt-tekst", title: "Beskriv billedet for dem, der ikke kan se det", run: editAlt, imageOnly: true },
  ];

  const buttons: [HTMLButtonElement, ToolButton][] = [];
  for (const tool of tools) {
    if (tool === "|") {
      const separator = document.createElement("span");
      separator.className = "cms-tool-sep";
      separator.setAttribute("aria-hidden", "true");
      toolbar.append(separator);
      continue;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cms-tool";
    if (tool.icon) {
      button.innerHTML = tool.icon;
    } else {
      button.textContent = tool.label;
    }
    button.title = tool.title;
    button.setAttribute("aria-label", tool.title);
    // mousedown would move the focus (and the selection) out of the editor before the click.
    button.addEventListener("mousedown", (event) => event.preventDefault());
    button.addEventListener("click", () => tool.run(editor));
    (tool.imageOnly ? imageBar : toolbar).append(button);
    buttons.push([button, tool]);
  }
  toolbar.append(sourceButton);

  function updateButtons(): void {
    imageBar.hidden = sourceMode || !editor.isActive("image");
    for (const [button, tool] of buttons) {
      if (tool.active) button.setAttribute("aria-pressed", String(tool.active(editor)));
    }
  }
  editor.on("selectionUpdate", updateButtons);
  editor.on("transaction", updateButtons);
  updateButtons();

  // The preview reads the form; make sure it reads what is on screen.
  textarea.form?.addEventListener("cms:sync", () => {
    if (!sourceMode && changed) textarea.value = serialize(editor);
  });

  if (lossy.length) {
    notice.textContent =
      `Teksten indeholder ${lossy.join(", ")}, som den visuelle editor ikke kan vise, så den vises som HTML. ` +
      "Ret den her, eller tryk HTML for at skifte til den visuelle editor.";
    notice.hidden = false;
    setSourceMode(true);
    textarea.blur();
  }
}

/** The editor's HTML for the form, without the empty paragraph Tiptap keeps at the end so there is
 * always somewhere to type after a quote or a picture. */
function serialize(editor: Editor): string {
  return editor.isEmpty ? "" : editor.getHTML().replace(/(<p><\/p>)+$/, "");
}

function editLink(editor: Editor): void {
  const current = (editor.getAttributes("link").href as string | undefined) ?? "";
  const answer = window.prompt("Link (fx /vision, https://… eller mailto:…). Tomt fjerner linket.", current);
  if (answer === null) return;
  const href = answer.trim();
  const chain = editor.chain().focus().extendMarkRange("link");
  if (!href) {
    chain.unsetLink().run();
    return;
  }
  if (editor.state.selection.empty && !editor.isActive("link")) {
    chain.insertContent({ type: "text", text: href, marks: [{ type: "link", attrs: { href } }] }).run();
    return;
  }
  chain.setLink({ href }).run();
}

async function insertImage(editor: Editor, options: EditorOptions): Promise<void> {
  const picked = await options.pickImage();
  if (!picked) {
    editor.commands.focus();
    return;
  }
  editor
    .chain()
    .focus()
    .insertContent({
      type: "image",
      attrs: { src: picked.url, alt: picked.alt, style: placeStyle(null, picked.placement) },
    })
    .run();
}

function placeImage(editor: Editor, placement: Placement): void {
  const style = (editor.getAttributes("image").style as string | null) ?? null;
  editor.chain().focus().updateAttributes("image", { style: placeStyle(style, placement), height: null }).run();
}

function isPlaced(editor: Editor, placement: Placement): boolean {
  return editor.isActive("image") && placementOf((editor.getAttributes("image").style as string | null) ?? null) === placement;
}

function editAlt(editor: Editor): void {
  const current = (editor.getAttributes("image").alt as string | undefined) ?? "";
  const answer = window.prompt("Alt-tekst: hvad viser billedet?", current);
  if (answer === null) return;
  editor.chain().focus().updateAttributes("image", { alt: answer.trim() }).run();
}
