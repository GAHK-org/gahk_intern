// The Hjemmeside editor's pages (templates/hjemmeside/), loaded by hjemmeside-loader.ts on those
// pages only. Everything here enhances plain forms that work without it:
//
//   * "Forhåndsvis" shows the preview in a dialog instead of a new tab;
//   * the image pickers (radio buttons with thumbnails) keep their summary line in step;
//   * the image library takes files dropped on its upload form;
//   * each body textarea becomes the visual editor (hjemmeside-editor.ts, Tiptap), with an image
//     picker that lists and uploads to the library.
//
// Imports nothing from the main bundle (see hjemmeside-loader.ts).
import type * as EditorModule from "./hjemmeside-editor";

export interface HjemmesideDeps {
  csrfToken: () => string;
  loadEditor: () => Promise<typeof EditorModule>;
}

export interface LibraryImage {
  id: number;
  url: string;
  caption: string;
  name: string;
}

export type Placement = "inline" | "left" | "right";

export interface PickedImage {
  url: string;
  alt: string;
  placement: Placement;
}

export function start(root: HTMLElement, deps: HjemmesideDeps): void {
  root.querySelectorAll<HTMLButtonElement>("[data-hj-preview]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      void showPreview(button, root.dataset.preview ?? "", deps);
    });
  });
  root.querySelectorAll<HTMLElement>("[data-hj-image-picker]").forEach(hookImagePicker);
  root.querySelectorAll<HTMLFormElement>("[data-hj-dropzone]").forEach(hookDropzone);
  root.querySelectorAll<HTMLElement>("[data-hj-address-preview]").forEach(hookAddressPreview);

  const fields = root.querySelectorAll<HTMLTextAreaElement>("textarea[data-hjemmeside-editor]");
  if (fields.length) {
    const picker = new LibraryPicker(root.dataset.imageLibrary ?? "", root.dataset.imageUpload ?? "", deps);
    void deps.loadEditor().then((editor) => {
      fields.forEach((field) => editor.mountEditor(field, { pickImage: () => picker.pick() }));
    });
  }
}

// ------------------------------------------------------------------------------------------ dialog
interface Dialog {
  dialog: HTMLDialogElement;
  head: HTMLElement;
  body: HTMLElement;
}

function makeDialog(className: string, title: string): Dialog {
  const dialog = document.createElement("dialog");
  dialog.className = `hj-dialog ${className}`;
  const head = document.createElement("div");
  head.className = "hj-dialog-head";
  const heading = document.createElement("h2");
  heading.textContent = title;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "btn btn-secondary";
  close.textContent = "Luk";
  close.addEventListener("click", () => dialog.close());
  head.append(heading, close);
  const body = document.createElement("div");
  body.className = "hj-dialog-body";
  dialog.append(head, body);
  // A click on the backdrop (the dialog element itself, outside its content) closes it.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  document.body.append(dialog);
  return { dialog, head, body };
}

function statusLine(text: string, warn = false): HTMLElement {
  const p = document.createElement("p");
  p.className = warn ? "hj-note hj-note-warn" : "hj-note";
  p.textContent = text;
  return p;
}

// ----------------------------------------------------------------------------------------- preview
let previewDialog: Dialog | null = null;

function previewParts(): Dialog {
  if (previewDialog) return previewDialog;
  const parts = makeDialog("hj-preview-dialog", "Forhåndsvisning");
  const sizes = document.createElement("div");
  sizes.className = "hj-preview-sizes";
  for (const [label, width] of [
    ["Computer", ""],
    ["Mobil", "390px"],
  ] as const) {
    const size = document.createElement("button");
    size.type = "button";
    size.className = "btn btn-secondary";
    size.textContent = label;
    size.addEventListener("click", () => {
      const frame = parts.body.querySelector("iframe");
      if (frame) frame.style.maxWidth = width;
    });
    sizes.append(size);
  }
  parts.head.insertBefore(sizes, parts.head.lastChild);
  previewDialog = parts;
  return parts;
}

