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
//     A picture is laid out with the picture bar (placement and size, cms-image-layout.ts), which
//     replaces such inline sizes with classes the moment somebody uses it.
//   * The textarea is written only when the editor changes something, so opening a page and saving
//     it untouched sends back exactly what was stored. And a body with structure the editor cannot
//     hold (tables and the like) opens as HTML, saying why, instead of losing it on the first keystroke.
import { Editor, type Attributes } from "@tiptap/core";
import Image from "@tiptap/extension-image";
import Link from "@tiptap/extension-link";
import StarterKit from "@tiptap/starter-kit";

import type { PickedImage } from "./cms";
import {
  DEFAULT_LAYOUT,
  PLACEMENTS,
  SIZES,
  fitLayout,
  layoutAttributes,
  layoutOf,
  type ImageAttributes,
  type ImageLayout,
} from "./cms-image-layout";

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
  /** The icon with the label beside it, rather than the icon alone. */
  labelled?: boolean;
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
  // Shown under the toolbar while a picture is selected: where it sits, how big, and its alt text.
  const imageBar = document.createElement("div");
  imageBar.className = "cms-image-bar";
  imageBar.setAttribute("role", "toolbar");
  imageBar.setAttribute("aria-label", "Billedet");
  imageBar.hidden = true;
  const notice = document.createElement("p");
  notice.className = "cms-note cms-note-warn";
  notice.hidden = true;
  // One sticky header for both bars, so the picture bar is in view wherever the picture is.
  const bars = document.createElement("div");
  bars.className = "cms-editor-bars";
  bars.append(toolbar, imageBar);
  textarea.before(wrapper);
  wrapper.append(bars, notice, surface, textarea);
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
  ];

  const buttons: [HTMLButtonElement, ToolButton][] = [];
  const makeButton = (tool: ToolButton, into: HTMLElement): void => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cms-tool";
    if (tool.icon) {
      button.innerHTML = tool.icon;
      if (tool.labelled) button.append(tool.label);
    } else {
      button.textContent = tool.label;
    }
    button.title = tool.title;
    button.setAttribute("aria-label", tool.labelled ? tool.label : tool.title);
    // mousedown would move the focus (and the selection) out of the editor before the click.
    button.addEventListener("mousedown", (event) => event.preventDefault());
    button.addEventListener("click", () => tool.run(editor));
    into.append(button);
    buttons.push([button, tool]);
  };
  for (const tool of tools) {
    if (tool === "|") {
      const separator = document.createElement("span");
      separator.className = "cms-tool-sep";
      separator.setAttribute("aria-hidden", "true");
      toolbar.append(separator);
    } else {
      makeButton(tool, toolbar);
    }
  }
  toolbar.append(sourceButton);

  // ------------------------------------------------------------------------- the picture bar
  const imageGroup = (label: string): HTMLElement => {
    const group = document.createElement("span");
    group.className = "cms-image-group";
    if (label) {
      group.setAttribute("role", "group");
      group.setAttribute("aria-label", label);
      const text = document.createElement("span");
      text.className = "cms-image-bar-label";
      text.setAttribute("aria-hidden", "true");
      text.textContent = label;
      group.append(text);
    }
    imageBar.append(group);
    return group;
  };
  const placementGroup = imageGroup("Placering");
  for (const placement of PLACEMENTS) {
    makeButton(
      {
        label: placement.label,
        icon: placement.icon,
        labelled: true,
        title: placement.title,
        run: (e) => setLayout(e, { placement: placement.value }, "placement"),
        active: (e) => hasLayout(e, "placement", placement.value),
      },
      placementGroup,
    );
  }
  const sizeGroup = imageGroup("Størrelse");
  for (const size of SIZES) {
    makeButton(
      {
        label: size.label,
        title: size.title,
        run: (e) => setLayout(e, { size: size.value }, "size"),
        active: (e) => hasLayout(e, "size", size.value),
      },
      sizeGroup,
    );
  }
  const otherGroup = imageGroup("");
  makeButton({ label: "Alt-tekst", title: "Beskriv billedet for dem, der ikke kan se det", run: editAlt }, otherGroup);
  makeButton(
    { label: "Fjern", title: "Fjern billedet fra teksten", run: (e) => e.chain().focus().deleteSelection().run() },
    otherGroup,
  );

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
  // Selected once it is in, so its picture bar shows at once for adjusting what was picked.
  const at = editor.state.selection.from;
  editor
    .chain()
    .focus()
    .insertContent({
      type: "image",
      attrs: { src: picked.url, alt: picked.alt, ...layoutAttributes({}, picked.layout) },
    })
    .setNodeSelection(at)
    .run();
}

/** Give the selected picture `pick` (a placement or a size), keeping the other half of its layout.
 * An old picture without one gets the default for its side: medium beside the text, else large. */
function setLayout(editor: Editor, pick: Partial<ImageLayout>, changed: "placement" | "size"): void {
  const attributes = editor.getAttributes("image") as ImageAttributes;
  const now = layoutOf(attributes);
  const placement = now.placement ?? DEFAULT_LAYOUT.placement;
  const size = now.size ?? (placement === "center" ? DEFAULT_LAYOUT.size : "medium");
  const layout = fitLayout({ placement, size, ...pick }, changed);
  updatePicture(editor, layoutAttributes(attributes, layout));
}

/** Change the selected picture and keep it selected (and its bar shown): updating an inline node's
 * attributes would otherwise leave the cursor beside it. */
function updatePicture(editor: Editor, attributes: Record<string, unknown>): void {
  const at = editor.state.selection.from;
  editor.chain().focus().updateAttributes("image", attributes).setNodeSelection(at).run();
}

function hasLayout<K extends keyof ImageLayout>(editor: Editor, key: K, value: ImageLayout[K]): boolean {
  return editor.isActive("image") && layoutOf(editor.getAttributes("image") as ImageAttributes)[key] === value;
}

function editAlt(editor: Editor): void {
  const current = (editor.getAttributes("image").alt as string | undefined) ?? "";
  const answer = window.prompt("Alt-tekst: hvad viser billedet?", current);
  if (answer === null) return;
  updatePicture(editor, { alt: answer.trim() });
}