async function showPreview(button: HTMLButtonElement, url: string, deps: HjemmesideDeps): Promise<void> {
  const form = button.form;
  if (!form || !url) return;
  form.dispatchEvent(new CustomEvent("hj:sync")); // the visual editor writes its HTML to the textarea
  const { dialog, body } = previewParts();
  body.replaceChildren(statusLine("Henter forhåndsvisning …"));
  if (!dialog.open) dialog.showModal();
  try {
    const response = await fetch(url, {
      method: "POST",
      body: new FormData(form),
      headers: { "X-CSRFToken": deps.csrfToken() },
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const html = await response.text();
    const frame = document.createElement("iframe");
    // No allow-scripts: the preview is gahk.dk's HTML on intern's origin and must never run here.
    // allow-same-origin so the images a draft uses load from /media/cms/ with the editor's session.
    frame.setAttribute("sandbox", "allow-same-origin allow-popups allow-popups-to-escape-sandbox");
    frame.title = "Forhåndsvisning";
    frame.className = "hj-preview-frame";
    frame.srcdoc = html;
    const parts: HTMLElement[] = [];
    if (response.headers.get("X-Hjemmeside-Preview") === "gahk.dk") {
      parts.push(statusLine("Sådan ser det ud på gahk.dk. Intet er gemt endnu."));
    }
    parts.push(frame);
    body.replaceChildren(...parts);
  } catch {
    body.replaceChildren(statusLine("Forhåndsvisningen kunne ikke hentes. Prøv igen om lidt.", true));
  }
}

// ------------------------------------------------------------------------- radio image pickers
function hookImagePicker(picker: HTMLElement): void {
  const picked = picker.querySelector<HTMLElement>("[data-hj-picked]");
  const label = picker.querySelector<HTMLElement>("[data-hj-picked-label]");
  if (!picked || !label) return;
  picker.addEventListener("change", (event) => {
    const input = event.target as HTMLInputElement;
    if (input.type !== "radio") return;
    const option = input.closest("label");
    const image = option?.querySelector("img");
    const old = picked.querySelector("img, .hj-thumb-none");
    const replacement = image ? (image.cloneNode() as HTMLElement) : document.createElement("span");
    if (!image) replacement.className = "hj-thumb-none";
    old?.replaceWith(replacement);
    label.textContent = option?.querySelector(".hj-thumb-label")?.textContent ?? "";
    picker.querySelector("details")?.removeAttribute("open");
  });
}

// ------------------------------------------------------------------------------- page address
/** The address the section and last-part fields add up to, as gahk.dk will show it. Only a hint:
 * the server normalises and checks it (cms.forms.PageForm). */
function hookAddressPreview(preview: HTMLElement): void {
  const form = preview.closest("form");
  const parent = form?.querySelector<HTMLSelectElement>('select[name="path_parent"]');
  const segment = form?.querySelector<HTMLInputElement>('input[name="path_segment"]');
  if (!parent || !segment) return;
  const update = (): void => {
    const last = segment.value.trim().toLowerCase().replace(/\s+/g, "-").replace(/^\/+|\/+$/g, "");
    if (!last) {
      preview.innerHTML = '<span class="muted">Ingen adresse – siden vises ikke på gahk.dk</span>';
      return;
    }
    const bold = document.createElement("b");
    bold.textContent = parent.value ? `${parent.value}/${last}` : last;
    preview.replaceChildren("gahk.dk/", bold);
  };
  parent.addEventListener("change", update);
  segment.addEventListener("input", update);
  update();
}

// ------------------------------------------------------------------------------ dropped uploads
function hookDropzone(form: HTMLFormElement): void {
  const input = form.querySelector<HTMLInputElement>('input[type="file"]');
  if (!input) return;
  const over = (event: DragEvent): void => {
    if (!event.dataTransfer?.types.includes("Files")) return;
    event.preventDefault();
    form.classList.add("is-dragging");
  };
  form.addEventListener("dragenter", over);
  form.addEventListener("dragover", over);
  form.addEventListener("dragleave", (event) => {
    if (!form.contains(event.relatedTarget as Node | null)) form.classList.remove("is-dragging");
  });
  form.addEventListener("drop", (event) => {
    if (!event.dataTransfer?.files.length) return;
    event.preventDefault();
    form.classList.remove("is-dragging");
    input.files = event.dataTransfer.files;
    form.requestSubmit();
  });
}

// ------------------------------------------------------------------------ the editor's library
class LibraryPicker {
  private parts: Dialog | null = null;
  private resolve: ((picked: PickedImage | null) => void) | null = null;
  private selected: LibraryImage | null = null;
  private images: LibraryImage[] = [];
  private grid = document.createElement("div");
  private search = document.createElement("input");
  private alt = document.createElement("input");
  private insert = document.createElement("button");
  private message = document.createElement("p");
  private placement = document.createElement("fieldset");

  constructor(
    private listUrl: string,
    private uploadUrl: string,
    private deps: HjemmesideDeps,
  ) {}

  pick(): Promise<PickedImage | null> {
    const parts = this.parts ?? this.build();
    this.selected = null;
    this.alt.value = "";
    this.placement.querySelector<HTMLInputElement>('input[value="inline"]')!.checked = true;
    this.updateSelection();
    void this.load();
    if (!parts.dialog.open) parts.dialog.showModal();
    return new Promise((resolve) => {
      this.resolve = resolve;
    });
  }

  private build(): Dialog {
    const parts = makeDialog("hj-picker-dialog", "Indsæt billede");
    this.parts = parts;
    parts.dialog.addEventListener("close", () => this.finish(null));

    const tools = document.createElement("div");
    tools.className = "hj-picker-tools";
    this.search.type = "search";
    this.search.placeholder = "Søg i billederne";
    this.search.setAttribute("aria-label", "Søg i billederne");
    this.search.addEventListener("input", () => this.render());
    const upload = document.createElement("label");
    upload.className = "btn btn-secondary hj-picker-upload";
    upload.textContent = "Læg nyt billede op";
    const file = document.createElement("input");
    file.type = "file";
    file.accept = "image/jpeg,image/png,image/gif,image/webp,image/heic,image/heif";
    file.className = "sr-only";
    file.addEventListener("change", () => {
      const chosen = file.files?.[0];
      file.value = "";
      if (chosen) void this.upload(chosen);
    });
    upload.append(file);
    tools.append(this.search, upload);

    this.message.hidden = true;
    this.grid.className = "hj-picker-grid";

    const foot = document.createElement("div");
    foot.className = "hj-picker-foot";
    const altLabel = document.createElement("label");
    altLabel.className = "hj-picker-alt";
    altLabel.textContent = "Alt-tekst: hvad viser billedet?";
    this.alt.type = "text";
    this.alt.maxLength = 255;
    altLabel.append(this.alt);
    this.placement.className = "hj-placement";
    const legend = document.createElement("legend");
    legend.textContent = "Placering";
    this.placement.append(legend);
    for (const [value, text] of [
      ["inline", "I teksten"],
      ["left", "Til venstre"],
      ["right", "Til højre"],
    ] as const) {
      const option = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "hj-placement";
      radio.value = value;
      option.append(radio, ` ${text}`);
      this.placement.append(option);
    }
    this.insert.type = "button";
    this.insert.className = "btn";
    this.insert.textContent = "Indsæt";
    this.insert.addEventListener("click", () => {
      if (!this.selected) return;
      const checked = this.placement.querySelector<HTMLInputElement>("input:checked");
      this.finish({
        url: this.selected.url,
        alt: this.alt.value.trim(),
        placement: (checked?.value ?? "inline") as Placement,
      });
      parts.dialog.close();
    });
    foot.append(altLabel, this.placement, this.insert);
    parts.body.append(tools, this.message, this.grid, foot);
    return parts;
  }

  private finish(picked: PickedImage | null): void {
    const resolve = this.resolve;
    this.resolve = null;
    resolve?.(picked);
  }

  private say(text: string, warn = false): void {
    this.message.textContent = text;
    this.message.className = warn ? "hj-note hj-note-warn" : "hj-note";
    this.message.hidden = !text;
  }

  private async load(): Promise<void> {
    this.say("Henter billeder …");
    try {
      const response = await fetch(this.listUrl, { credentials: "same-origin" });
      if (!response.ok) throw new Error(String(response.status));
      this.images = ((await response.json()) as { images: LibraryImage[] }).images;
      this.say("");
      this.render();
    } catch {
      this.say("Billederne kunne ikke hentes.", true);
    }
  }

  private async upload(file: File): Promise<void> {
    this.say(`Lægger ${file.name} op …`);
    const body = new FormData();
    body.append("file", file);
    body.append("caption", this.alt.value.trim());
    try {
      const response = await fetch(this.uploadUrl, {
        method: "POST",
        body,
        headers: { "X-CSRFToken": this.deps.csrfToken() },
        credentials: "same-origin",
      });
      const result = (await response.json().catch(() => ({}))) as { image?: LibraryImage; error?: string };
      if (!response.ok || !result.image) throw new Error(result.error ?? "Billedet kunne ikke lægges op.");
      this.images.unshift(result.image);
      this.say("");
      this.search.value = "";
      this.choose(result.image);
      this.render();
    } catch (error) {
      this.say(error instanceof Error ? error.message : "Billedet kunne ikke lægges op.", true);
    }
  }

  private choose(image: LibraryImage): void {
    this.selected = image;
    if (!this.alt.value) this.alt.value = image.caption;
    this.updateSelection();
  }

  private updateSelection(): void {
    this.insert.disabled = !this.selected;
    this.grid.querySelectorAll<HTMLButtonElement>("button").forEach((button) => {
      button.setAttribute("aria-pressed", String(Number(button.dataset.id) === this.selected?.id));
    });
  }

  private render(): void {
    const query = this.search.value.trim().toLowerCase();
    const shown = this.images.filter(
      (image) => !query || image.caption.toLowerCase().includes(query) || image.url.toLowerCase().includes(query),
    );
    this.grid.replaceChildren(
      ...shown.map((image) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "hj-picker-item";
        button.dataset.id = String(image.id);
        button.title = image.caption || image.name;
        const img = document.createElement("img");
        img.src = image.url;
        img.alt = "";
        img.loading = "lazy";
        const caption = document.createElement("span");
        caption.textContent = image.caption || "(ingen beskrivelse)";
        button.append(img, caption);
        button.addEventListener("click", () => {
          this.alt.value = "";
          this.choose(image);
        });
        button.addEventListener("dblclick", () => this.insert.click());
        return button;
      }),
    );
    if (!shown.length) this.grid.append(statusLine(query ? "Ingen billeder matcher." : "Biblioteket er tomt."));
    this.updateSelection();
  }
}
