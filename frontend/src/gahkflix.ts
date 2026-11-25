/**
 * GAHKFlix (templates/gahkflix/): the rows, the uploader, the editors on the edit page and the player.
 *
 * Every page outside GAHKFlix is a no-op: everything below hangs off `body.flix`.
 *
 * THE UPLOAD GOES STRAIGHT TO THE BUCKET (gahkflix/uploads.py): begin hands back a policy and a
 * signed token, the file is POSTed to the bucket with XHR (fetch has no upload progress), and commit
 * creates the video. Before any of that, a fingerprint of the file — SHA-256 of its size and its
 * first and last 4 MB — lets the server refuse a file it already has without the browser reading
 * all five gigabytes of it.
 *
 * THE PLAYER IS ONE <video>: the intro first, then the film in the same element (see play.html).
 */
import "./gahkflix.css";
import { csrfToken } from "./htmx-csrf";

const body = document.body;

function jsonConfig<T>(id: string): T | null {
  const el = document.getElementById(id);
  if (!el?.textContent) return null;
  try {
    return JSON.parse(el.textContent) as T;
  } catch {
    return null;
  }
}

function postJSON(url: string, data: unknown, keepalive = false): Promise<Response> {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
    body: JSON.stringify(data),
    credentials: "same-origin",
    keepalive,
  });
}

async function errorOf(response: Response, fallback: string): Promise<string> {
  try {
    const data = await response.json();
    return data.error || fallback;
  } catch {
    return fallback;
  }
}

function debounce<A extends unknown[]>(fn: (...args: A) => void, ms: number): (...args: A) => void {
  let timer = 0;
  return (...args: A) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), ms);
  };
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toLocaleString("da-DK", { maximumFractionDigits: 2 })} GB`;
  return `${Math.max(1, Math.round(bytes / 1024 ** 2)).toLocaleString("da-DK")} MB`;
}

function formatSeconds(seconds: number): string {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} sek`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${Math.floor(seconds / 3600)} t ${Math.round((seconds % 3600) / 60)} min`;
}

// --- Header, rows and small page behaviour --------------------------------------------------------

function initHeader(): void {
  const header = document.querySelector<HTMLElement>("[data-flix-header]");
  if (!header) return;
  const solid = () => header.classList.toggle("is-solid", window.scrollY > 10);
  window.addEventListener("scroll", solid, { passive: true });
  solid();
  const menu = header.querySelector<HTMLButtonElement>("[data-flix-menu]");
  menu?.addEventListener("click", () => {
    const open = header.classList.toggle("is-open");
    menu.setAttribute("aria-expanded", String(open));
  });
  const search = header.querySelector<HTMLFormElement>("[data-flix-search]");
  search?.addEventListener("click", () => search.querySelector("input")?.focus());
}

function initRows(): void {
  for (const row of document.querySelectorAll<HTMLElement>("[data-flix-row]")) {
    const slider = row.querySelector<HTMLElement>("[data-flix-slider]");
    const prev = row.querySelector<HTMLButtonElement>("[data-flix-prev]");
    const next = row.querySelector<HTMLButtonElement>("[data-flix-next]");
    if (!slider) continue;
    const update = () => {
      if (prev) prev.hidden = slider.scrollLeft <= 4;
      if (next) next.hidden = slider.scrollLeft + slider.clientWidth >= slider.scrollWidth - 4;
    };
    const page = (direction: number) =>
      slider.scrollBy({ left: direction * slider.clientWidth * 0.85, behavior: "smooth" });
    prev?.addEventListener("click", () => page(-1));
    next?.addEventListener("click", () => page(1));
    slider.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    update();
  }
}

function initSeasons(): void {
  for (const section of document.querySelectorAll<HTMLElement>("[data-flix-seasons]")) {
    const select = section.querySelector<HTMLSelectElement>("[data-flix-season-select]");
    select?.addEventListener("change", () => {
      for (const season of section.querySelectorAll<HTMLElement>("[data-flix-season]")) {
        season.hidden = season.dataset.flixSeason !== select.value;
      }
    });
  }
}

/** The views chart (_stats.html): each day's column is its own hover and focus target. */
function initCharts(): void {
  for (const chart of document.querySelectorAll<HTMLElement>("[data-flix-chart]")) {
    const tooltip = chart.querySelector<HTMLElement>(".flix-tooltip");
    const plot = chart.querySelector<HTMLElement>(".flix-chart-plot");
    if (!tooltip || !plot) continue;
    const show = (bar: HTMLElement) => {
      tooltip.querySelector("b")!.textContent = bar.dataset.value ?? "";
      tooltip.querySelector("span")!.textContent = bar.dataset.label ?? "";
      tooltip.hidden = false;
      const barBox = bar.getBoundingClientRect();
      const plotBox = plot.getBoundingClientRect();
      const half = tooltip.offsetWidth / 2;
      const center = barBox.left - plotBox.left + barBox.width / 2;
      tooltip.style.left = `${Math.min(Math.max(center, half), plotBox.width - half)}px`;
    };
    const hide = () => (tooltip.hidden = true);
    for (const bar of chart.querySelectorAll<HTMLElement>(".flix-bar")) {
      bar.addEventListener("pointerenter", () => show(bar));
      bar.addEventListener("focus", () => show(bar));
      bar.addEventListener("pointerleave", hide);
      bar.addEventListener("blur", hide);
    }
  }
}

function initForms(): void {
  for (const form of document.querySelectorAll<HTMLFormElement>("form[data-flix-confirm]")) {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.flixConfirm ?? "Er du sikker?")) event.preventDefault();
    });
  }
  for (const input of document.querySelectorAll<HTMLInputElement>("input[data-flix-autosubmit]")) {
    input.addEventListener("change", () => {
      if (input.files?.length) input.form?.requestSubmit();
    });
  }
}

function initLiveSearch(): void {
  const form = document.querySelector<HTMLFormElement>("form[data-flix-live-search]");
  const results = document.querySelector<HTMLElement>("[data-flix-search-results]");
  const input = form?.querySelector<HTMLInputElement>("input[name=q]");
  if (!form || !results || !input) return;
  let latest = 0;
  const run = debounce(async () => {
    const query = input.value.trim();
    const ticket = ++latest;
    const url = `${form.dataset.flixLiveSearch}?${new URLSearchParams({ q: query, partial: "1" })}`;
    const response = await fetch(url, { credentials: "same-origin" });
    if (!response.ok || ticket !== latest) return;
    // Server-rendered from our own template, every name already escaped by Django.
    results.innerHTML = await response.text();
    history.replaceState(null, "", query ? `?${new URLSearchParams({ q: query })}` : location.pathname);
  }, 250);
  input.addEventListener("input", run);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    run();
  });
}

// --- Uploading ------------------------------------------------------------------------------------

type UploadConfig = {
  beginUrl: string;
  commitUrl: string;
  video: number | null;
  maxBytes: number;
  maxFiles: number;
  maxDrafts: number;
};
type UploadPlan = { mode: "s3" | "direct"; url: string; fields?: Record<string, string> };

const FINGERPRINT_PART = 4 * 1024 * 1024;

async function fingerprint(file: File): Promise<string> {
  const size = new DataView(new ArrayBuffer(8));
  size.setBigUint64(0, BigInt(file.size), true);
  const parts: BlobPart[] = [size.buffer];
  if (file.size <= 2 * FINGERPRINT_PART) parts.push(file);
  else parts.push(file.slice(0, FINGERPRINT_PART), file.slice(file.size - FINGERPRINT_PART));
  const digest = await crypto.subtle.digest("SHA-256", await new Blob(parts).arrayBuffer());
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

function sendWithProgress(
  url: string,
  form: FormData,
  headers: Record<string, string>,
  onProgress: (loaded: number, total: number) => void,
): Promise<number> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value);
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress(event.loaded, event.total);
    });
    xhr.addEventListener("load", () => resolve(xhr.status));
    xhr.addEventListener("error", () => reject(new Error("Forbindelsen blev afbrudt.")));
    xhr.addEventListener("abort", () => reject(new Error("Uploaden blev afbrudt.")));
    xhr.send(form);
  });
}

/** The upload widget: one file for a new version, or several new videos, sent one after another. */
function initUpload(): void {
  const root = document.querySelector<HTMLElement>("[data-flix-upload]");
  const config = jsonConfig<UploadConfig>("flix-upload-config");
  if (!root || !config) return;
  const drop = root.querySelector<HTMLElement>("[data-flix-drop]")!;
  const input = root.querySelector<HTMLInputElement>("[data-flix-file]")!;
  const queue = root.querySelector<HTMLOListElement>("[data-flix-queue]")!;
  const status = root.querySelector<HTMLElement>("[data-flix-upload-status]")!;
  let room = config.maxFiles;
  let busy = false;
  const roomNote = root.querySelector<HTMLElement>("[data-flix-room]");

  const say = (text: string, error = false) => {
    status.textContent = text;
    status.classList.toggle("is-error", error);
  };
  const guard = (event: BeforeUnloadEvent) => {
    event.preventDefault();
  };

  /** One file's row in the list: its name, size, a meter and what is happening to it. */
  function row(file: File) {
    const item = document.createElement("li");
    item.className = "flix-upload-item";
    const head = document.createElement("p");
    const name = document.createElement("b");
    name.textContent = file.name;
    const size = document.createElement("span");
    size.className = "muted";
    size.textContent = ` ${formatBytes(file.size)}`;
    head.append(name, size);
    const meterBox = document.createElement("div");
    meterBox.className = "flix-meter";
    const meter = document.createElement("span");
    meterBox.append(meter);
    const line = document.createElement("p");
    line.className = "flix-upload-status";
    item.append(head, meterBox, line);
    queue.append(item);
    return {
      progress: (fraction: number) => (meter.style.width = `${(fraction * 100).toFixed(1)}%`),
      say: (text: string, error = false) => {
        line.textContent = text;
        line.classList.toggle("is-error", error);
      },
      done: (editUrl: string) => {
        meterBox.hidden = true;
        line.textContent = "Uploadet. Den behandles nu. ";
        const link = document.createElement("a");
        link.href = editUrl;
        link.textContent = "Skriv titel og oplysninger";
        line.append(link);
      },
      failed: (text: string) => {
        meterBox.hidden = true;
        line.textContent = text;
        line.classList.add("is-error");
      },
    };
  }

  /** Send one file; the address of its edit page, or an Error saying why not. */
  async function send(file: File, show: ReturnType<typeof row>): Promise<string> {
    if (file.size > config!.maxBytes) {
      throw new Error(`Videoen er for stor (${formatBytes(file.size)}; højst ${formatBytes(config!.maxBytes)}).`);
    }
    show.say("Tjekker filen …");
    const begun = await postJSON(config!.beginUrl, {
      name: file.name,
      size: file.size,
      type: file.type,
      fingerprint: await fingerprint(file),
      lastModified: String(file.lastModified || ""),
      video: config!.video,
    });
    if (!begun.ok) throw new Error(await errorOf(begun, "Uploaden kunne ikke starte."));
    const { token, upload: plan } = (await begun.json()) as { token: string; upload: UploadPlan };

    const form = new FormData();
    const headers: Record<string, string> = {};
    if (plan.mode === "s3") {
      for (const [name, value] of Object.entries(plan.fields ?? {})) form.append(name, value);
    } else {
      form.append("token", token);
      headers["X-CSRFToken"] = csrfToken();
    }
    form.append("file", file);
    const started = performance.now();
    const statusCode = await sendWithProgress(plan.url, form, headers, (loaded, total) => {
      const fraction = loaded / total;
      show.progress(fraction);
      const seconds = (performance.now() - started) / 1000;
      const rate = loaded / Math.max(seconds, 0.001);
      const left = rate > 0 ? (total - loaded) / rate : 0;
      show.say(
        `Sender … ${Math.floor(fraction * 100)} % · ${formatBytes(rate)}/s` +
          (fraction < 1 && seconds > 3 ? ` · ca. ${formatSeconds(left)} tilbage` : ""),
      );
    });
    if (statusCode < 200 || statusCode >= 300) throw new Error("Lageret afviste filen. Prøv igen.");

    show.say("Gemmer …");
    const committed = await postJSON(config!.commitUrl, { token });
    if (!committed.ok) throw new Error(await errorOf(committed, "Uploaden kunne ikke gemmes."));
    return ((await committed.json()) as { editUrl: string }).editUrl;
  }

  async function upload(files: File[]): Promise<void> {
    if (busy || !files.length) return;
    if (!window.crypto?.subtle) {
      say("Din browser kan ikke uploade her. Brug en opdateret browser over https.", true);
      return;
    }
    if (config!.video && files.length > 1) {
      say("Vælg kun én fil: den nye version.", true);
      return;
    }
    if (files.length > room) {
      // Not the first few of them quietly: the uploader picks which.
      say(
        room
          ? `Du har plads til ${room} ${room === 1 ? "kladde" : "kladder"} mere (højst ${config!.maxDrafts}), ` +
              `men valgte ${files.length} filer. Vælg færre.`
          : `Du har allerede ${config!.maxDrafts} videoer, der ikke er udgivet. Udgiv eller slet en af dem først.`,
        true,
      );
      return;
    }
    busy = true;
    drop.hidden = true;
    queue.hidden = false;
    queue.replaceChildren();
    say(files.length > 1 ? `Sender ${files.length} videoer, én ad gangen. Lad siden være åben imens.` : "");
    window.addEventListener("beforeunload", guard);
    const rows = files.map(row);
    const sent: string[] = [];
    for (const [index, file] of files.entries()) {
      try {
        sent.push(await send(file, rows[index]));
        rows[index].done(sent[sent.length - 1]);
        room -= config!.video ? 0 : 1;
        if (roomNote) roomNote.textContent = `, og du har plads til ${room} ${room === 1 ? "kladde" : "kladder"} mere`;
        refreshLiveLists(); // the file just saved joins the list above
      } catch (error) {
        rows[index].failed(error instanceof Error ? error.message : "Uploaden mislykkedes.");
      }
    }
    window.removeEventListener("beforeunload", guard);
    busy = false;
    input.value = "";
    if (files.length === 1 && sent.length === 1) {
      say("Færdig. Videoen behandles nu …");
      window.location.href = sent[0];
      return;
    }
    drop.hidden = room <= 0;
    if (files.length === 1) say("");
    else
      say(
        `${sent.length} af ${files.length} videoer er uploadet. De behandles én ad gangen; ` +
          "skriv titel og oplysninger imens.",
        sent.length < files.length,
      );
  }

  input.addEventListener("change", () => void upload([...(input.files ?? [])]));
  drop.addEventListener("dragover", (event) => {
    event.preventDefault();
    drop.classList.add("is-over");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("is-over");
    void upload([...(event.dataTransfer?.files ?? [])]);
  });
}

/** Lists of one's own uploads (`data-flix-live`: the address of a fresh copy) that follow the
 * processing as it happens: re-fetched every few seconds while any of them (`data-flix-pending`) waits
 * or is processed, and at once when the uploader has saved a new file. */
const LIVE_LIST_MS = 3000;
const liveLists: (() => Promise<void>)[] = [];

function initLiveLists(): void {
  for (const section of document.querySelectorAll<HTMLElement>("[data-flix-live]")) {
    const target = section.querySelector<HTMLElement>("[data-flix-live-list]")!;
    let timer = 0;
    const schedule = () => {
      window.clearTimeout(timer);
      if (target.querySelector("[data-flix-pending]")) timer = window.setTimeout(() => void refresh(), LIVE_LIST_MS);
    };
    const refresh = async () => {
      window.clearTimeout(timer);
      if (!document.hidden) {
        try {
          const response = await fetch(section.dataset.flixLive!, { credentials: "same-origin" });
          if (response.ok) {
            target.innerHTML = await response.text();
            section.hidden = !target.querySelector(".flix-list-item");
          }
        } catch {
          // A missed refresh is harmless; the next one catches up.
        }
      }
      schedule();
    };
    liveLists.push(refresh);
    schedule();
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) void refresh();
    });
  }
}

function refreshLiveLists(): void {
  for (const refresh of liveLists) void refresh();
}

type ProcessingStatus = { state: string; progress: number; ahead: number };

/** The edit page while a file is processed: whether it waits its turn or how far it has come,
 * and a reload when it is done. */
function initProcessing(): void {
  const panel = document.querySelector<HTMLElement>("[data-flix-processing]");
  if (!panel) return;
  const meter = panel.querySelector<HTMLElement>("[data-flix-meter]");
  const text = panel.querySelector<HTMLElement>("[data-flix-processing-status]");
  const poll = async () => {
    try {
      const response = await fetch(panel.dataset.flixProcessing!, { credentials: "same-origin" });
      if (response.ok) {
        const data = (await response.json()) as ProcessingStatus;
        if (data.state === "ready" || data.state === "failed" || data.state === "") {
          window.location.reload();
          return;
        }
        const queued = data.state === "queued";
        for (const part of panel.querySelectorAll<HTMLElement>("[data-flix-when]")) {
          part.hidden = part.dataset.flixWhen !== (queued ? "queued" : "processing");
        }
        if (meter) meter.style.width = `${data.progress}%`;
        if (text) {
          const waiting = data.ahead ? `${data.ahead} ${data.ahead === 1 ? "video" : "videoer"} foran` : "den er den næste";
          text.textContent = queued ? `I kø · ${waiting}` : `${data.progress} % færdig`;
        }
      }
    } catch {
      // A missed poll is harmless; the next one catches up.
    }
    window.setTimeout(poll, 3000);
  };
  window.setTimeout(poll, 3000);
}

// --- The editors: cast and tags -------------------------------------------------------------------

type Option = { label: string; detail?: string; value: unknown };

/** A text input with a dropdown of suggestions, driven from the keyboard or the pointer. */
class Suggest {
  private options: Option[] = [];
  private active = -1;

  constructor(
    private input: HTMLInputElement,
    private list: HTMLUListElement,
    private fetchOptions: (query: string) => Promise<Option[]>,
    private choose: (option: Option | null, text: string) => void,
  ) {
    const refresh = debounce(() => void this.refresh(), 180);
    input.addEventListener("input", refresh);
    input.addEventListener("keydown", (event) => this.key(event));
    input.addEventListener("blur", () => window.setTimeout(() => this.close(), 150));
  }

  private async refresh(): Promise<void> {
    this.options = await this.fetchOptions(this.input.value.trim());
    this.active = -1;
    this.render();
  }

  private render(): void {
    this.list.replaceChildren();
    this.options.forEach((option, index) => {
      const item = document.createElement("li");
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(index === this.active));
      item.textContent = option.label;
      if (option.detail) {
        const small = document.createElement("small");
        small.textContent = option.detail;
        item.append(small);
      }
      item.addEventListener("mousedown", (event) => {
        event.preventDefault();
        this.pick(option);
      });
      this.list.append(item);
    });
    this.list.hidden = this.options.length === 0;
  }

  private key(event: KeyboardEvent): void {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!this.options.length) return;
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      this.active = (this.active + step + this.options.length) % this.options.length;
      this.render();
    } else if (event.key === "Enter" || (event.key === "," && this.input.dataset.comma === "1")) {
      event.preventDefault();
      if (this.active >= 0) this.pick(this.options[this.active]);
      else if (this.input.value.trim()) this.pick(null);
    } else if (event.key === "Escape") {
      this.close();
    }
  }

  private pick(option: Option | null): void {
    this.choose(option, this.input.value.trim());
    this.input.value = "";
    this.close();
  }

  close(): void {
    this.options = [];
    this.active = -1;
    this.list.hidden = true;
    this.list.replaceChildren();
  }
}

type Credit = { resident: number | null; name: string; role: string };

function hiddenTarget(root: HTMLElement): HTMLInputElement | null {
  return document.getElementById(root.dataset.target ?? "") as HTMLInputElement | null;
}

function parseList<T>(value: string): T[] {
  try {
    const parsed = JSON.parse(value || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function initCastEditor(): void {
  const root = document.querySelector<HTMLElement>("[data-flix-cast]");
  const hidden = root && hiddenTarget(root);
  if (!root || !hidden) return;
  const list = root.querySelector<HTMLElement>("[data-list]")!;
  const credits = parseList<Credit>(hidden.value);
  const sync = () => (hidden.value = JSON.stringify(credits));

  const render = () => {
    list.replaceChildren();
    credits.forEach((credit, index) => {
      const row = document.createElement("div");
      row.className = "flix-credit";
      const name = document.createElement("span");
      name.className = "flix-credit-name";
      name.textContent = credit.name;
      const kind = document.createElement("small");
      kind.textContent = credit.resident ? "beboer" : "udefra";
      name.append(kind);
      const role = document.createElement("input");
      role.type = "text";
      role.value = credit.role;
      role.placeholder = "Rolle (valgfri)";
      role.maxLength = 120;
      role.setAttribute("aria-label", `Rolle for ${credit.name}`);
      role.addEventListener("input", () => {
        credit.role = role.value;
        sync();
      });
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "flix-x";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `Fjern ${credit.name}`);
      remove.addEventListener("click", () => {
        credits.splice(index, 1);
        sync();
        render();
      });
      row.append(name, role, remove);
      list.append(row);
    });
  };

  new Suggest(
    root.querySelector<HTMLInputElement>("[data-input]")!,
    root.querySelector<HTMLUListElement>("[data-suggestions]")!,
    async (query) => {
      if (!query) return [];
      const response = await fetch(`${root.dataset.lookup}?${new URLSearchParams({ q: query })}`, {
        credentials: "same-origin",
      });
      const data = response.ok ? await response.json() : { residents: [] };
      const taken = new Set(credits.map((c) => c.resident));
      const options: Option[] = (data.residents as { id: number; name: string }[])
        .filter((r) => !taken.has(r.id))
        .map((r) => ({ label: r.name, detail: "beboer", value: r }));
      options.push({ label: `Tilføj "${query}"`, detail: "udefra", value: null });
      return options;
    },
    (option, text) => {
      const resident = option?.value as { id: number; name: string } | null | undefined;
      if (resident) credits.push({ resident: resident.id, name: resident.name, role: "" });
      else if (text) credits.push({ resident: null, name: text.slice(0, 120), role: "" });
      else return;
      sync();
      render();
    },
  );
  render();
}

const TAG_PATTERN = /^[\p{L}\p{N}._-]{1,30}$/u;

function initTagEditor(): void {
  const root = document.querySelector<HTMLElement>("[data-flix-tags]");
  const hidden = root && hiddenTarget(root);
  if (!root || !hidden) return;
  const list = root.querySelector<HTMLElement>("[data-list]")!;
  const input = root.querySelector<HTMLInputElement>("[data-input]")!;
  input.dataset.comma = "1";
  const tags = parseList<string>(hidden.value);
  const sync = () => (hidden.value = JSON.stringify(tags));
  const add = (raw: string) => {
    const name = raw.trim().replace(/^#/, "");
    if (!TAG_PATTERN.test(name)) {
      input.setCustomValidity("Et tag må kun indeholde bogstaver, tal, punktum, bindestreg og understregning.");
      input.reportValidity();
      return;
    }
    input.setCustomValidity("");
    if (!tags.some((t) => t.toLowerCase() === name.toLowerCase())) tags.push(name);
    sync();
    render();
  };
  const render = () => {
    list.replaceChildren();
    tags.forEach((tag, index) => {
      const chip = document.createElement("span");
      chip.className = "flix-chip";
      chip.textContent = `#${tag}`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `Fjern ${tag}`);
      remove.addEventListener("click", () => {
        tags.splice(index, 1);
        sync();
        render();
      });
      chip.append(remove);
      list.append(chip);
    });
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Backspace" && !input.value && tags.length) {
      tags.pop();
      sync();
      render();
    }
  });
  new Suggest(
    input,
    root.querySelector<HTMLUListElement>("[data-suggestions]")!,
    async (query) => {
      if (!query) return [];
      const response = await fetch(`${root.dataset.lookup}?${new URLSearchParams({ q: query })}`, {
        credentials: "same-origin",
      });
      const data = response.ok ? await response.json() : { tags: [] };
      return (data.tags as string[])
        .filter((name) => !tags.some((t) => t.toLowerCase() === name.toLowerCase()))
        .map((name) => ({ label: `#${name}`, value: name }));
    },
    (option, text) => add(option ? String(option.value) : text),
  );
  render();
}

// --- Season and episode numbers ------------------------------------------------------------------
//
// Digits only, and a dropdown under each field of what the series already has: its seasons, and the
// episodes of the season chosen, each titled. Free numbers — the next one, and any gaps — can be
// picked; taken ones are shown but cannot. Typing any number is still allowed: an earlier season or
// episode is as good as the next.

type EpisodeNumber = { season: number | null; episode: number | null; title: string };
type NumberOption = { value: number; label: string; tag: string; taken: boolean };

function numberOptions(
  kind: "season" | "episode",
  rows: EpisodeNumber[],
  season: number | null,
): NumberOption[] {
  if (kind === "season") {
    const counts = new Map<number, number>();
    for (const row of rows) if (row.season) counts.set(row.season, (counts.get(row.season) ?? 0) + 1);
    const seasons = [...counts.keys()].sort((a, b) => a - b);
    const next = (seasons.at(-1) ?? 0) + 1;
    return [
      ...seasons.map((value) => ({
        value,
        label: `Sæson ${value}`,
        tag: `${counts.get(value)} afsnit`,
        taken: false,
      })),
      { value: next, label: `Sæson ${next}`, tag: seasons.length ? "ny" : "første", taken: false },
    ];
  }
  const episodes = new Map<number, string>();
  for (const row of rows) if (row.episode && row.season === season) episodes.set(row.episode, row.title);
  const highest = Math.max(0, ...episodes.keys());
  const options: NumberOption[] = [];
  for (let value = 1; value <= highest + 1; value++) {
    const title = episodes.get(value);
    if (title !== undefined) options.push({ value, label: `Afsnit ${value} · ${title}`, tag: "findes", taken: true });
    else options.push({ value, label: `Afsnit ${value}`, tag: value > highest ? "næste" : "ledigt", taken: false });
  }
  return options;
}

function initNumberPickers(): void {
  const row = document.querySelector<HTMLElement>("[data-flix-numbers]");
  const numbers = jsonConfig<Record<string, EpisodeNumber[]>>("flix-episode-numbers");
  if (!row || !numbers) return;
  const series = document.getElementById(row.dataset.series ?? "") as HTMLSelectElement | null;
  const inputs = {
    season: row.querySelector<HTMLInputElement>('[data-flix-number="season"]')!,
    episode: row.querySelector<HTMLInputElement>('[data-flix-number="episode"]')!,
  };

  for (const kind of ["season", "episode"] as const) {
    const input = inputs[kind];
    const list = row.querySelector<HTMLUListElement>(`[data-numbers-for="${kind}"]`)!;
    const show = () => {
      list.replaceChildren();
      const rows = numbers[series?.value ?? ""] ?? [];
      if (!series?.value) {
        const item = document.createElement("li");
        item.className = "flix-number-taken";
        item.textContent = "Vælg en serie først";
        list.append(item);
      } else {
        const season = inputs.season.value ? Number(inputs.season.value) : null;
        for (const option of numberOptions(kind, rows, season)) {
          const item = document.createElement("li");
          item.setAttribute("role", "option");
          item.setAttribute("aria-disabled", String(option.taken));
          if (option.taken) item.className = "flix-number-taken";
          item.textContent = option.label;
          const tag = document.createElement("span");
          tag.className = "flix-number-tag";
          tag.textContent = option.tag;
          item.append(tag);
          if (!option.taken) {
            item.addEventListener("mousedown", (event) => {
              event.preventDefault(); // keep the focus, so the list does not close before the click
              input.value = String(option.value);
              list.hidden = true;
            });
          }
          list.append(item);
        }
      }
      list.hidden = false;
    };
    input.addEventListener("focus", show);
    input.addEventListener("click", show);
    input.addEventListener("blur", () => window.setTimeout(() => (list.hidden = true), 120));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") list.hidden = true;
    });
  }
}

/** Number fields — season, episode, the release year — take digits only, as they are typed. */
function initDigitFields(): void {
  for (const input of document.querySelectorAll<HTMLInputElement>('input[inputmode="numeric"]')) {
    input.addEventListener("input", () => {
      const digits = input.value.replace(/\D+/g, "").slice(0, input.maxLength > 0 ? input.maxLength : undefined);
      if (digits !== input.value) input.value = digits;
    });
  }
}

// --- Picking a frame yourself ---------------------------------------------------------------------

function formatTime(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds - minutes * 60;
  return `${minutes}:${rest.toFixed(2).padStart(5, "0").replace(".", ",")}`;
}

function initScrubber(): void {
  for (const root of document.querySelectorAll<HTMLElement>("[data-flix-scrub]")) {
    const video = root.querySelector("video")!;
    const time = root.querySelector<HTMLOutputElement>("[data-flix-scrub-time]")!;
    const at = root.querySelector<HTMLInputElement>("[data-flix-scrub-at]")!;
    const update = () => {
      time.value = formatTime(video.currentTime);
      at.value = video.currentTime.toFixed(3);
    };
    for (const name of ["timeupdate", "seeked", "loadedmetadata"]) video.addEventListener(name, update);
    for (const button of root.querySelectorAll<HTMLButtonElement>("[data-step]")) {
      button.addEventListener("click", () => {
        video.pause();
        const limit = Number.isFinite(video.duration) ? video.duration : Infinity;
        video.currentTime = Math.min(limit, Math.max(0, video.currentTime + Number(button.dataset.step)));
      });
    }
    update();
  }
}

// --- What is typed on the edit page ---------------------------------------------------------------
//
// THE DETAILS OUTLIVE THE PAGE. Most of what is done on the edit page reloads it: picking a picture,
// a new version, the processing finishing. What was typed under "Oplysninger" and not saved is kept
// in the tab's sessionStorage as the page goes, and put back when it comes again — only the fields
// that were changed, so a date the worker found meanwhile still shows — with a note that it is not
// saved yet. Saving the form, or "Fortryd det", lets it go. "Vis 10 nye billeder" and "Opret en ny
// serie" do not reload the page at all.

type KeptDetails = { at: number; base: Record<string, string>; values: Record<string, string> };

// A page come back to after this is not taken to be the same piece of work.
const KEEP_DETAILS_MS = 12 * 60 * 60 * 1000;

function detailFields(form: HTMLFormElement): (HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement)[] {
  return [...form.elements].filter(
    (element): element is HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement =>
      (element instanceof HTMLInputElement && element.type !== "file") ||
      element instanceof HTMLTextAreaElement ||
      element instanceof HTMLSelectElement,
  );
}

function detailValues(form: HTMLFormElement): Record<string, string> {
  const values: Record<string, string> = {};
  for (const field of detailFields(form)) {
    if (!field.name || field.name === "csrfmiddlewaretoken") continue;
    // The cast and the tags are JSON, written by the server one way and by their editors another.
    let value = field.value;
    if (field instanceof HTMLInputElement && field.type === "checkbox") {
      value = field.checked ? "on" : "";
    } else if (field.type === "hidden") {
      try {
        value = JSON.stringify(JSON.parse(value || "null"));
      } catch {
        // Not JSON: compared as it is.
      }
    }
    values[field.name] = value;
  }
  return values;
}

function initDetailsKeeper(): void {
  const form = document.querySelector<HTMLFormElement>("form[data-flix-details]");
  if (!form) return;
  const key = `gahkflix-details-${form.dataset.flixDetails}`;
  const notice = form.querySelector<HTMLElement>("[data-flix-unsaved]");
  const loaded = detailValues(form);
  // A form sent back with errors shows what was typed, and none of it is saved.
  const base = form.hasAttribute("data-flix-bound") ? {} : loaded;
  let letGo = false;
  const store = (kept: KeptDetails | null) => {
    try {
      if (kept) window.sessionStorage.setItem(key, JSON.stringify(kept));
      else window.sessionStorage.removeItem(key);
    } catch {
      // Private mode: nothing is kept.
    }
  };

  let kept: KeptDetails | null = null;
  try {
    kept = JSON.parse(window.sessionStorage.getItem(key) ?? "null") as KeptDetails | null;
  } catch {
    kept = null;
  }
  if (kept && Date.now() - kept.at < KEEP_DETAILS_MS) {
    let restored = false;
    for (const field of detailFields(form)) {
      const value = kept.values[field.name];
      if (value === undefined || value === kept.base[field.name] || value === loaded[field.name]) continue;
      if (field instanceof HTMLInputElement && field.type === "checkbox") field.checked = value === "on";
      else field.value = value;
      restored = true;
    }
    if (notice) notice.hidden = !restored;
  }
  store(null);

  // Saved (Gem, or Udgiv, whose button belongs to this form), or undone: nothing to keep.
  form.addEventListener("submit", () => (letGo = true));
  notice?.querySelector("[data-flix-discard]")?.addEventListener("click", () => {
    letGo = true;
    window.location.reload();
  });
  window.addEventListener("pagehide", () => {
    const values = detailValues(form);
    const changed = Object.keys(values).some((name) => values[name] !== base[name]);
    store(letGo || !changed ? null : { at: Date.now(), base, values });
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) letGo = false; // back from the browser's cache: the page is as it was left
  });
}

/** The release date's Premiere box: only a premiere has a time and a blur for its thumbnail, and the
 * preview shows the thumbnail as the blur chosen makes it (views.picture blurs it). */
function initPremiere(): void {
  const box = document.querySelector<HTMLElement>("[data-flix-premiere]");
  const tick = box?.querySelector<HTMLInputElement>('input[type="checkbox"]');
  if (!box || !tick) return;
  const blur = box.querySelector<HTMLSelectElement>("select[name='premiere_blur']");
  const preview = box.querySelector<HTMLImageElement>("[data-flix-premiere-preview]");
  const time = box.querySelector<HTMLInputElement>("input[name='premiere_time']");
  const sync = () => {
    for (const part of box.querySelectorAll<HTMLElement>("[data-flix-premiere-only]")) part.hidden = !tick.checked;
    for (const part of box.querySelectorAll<HTMLElement>("[data-flix-premiere-hide]")) part.hidden = tick.checked;
    if (time) time.required = tick.checked;
  };
  tick.addEventListener("change", () => {
    sync();
    if (tick.checked) time?.focus();
  });
  blur?.addEventListener("change", () => {
    if (preview) preview.src = `${preview.dataset.flixPremierePreview}&premiere=${encodeURIComponent(blur.value)}`;
  });
  blur?.dispatchEvent(new Event("change"));
  sync();
}

/** "Brug videoens dato": the date the file suggests ("2015", "2015-03" or "2015-03-12") put in the
 * year, month and day, a part it does not know left empty. Nothing is filled in without it. */
function initDateGuess(): void {
  const button = document.querySelector<HTMLButtonElement>("[data-flix-date-guess]");
  const form = button?.closest("form");
  if (!button || !form) return;
  button.addEventListener("click", () => {
    const [year = "", month = "", day = ""] = (button.dataset.flixDateGuess ?? "").split("-");
    const parts: [string, string][] = [
      ["released_year", year],
      ["released_month", month ? String(Number(month)) : ""],
      ["released_day", day ? String(Number(day)) : ""],
    ];
    for (const [name, value] of parts) {
      const field = form.elements.namedItem(name) as HTMLInputElement | HTMLSelectElement | null;
      if (!field) continue;
      field.value = value;
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });
}

/** "Vis 10 nye billeder" without leaving the page: the worker draws them, and the new ones are swapped
 * in from the edit page once its status says they are there. */
const NEW_FRAMES_WAIT_MS = 3 * 60 * 1000;

function initNewFrames(): void {
  const region = document.querySelector<HTMLElement>("[data-flix-frames]");
  if (!region) return;
  region.addEventListener("submit", async (event) => {
    const form = (event.target as HTMLElement).closest<HTMLFormElement>("form[data-flix-new-frames]");
    if (!form) return;
    event.preventDefault();
    const button = form.querySelector<HTMLButtonElement>("button")!;
    const status = region.querySelector<HTMLElement>("[data-flix-new-frames-status]");
    const say = (text: string) => {
      if (status) status.textContent = text;
    };
    button.disabled = true;
    say("Der tages 10 nye billeder fra videoen …");
    try {
      const started = await fetch(jsonUrl(form.action), {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
      });
      if (!started.ok) throw new Error(await errorOf(started, "Der kunne ikke tages nye billeder."));
      const { batch } = (await started.json()) as { batch: number };
      const until = Date.now() + NEW_FRAMES_WAIT_MS;
      while (Date.now() < until) {
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
        const polled = await fetch(form.dataset.flixNewFrames!, { credentials: "same-origin" }).catch(() => null);
        if (!polled?.ok || ((await polled.json()) as { batch: number }).batch <= batch) continue;
        const page = await fetch(window.location.pathname, { credentials: "same-origin" });
        const fresh = new DOMParser().parseFromString(await page.text(), "text/html").querySelector("[data-flix-frames]");
        if (!fresh) break;
        region.replaceChildren(...fresh.childNodes);
        return;
      }
      // The media worker takes one job at a time; it may be busy with somebody's film.
      say("De nye billeder er ikke klar endnu. Genindlæs siden om lidt for at se dem.");
    } catch (error) {
      say(error instanceof Error ? error.message : "Der kunne ikke tages nye billeder.");
    }
    button.disabled = false;
  });
}

/** "Opret en ny serie" in a dialog: the series is created and picked, and the page stays as it is. */
function initNewSeries(): void {
  const link = document.querySelector<HTMLAnchorElement>("a[data-flix-new-series]");
  const dialog = document.querySelector<HTMLDialogElement>("dialog[data-flix-series-dialog]");
  const select = document.getElementById(link?.dataset.series ?? "") as HTMLSelectElement | null;
  if (!link || !dialog || !select || typeof dialog.showModal !== "function") return;
  const form = dialog.querySelector<HTMLFormElement>("form")!;
  const error = dialog.querySelector<HTMLElement>("[data-flix-dialog-error]")!;
  const note = document.querySelector<HTMLElement>("[data-flix-new-series-note]");
  link.addEventListener("click", (event) => {
    event.preventDefault();
    error.hidden = true;
    dialog.showModal();
  });
  dialog.querySelector("[data-flix-dialog-close]")?.addEventListener("click", () => dialog.close());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    button.disabled = true;
    try {
      const response = await fetch(form.action, { method: "POST", body: new FormData(form), credentials: "same-origin" });
      if (!response.ok) throw new Error(await errorOf(response, "Serien kunne ikke oprettes."));
      const series = (await response.json()) as { id: number; title: string };
      // In its place in the list, which is by title.
      const before = [...select.options].find(
        (option) => option.value && option.text.localeCompare(series.title, "da", { sensitivity: "base" }) > 0,
      );
      select.add(new Option(series.title, String(series.id)), before ?? null);
      select.value = String(series.id);
      select.dispatchEvent(new Event("change", { bubbles: true }));
      form.reset();
      dialog.close();
      if (note) note.textContent = `${series.title} er oprettet og valgt. Husk at gemme.`;
    } catch (failure) {
      error.textContent = failure instanceof Error ? failure.message : "Serien kunne ikke oprettes.";
      error.hidden = false;
    } finally {
      button.disabled = false;
    }
  });
}

// --- The player -----------------------------------------------------------------------------------
//
// THE INTRO STARTS IN THE CLICK. A browser lets a page play sound only from inside the click (or tap)
// that asked for it; a page loaded by following a link has none of that, so on its own the play page
// has to wait for a second click. So "Afspil" (anything marked data-flix-play) opens the player OVER
// the page it was clicked on: the intro starts there and then, the film's details are fetched while
// it plays, and the address becomes the play page's so Back closes the player again. The play page
// itself is still there for a link opened directly, with a play button if the browser says no.
//
// ONE <video> for the intro and the film, and for the next episode after it: a phone that unlocked
// the element with a tap lets that element go on playing, where a fresh one would need another tap.

type FilmConfig = {
  title: string;
  subtitle: string;
  streams: Record<string, string>;
  qualityLabels: Record<string, string>;
  subtitles: SubtitleTrack[];
  startAt: number;
  viewUrl: string;
  positionUrl: string;
  playUrl: string;
  backUrl: string;
  next: UpNext | null;
  // The viewing the statistics keep of this opening of the film; null for a draft.
  viewing: number | null;
};

type SubtitleTrack = {
  id: number;
  label: string;
  language: string;
  url: string;
  default: boolean;
  // Only the lines that must be read whatever else is chosen (signs, foreign speech): shown when
  // no other track is.
  forced: boolean;
};

type UpNext = {
  url: string;
  title: string;
  kind: "episode" | "film";
  label: string;
  description: string;
  image: string;
};

const QUALITY_KEY = "gahkflix-quality";
// A view counts once this much of the film has actually played (or half of a very short one).
const VIEW_AFTER_SECONDS = 5;
const SAVE_EVERY_MS = 15000;
const NEXT_COUNTDOWN_SECONDS = 8;
const AUTOPLAY_KEY = "gahkflix-autoplay";
// Autoplays in a row with no input before the player asks "Ser du stadig med?".
const STILL_WATCHING_AFTER = 3;
// The subtitles chosen last, by language ("off" for none), and the sound: the next film starts so too.
const SUBTITLES_KEY = "gahkflix-subtitles";
const VOLUME_KEY = "gahkflix-volume";
const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2];
// A jump: the ±10 s buttons, J and L, the arrow keys, a double tap on either side.
const JUMP_SECONDS = 10;
// Two taps closer than this are a double tap.
const DOUBLE_TAP_MS = 300;
// How long the controls stay after the mouse last moved while the film plays.
const IDLE_AFTER_MS = 3000;

function stored(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function store(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Private mode: the choice just is not remembered.
  }
}

/** "4:05", "1:02:09". */
function clock(seconds: number): string {
  const whole = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = String(whole % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

/** "Normal", "1,5×". */
function speedLabel(rate: number): string {
  return rate === 1 ? "Normal" : `${String(rate).replace(".", ",")}×`;
}

function jsonUrl(url: string): string {
  const target = new URL(url, window.location.href);
  target.searchParams.set("format", "json");
  return target.toString();
}

async function fetchFilm(url: string): Promise<FilmConfig> {
  const response = await fetch(jsonUrl(url), { credentials: "same-origin", headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(await errorOf(response, "Videoen kunne ikke hentes."));
  return (await response.json()) as FilmConfig;
}

type WebkitVideo = HTMLVideoElement & {
  webkitDisplayingFullscreen?: boolean;
  webkitEnterFullscreen?: () => void;
  webkitExitFullscreen?: () => void;
};

class Player {
  private video: WebkitVideo;
  private skip: HTMLButtonElement;
  private start: HTMLButtonElement;
  private errorBox: HTMLElement;
  private next: HTMLAnchorElement;
  private back: HTMLAnchorElement;
  private autoplay: HTMLInputElement;
  private upNext: HTMLElement;
  private upNextPlay: HTMLButtonElement;
  private upNextCancel: HTMLButtonElement;
  private upNextRing: SVGElement;
  // The controls.
  private controls: HTMLElement;
  private timeline: HTMLElement;
  private played: HTMLElement;
  private buffered: HTMLElement;
  private thumb: HTMLElement;
  private tip: HTMLElement;
  private toggle: HTMLButtonElement;
  private mute: HTMLButtonElement;
  private volume: HTMLInputElement;
  private time: HTMLElement;
  private qualityBar: HTMLElement;
  private qualityGroup: HTMLElement;
  private speeds: HTMLElement;
  private captionsWrap: HTMLElement;
  private captionsMenu: HTMLElement;
  private captions: HTMLElement;
  private pip: HTMLButtonElement;
  private fullscreen: HTMLButtonElement;
  private flash: HTMLElement;
  private spinner: HTMLElement;

  private stillWatching = false;
  private autoplays = 0;
  private film: FilmConfig | null = null;
  private phase: "intro" | "film" = "intro";
  private filmWanted = false;
  private quality = "";
  private speed = 1;
  // The subtitle track chosen in the menu (null: none), and the one last shown, for C to bring back.
  private track: SubtitleTrack | null = null;
  private lastTrack: SubtitleTrack | null = null;
  private filmTime = 0;
  // Seconds of the film that have actually played, and of those the ones not yet reported.
  private watched = 0;
  private unreported = 0;
  private lastTime: number | null = null;
  private counted = false;
  private retries = 0;
  private lastSaved = 0;
  private countdown = 0;
  private idleTimer = 0;
  private spinnerTimer = 0;
  private tapTimer = 0;
  private lastTap: { at: number; side: "back" | "middle" | "ahead" } | null = null;
  private lastPointer = "mouse";
  private scrubbing = false;
  private closed = false;
  private listeners = new AbortController();

  constructor(
    private root: HTMLElement,
    private overlay: boolean,
  ) {
    const find = <T extends Element>(selector: string) => root.querySelector<T>(selector)!;
    this.video = find<HTMLVideoElement>("video");
    this.skip = find("[data-flix-skip]");
    this.start = find("[data-flix-start]");
    this.errorBox = find("[data-flix-error]");
    this.next = find("[data-flix-next]");
    this.back = find("[data-flix-back]");
    this.autoplay = find("[data-flix-autoplay]");
    this.autoplay.checked = stored(AUTOPLAY_KEY) !== "off";
    this.upNext = find("[data-flix-upnext]");
    this.upNextPlay = find("[data-flix-upnext-play]");
    this.upNextCancel = find("[data-flix-upnext-cancel]");
    this.upNextRing = find("[data-flix-upnext-ring]");
    this.controls = find("[data-flix-controls]");
    this.timeline = find("[data-flix-timeline]");
    this.played = find("[data-flix-played]");
    this.buffered = find("[data-flix-buffered]");
    this.thumb = find("[data-flix-thumb]");
    this.tip = find("[data-flix-tip]");
    this.toggle = find("[data-flix-toggle]");
    this.mute = find("[data-flix-mute]");
    this.volume = find("[data-flix-volume]");
    this.time = find("[data-flix-time]");
    this.qualityBar = find("[data-flix-quality]");
    this.qualityGroup = find("[data-flix-quality-group]");
    this.speeds = find("[data-flix-speeds]");
    this.captionsWrap = find("[data-flix-captions-wrap]");
    this.captionsMenu = find('[data-flix-menu="captions"]');
    this.captions = find("[data-flix-captions]");
    this.pip = find("[data-flix-pip]");
    this.fullscreen = find("[data-flix-fullscreen]");
    this.flash = find("[data-flix-flash]");
    this.spinner = find("[data-flix-spinner]");
    this.pip.hidden = !document.pictureInPictureEnabled;
    this.speeds.replaceChildren(...SPEEDS.map((rate) => this.menuItem(speedLabel(rate), "speed", String(rate))));
    this.restoreSound();
    this.bind();
  }

  /** The intro. Called straight from the click, so the browser lets it play with sound. */
  playIntro(): void {
    this.phase = "intro";
    this.filmWanted = false;
    this.controls.hidden = true;
    this.closeMenus();
    this.next.hidden = true;
    this.errorBox.hidden = true;
    this.captions.replaceChildren();
    this.hideUpNext();
    this.video.defaultPlaybackRate = 1;
    this.video.playbackRate = 1;
    const intro = body.dataset.flixIntro;
    if (!intro) {
      this.startFilm();
      return;
    }
    this.skip.hidden = false;
    this.video.src = intro;
    this.tryPlay();
  }

  /** The film to play after the intro; it starts at once if the intro is already over. */
  setFilm(film: FilmConfig): void {
    this.film = film;
    this.filmTime = film.startAt || 0;
    this.watched = 0;
    this.unreported = 0;
    this.lastTime = null;
    this.counted = false;
    this.retries = 0;
    this.root.querySelector("[data-flix-title]")!.textContent = film.title;
    this.root.querySelector("[data-flix-subtitle]")!.textContent = film.subtitle;
    document.title = `${film.subtitle || film.title} – GAHKFlix`;
    if (!this.overlay) this.back.href = film.backUrl;
    const qualities = Object.keys(film.streams);
    this.quality = stored(QUALITY_KEY) ?? "";
    if (!qualities.includes(this.quality)) {
      const narrow = window.matchMedia("(max-width: 900px)").matches;
      this.quality = narrow && qualities.includes("hd") ? "hd" : qualities[0];
    }
    this.qualityBar.replaceChildren(
      ...qualities.map((quality) => this.menuItem(film.qualityLabels[quality] ?? quality, "quality", quality)),
    );
    this.qualityGroup.hidden = qualities.length < 2;
    this.setTracks(film.subtitles ?? []);
    this.next.href = film.next?.url ?? "#";
    this.next.textContent = "Næste afsnit ▸";
    if (this.filmWanted) this.startFilm();
  }

  private tryPlay(): void {
    this.video.play().catch(() => (this.start.hidden = false));
  }

  private startFilm(): void {
    this.filmWanted = true;
    if (!this.film || this.phase === "film" || this.closed) return;
    this.phase = "film";
    this.skip.hidden = true;
    this.controls.hidden = false;
    this.load(this.filmTime, true);
    this.showTrack();
  }

  private load(at: number, play: boolean): void {
    if (!this.film) return;
    this.video.defaultPlaybackRate = this.speed;
    this.video.src = this.film.streams[this.quality];
    this.video.playbackRate = this.speed;
    this.video.addEventListener(
      "loadedmetadata",
      () => {
        if (at > 1 && (!this.video.duration || at < this.video.duration - 2)) this.video.currentTime = at;
      },
      { once: true },
    );
    this.check(this.qualityBar, this.quality);
    this.check(this.speeds, String(this.speed));
    if (play) this.tryPlay();
  }

  // --- Menus ------------------------------------------------------------------------------------

  private menuItem(label: string, kind: string, value: string): HTMLButtonElement {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "flix-popup-item";
    item.setAttribute("role", "menuitemradio");
    item.setAttribute("aria-checked", "false");
    item.dataset.kind = kind;
    item.dataset.value = value;
    item.textContent = label;
    return item;
  }

  /** Ticks the item with `value` among the menu items in `group`. */
  private check(group: HTMLElement, value: string): void {
    for (const item of group.querySelectorAll<HTMLElement>("[data-value]")) {
      item.setAttribute("aria-checked", String(item.dataset.value === value));
    }
  }

  private openMenu(name: string): void {
    for (const button of this.root.querySelectorAll<HTMLElement>("[data-flix-menu-button]")) {
      const open = button.dataset.flixMenuButton === name;
      button.setAttribute("aria-expanded", String(open));
      this.root.querySelector<HTMLElement>(`[data-flix-menu="${button.dataset.flixMenuButton}"]`)!.hidden = !open;
    }
    this.root.classList.toggle("is-menu-open", Boolean(name));
    if (name) {
      const menu = this.root.querySelector<HTMLElement>(`[data-flix-menu="${name}"]`)!;
      (menu.querySelector<HTMLElement>('[aria-checked="true"]') ?? menu.querySelector<HTMLElement>("button"))?.focus();
    }
  }

  private closeMenus(): boolean {
    const open = this.root.classList.contains("is-menu-open");
    this.openMenu("");
    return open;
  }

  // --- Subtitles ----------------------------------------------------------------------------------
  //
  // DRAWN BY THE PLAYER, not the browser: the tracks are "hidden" (loaded, cues firing, nothing drawn),
  // and the cues go into .flix-captions, which moves up while the controls show. Only iOS's own
  // fullscreen player, where nothing of ours shows, draws them itself ("showing").

  private setTracks(tracks: SubtitleTrack[]): void {
    for (const old of this.video.querySelectorAll("track")) old.remove();
    this.captions.replaceChildren();
    for (const track of tracks) {
      const element = document.createElement("track");
      element.kind = "subtitles";
      element.label = track.label;
      element.srclang = track.language || "und";
      element.src = track.url;
      element.dataset.id = String(track.id);
      this.video.append(element);
      element.track.mode = "disabled";
      element.track.addEventListener("cuechange", () => this.drawCues());
    }
    const choosable = tracks.filter((track) => !track.forced);
    this.captionsWrap.hidden = choosable.length === 0;
    this.captionsMenu.replaceChildren(
      this.menuItem("Fra", "subtitles", ""),
      ...choosable.map((track) => this.menuItem(track.label, "subtitles", String(track.id))),
    );
    // As chosen last time, by language; else the track the file marks as its default.
    const wanted = stored(SUBTITLES_KEY);
    this.track =
      wanted === "off"
        ? null
        : ((wanted ? choosable.find((track) => (track.language || String(track.id)) === wanted) : undefined) ??
          choosable.find((track) => track.default) ??
          null);
    this.lastTrack = this.track ?? choosable[0] ?? null;
    this.showTrack();
  }

  /** The track to draw: the one chosen, else the forced lines of the film, if it has any. */
  private shownTrack(): SubtitleTrack | null {
    return this.track ?? this.film?.subtitles?.find((track) => track.forced) ?? null;
  }

  private showTrack(): void {
    const shown = this.phase === "film" ? this.shownTrack() : null;
    const native = Boolean(this.video.webkitDisplayingFullscreen);
    for (const element of this.video.querySelectorAll("track")) {
      const on = shown !== null && element.dataset.id === String(shown.id);
      element.track.mode = on ? (native ? "showing" : "hidden") : "disabled";
    }
    this.check(this.captionsMenu, this.track ? String(this.track.id) : "");
    this.root.classList.toggle("has-subtitles", this.track !== null);
    this.drawCues();
  }

  private chooseTrack(track: SubtitleTrack | null): void {
    this.track = track;
    if (track) this.lastTrack = track;
    store(SUBTITLES_KEY, track ? track.language || String(track.id) : "off");
    this.showTrack();
  }

  private drawCues(): void {
    const shown = this.phase === "film" ? this.shownTrack() : null;
    const element = shown && this.video.querySelector<HTMLTrackElement>(`track[data-id="${shown.id}"]`);
    const cues = element ? [...(element.track.activeCues ?? [])] : [];
    this.captions.replaceChildren(
      ...cues.map((cue) => {
        const line = document.createElement("span");
        line.className = "flix-caption";
        // getCueAsHTML keeps <i> and <b> and nothing that could run; plain text where it is missing.
        if (cue instanceof VTTCue && typeof cue.getCueAsHTML === "function") line.append(cue.getCueAsHTML());
        else line.textContent = (cue as VTTCue).text ?? "";
        return line;
      }),
    );
  }

  // --- Sound, time and the timeline -----------------------------------------------------------------

  private restoreSound(): void {
    try {
      const sound = JSON.parse(stored(VOLUME_KEY) ?? "null") as { volume?: number; muted?: boolean } | null;
      if (sound && typeof sound.volume === "number") this.video.volume = Math.min(1, Math.max(0, sound.volume));
      if (sound?.muted) this.video.muted = true;
    } catch {
      // Unreadable: full sound.
    }
    this.showSound();
  }

  private showSound(): void {
    const { volume, muted } = this.video;
    const level = muted || volume === 0 ? "muted" : volume < 0.5 ? "quiet" : "loud";
    this.mute.dataset.level = level;
    this.mute.setAttribute("aria-label", level === "muted" ? "Slå lyden til" : "Slå lyden fra");
    this.volume.value = String(muted ? 0 : volume);
    this.volume.style.setProperty("--flix-fill", `${(muted ? 0 : volume) * 100}%`);
  }

  private setVolume(volume: number): void {
    this.video.volume = Math.min(1, Math.max(0, volume));
    this.video.muted = this.video.volume === 0;
  }

  private seekBy(seconds: number): void {
    if (this.phase !== "film" || !Number.isFinite(this.video.duration)) return;
    this.seekTo(this.video.currentTime + seconds);
  }

  private seekTo(at: number): void {
    const { duration } = this.video;
    this.lastTime = null;
    this.video.currentTime = Math.min(Math.max(0, at), Math.max(0, (duration || 0) - 0.1));
    this.showTime();
  }

  private showTime(at = this.video.currentTime): void {
    const duration = Number.isFinite(this.video.duration) ? this.video.duration : 0;
    const share = duration ? Math.min(1, at / duration) : 0;
    this.played.style.width = `${share * 100}%`;
    this.thumb.style.left = `${share * 100}%`;
    this.time.textContent = `${clock(at)} / ${clock(duration)}`;
    this.timeline.setAttribute("aria-valuemax", String(Math.round(duration)));
    this.timeline.setAttribute("aria-valuenow", String(Math.round(at)));
    this.timeline.setAttribute("aria-valuetext", `${clock(at)} af ${clock(duration)}`);
    // What is loaded from here on.
    const ranges = this.video.buffered;
    let end = 0;
    for (let index = 0; index < ranges.length; index++) {
      if (ranges.start(index) <= at + 0.5 && ranges.end(index) >= at) end = ranges.end(index);
    }
    this.buffered.style.width = duration ? `${Math.min(1, end / duration) * 100}%` : "0";
  }

  /** The moment under the pointer on the timeline. */
  private timeAt(event: PointerEvent): number {
    const box = this.timeline.getBoundingClientRect();
    const share = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
    this.tip.textContent = clock(share * (this.video.duration || 0));
    this.tip.style.left = `${share * 100}%`;
    return share * (this.video.duration || 0);
  }

  // --- Showing and hiding the controls ----------------------------------------------------------------

  /** The title bar and the controls fade out while the film plays undisturbed. */
  private wake(): void {
    this.root.classList.remove("is-idle");
    window.clearTimeout(this.idleTimer);
    this.idleTimer = window.setTimeout(() => this.sleep(), IDLE_AFTER_MS);
  }

  private sleep(): void {
    if (this.video.paused || this.scrubbing || this.root.classList.contains("is-menu-open")) return;
    window.clearTimeout(this.idleTimer);
    this.root.classList.add("is-idle");
  }

  private showFlash(kind: "play" | "pause"): void {
    this.flash.dataset.kind = kind;
    this.flash.classList.remove("is-on");
    void this.flash.offsetWidth; // start the animation over
    this.flash.classList.add("is-on");
  }

  private showJump(side: "back" | "ahead"): void {
    const mark = this.root.querySelector<HTMLElement>(`[data-flix-jump="${side}"]`)!;
    mark.classList.remove("is-on");
    void mark.offsetWidth;
    mark.classList.add("is-on");
  }

  private togglePlay(flash = false): void {
    if (this.video.paused) {
      this.hideUpNext();
      void this.video.play().catch(() => (this.start.hidden = false));
    } else {
      this.video.pause();
    }
    if (flash && this.phase === "film") this.showFlash(this.video.paused ? "pause" : "play");
  }

  private toggleFullscreen(): void {
    if (document.fullscreenElement) {
      void document.exitFullscreen().catch(() => undefined);
    } else if (document.fullscreenEnabled && this.root.requestFullscreen) {
      void this.root.requestFullscreen().catch(() => undefined);
    } else {
      // An iPhone: only the video itself, in the system's player.
      this.video.webkitEnterFullscreen?.();
    }
  }

  private toggleSubtitles(): void {
    if (this.captionsWrap.hidden) return;
    this.chooseTrack(this.track ? null : this.lastTrack);
  }

  /** A tap on the picture with a finger: once shows or hides the controls; twice on either side jumps. */
  private tap(event: MouseEvent): void {
    const box = this.root.getBoundingClientRect();
    const x = (event.clientX - box.left) / box.width;
    const side = x < 1 / 3 ? "back" : x > 2 / 3 ? "ahead" : "middle";
    const now = Date.now();
    const again = this.lastTap && now - this.lastTap.at < DOUBLE_TAP_MS && this.lastTap.side === side;
    this.lastTap = { at: now, side };
    if (again && side !== "middle" && this.phase === "film") {
      window.clearTimeout(this.tapTimer);
      this.seekBy(side === "ahead" ? JUMP_SECONDS : -JUMP_SECONDS);
      this.showJump(side);
      return;
    }
    const showOrHide = () => {
      if (this.root.classList.contains("is-idle")) this.wake();
      else this.sleep();
    };
    window.clearTimeout(this.tapTimer);
    if (side === "middle") showOrHide();
    else this.tapTimer = window.setTimeout(showOrHide, DOUBLE_TAP_MS);
  }

  // --- Reporting, the end card, leaving ----------------------------------------------------------------

  /** Where the film is, and how much of it has played since the last report — put back to go with
   * the next one if this one does not arrive. */
  private savePosition(keepalive = false): Promise<unknown> {
    if (this.phase !== "film" || !this.film || !this.video.duration) return Promise.resolve();
    this.lastSaved = Date.now();
    const film = this.film;
    const watched = this.unreported;
    this.unreported = 0;
    const missed = () => {
      if (this.film === film) this.unreported += watched;
    };
    return postJSON(film.positionUrl, { position: this.video.currentTime, viewing: film.viewing, watched }, keepalive)
      .then((response) => {
        if (!response.ok) missed();
      })
      .catch(missed);
  }

  /** The end card: the next video, counting down to it unless autoplay is off or nobody seems to be there. */
  private showUpNext(): void {
    const next = this.film?.next;
    if (!next) return;
    this.next.hidden = true;
    this.stillWatching = this.autoplay.checked && this.autoplays >= STILL_WATCHING_AFTER;
    const image = this.upNext.querySelector<HTMLImageElement>("[data-flix-upnext-image]")!;
    image.hidden = !next.image;
    if (next.image) image.src = next.image;
    const text = (name: string, value: string) => {
      const element = this.upNext.querySelector<HTMLElement>(`[data-flix-upnext-${name}]`)!;
      element.textContent = value;
      element.hidden = !value;
    };
    text("kicker", this.stillWatching ? "Ser du stadig med?" : next.kind === "episode" ? "Næste afsnit" : "Næste film");
    text("title", next.title);
    text("label", next.label);
    text("description", next.description);
    this.upNextCancel.textContent = this.stillWatching ? "Stop" : "Annuller";
    const playLabel = this.upNext.querySelector<HTMLElement>("[data-flix-upnext-play-label]")!;
    playLabel.textContent = this.stillWatching ? "Fortsæt" : "Afspil nu";
    const counting = this.autoplay.checked && !this.stillWatching;
    this.upNextRing.toggleAttribute("hidden", !counting);
    this.upNext.hidden = false;
    this.upNext.style.setProperty("--flix-countdown", `${NEXT_COUNTDOWN_SECONDS}s`);
    this.upNext.classList.toggle("is-counting", counting);
    this.upNextPlay.focus({ preventScroll: true });
    if (!counting) return;
    let seconds = NEXT_COUNTDOWN_SECONDS;
    const tick = () => {
      playLabel.textContent = `Afspil nu (${seconds})`;
      if (seconds-- > 0) {
        this.countdown = window.setTimeout(tick, 1000);
        return;
      }
      this.autoplays++;
      void this.playNext();
    };
    tick();
  }

  private hideUpNext(): void {
    window.clearTimeout(this.countdown);
    this.upNext.hidden = true;
    this.upNext.classList.remove("is-counting");
  }

  /** The next video, in the same element, after the intro again. */
  private async playNext(): Promise<void> {
    const next = this.film?.next;
    if (!next) return;
    // Saved first, so the film that just ended counts as watched when the next one picks its own successor.
    const saved = this.savePosition();
    this.playIntro();
    await saved;
    try {
      const film = await fetchFilm(next.url);
      history.replaceState(history.state, "", next.url);
      this.setFilm(film);
    } catch {
      window.location.href = next.url;
    }
  }

  /** Away from the player: back where "Afspil" was clicked, or to the video's page. */
  leave(): void {
    if (this.overlay) history.back();
    else window.location.href = this.film?.backUrl ?? this.back.href;
  }

  close(): void {
    this.savePosition(true);
    this.closed = true;
    this.hideUpNext();
    window.clearTimeout(this.idleTimer);
    window.clearTimeout(this.spinnerTimer);
    window.clearTimeout(this.tapTimer);
    this.listeners.abort();
    this.video.pause();
    this.video.removeAttribute("src");
    this.video.load();
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined);
    if (document.pictureInPictureElement === this.video) void document.exitPictureInPicture().catch(() => undefined);
    this.root.remove();
    body.classList.remove("flix-player-open");
  }

  // --- Events -----------------------------------------------------------------------------------------

  private bind(): void {
    const { video } = this;
    const signal = this.listeners.signal;
    this.start.addEventListener("click", () => {
      this.start.hidden = true;
      void video.play();
    });
    this.skip.addEventListener("click", () => this.startFilm());
    this.back.addEventListener("click", (event) => {
      event.preventDefault();
      this.leave();
    });
    this.next.addEventListener("click", (event) => {
      event.preventDefault();
      void this.playNext();
    });
    this.upNextPlay.addEventListener("click", () => void this.playNext());
    this.upNextCancel.addEventListener("click", () => {
      if (this.stillWatching) this.leave();
      else this.hideUpNext();
    });
    this.autoplay.addEventListener("change", () => {
      // Let Space go back to pausing the film rather than flipping the switch again.
      this.autoplay.blur();
      store(AUTOPLAY_KEY, this.autoplay.checked ? "on" : "off");
      if (!this.upNext.hidden) {
        this.hideUpNext();
        this.showUpNext();
      }
    });
    // Anyone pressing anything is still there; a mouse drifting over the screen does not count.
    for (const name of ["keydown", "pointerdown"]) {
      document.addEventListener(name, () => (this.autoplays = 0), { capture: true, passive: true, signal });
    }

    // The controls.
    this.toggle.addEventListener("click", () => this.togglePlay());
    for (const button of this.root.querySelectorAll<HTMLElement>("[data-flix-seek]")) {
      button.addEventListener("click", () => this.seekBy(Number(button.dataset.flixSeek)));
    }
    this.mute.addEventListener("click", () => {
      if (video.muted || video.volume === 0) {
        video.muted = false;
        if (video.volume === 0) video.volume = 0.5;
      } else {
        video.muted = true;
      }
    });
    this.volume.addEventListener("input", () => this.setVolume(Number(this.volume.value)));
    video.addEventListener("volumechange", () => {
      this.showSound();
      store(VOLUME_KEY, JSON.stringify({ volume: video.volume, muted: video.muted }));
    });
    this.fullscreen.addEventListener("click", () => this.toggleFullscreen());
    document.addEventListener(
      "fullscreenchange",
      () => this.root.classList.toggle("is-fullscreen", document.fullscreenElement === this.root),
      { signal },
    );
    this.pip.addEventListener("click", () => {
      if (document.pictureInPictureElement) void document.exitPictureInPicture().catch(() => undefined);
      else if (this.phase === "film") void video.requestPictureInPicture().catch(() => undefined);
    });
    // iOS's own fullscreen player draws the subtitles itself; ours are behind it.
    video.addEventListener("webkitbeginfullscreen", () => this.showTrack());
    video.addEventListener("webkitendfullscreen", () => window.setTimeout(() => this.showTrack()));

    for (const button of this.root.querySelectorAll<HTMLElement>("[data-flix-menu-button]")) {
      button.addEventListener("click", () => {
        const name = button.dataset.flixMenuButton!;
        this.openMenu(button.getAttribute("aria-expanded") === "true" ? "" : name);
      });
    }
    this.controls.addEventListener("click", (event) => {
      const item = (event.target as HTMLElement).closest<HTMLElement>(".flix-popup-item");
      if (!item) return;
      const { kind, value = "" } = item.dataset;
      if (kind === "subtitles") {
        this.chooseTrack(this.film?.subtitles.find((track) => String(track.id) === value) ?? null);
      } else if (kind === "speed") {
        this.speed = Number(value);
        video.defaultPlaybackRate = this.speed;
        if (this.phase === "film") video.playbackRate = this.speed;
        this.check(this.speeds, value);
      } else if (kind === "quality" && value !== this.quality && this.phase === "film") {
        this.quality = value;
        store(QUALITY_KEY, value);
        this.load(video.currentTime, !video.paused);
      }
      this.closeMenus();
      this.toggle.focus({ preventScroll: true });
    });
    document.addEventListener(
      "pointerdown",
      (event) => {
        if (!(event.target as HTMLElement).closest(".flix-popup-wrap")) this.closeMenus();
      },
      { signal },
    );

    // The timeline: hover shows the moment, a press or a drag goes there.
    this.timeline.addEventListener("pointermove", (event) => {
      const at = this.timeAt(event);
      this.tip.hidden = false;
      if (this.scrubbing) {
        this.showTime(at);
        this.seekTo(at);
      }
    });
    this.timeline.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || this.phase !== "film") return;
      this.scrubbing = true;
      this.root.classList.add("is-scrubbing");
      this.timeline.setPointerCapture(event.pointerId);
      const at = this.timeAt(event);
      this.tip.hidden = false;
      this.seekTo(at);
    });
    const endScrub = () => {
      this.scrubbing = false;
      this.root.classList.remove("is-scrubbing");
      this.tip.hidden = true;
      this.wake();
    };
    this.timeline.addEventListener("pointerup", endScrub);
    this.timeline.addEventListener("pointercancel", endScrub);
    this.timeline.addEventListener("pointerleave", () => {
      if (!this.scrubbing) this.tip.hidden = true;
    });
    this.timeline.addEventListener("keydown", (event) => {
      if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        this.seekTo(event.key === "Home" ? 0 : video.duration);
      }
    });

    // The picture: a click plays or pauses and a double click is fullscreen; a finger is tap().
    video.addEventListener("pointerdown", (event) => (this.lastPointer = event.pointerType), { passive: true });
    video.addEventListener("click", (event) => {
      if (this.lastPointer === "touch" || this.lastPointer === "pen") this.tap(event);
      else if (this.phase === "film" && !this.closeMenus()) this.togglePlay(true);
    });
    video.addEventListener("dblclick", () => {
      if (this.lastPointer === "mouse") this.toggleFullscreen();
    });

    video.addEventListener("play", () => {
      this.root.classList.remove("is-paused");
      this.toggle.setAttribute("aria-label", "Pause");
      this.toggle.title = "Pause (k)";
      this.hideUpNext();
      this.wake();
    });
    video.addEventListener("pause", () => {
      this.root.classList.add("is-paused");
      this.toggle.setAttribute("aria-label", "Afspil");
      this.toggle.title = "Afspil (k)";
      this.wake();
    });
    // A spinner while the film waits for more of itself, unless that is over at once.
    video.addEventListener("waiting", () => {
      window.clearTimeout(this.spinnerTimer);
      this.spinnerTimer = window.setTimeout(() => (this.spinner.hidden = false), 400);
    });
    for (const name of ["playing", "canplay", "pause", "seeked", "error", "emptied"]) {
      video.addEventListener(name, () => {
        window.clearTimeout(this.spinnerTimer);
        this.spinner.hidden = true;
      });
    }
    for (const name of ["durationchange", "progress", "seeked", "loadedmetadata"]) {
      video.addEventListener(name, () => {
        if (this.phase === "film" && !this.scrubbing) this.showTime();
      });
    }

    video.addEventListener("ended", () => {
      if (this.phase === "intro") {
        this.startFilm();
        return;
      }
      void this.savePosition();
      // iOS plays fullscreen in its own player, over the page: leave it, or the end card is never seen.
      if (this.film?.next && video.webkitDisplayingFullscreen) video.webkitExitFullscreen?.();
      this.showUpNext();
    });

    video.addEventListener("error", () => {
      if (this.closed) return;
      if (this.phase === "intro") {
        // A missing intro must never stand between anyone and the film.
        this.startFilm();
        return;
      }
      // Most likely the signed URL ran out during a long film: ask for a fresh one, where we were.
      if (this.retries++ < 3) {
        this.load(this.filmTime, true);
        return;
      }
      this.errorBox.textContent = "Videoen kunne ikke afspilles. Prøv at genindlæse siden.";
      this.errorBox.hidden = false;
    });

    video.addEventListener("seeking", () => (this.lastTime = null));
    video.addEventListener("timeupdate", () => {
      if (this.phase !== "film" || !this.film) return;
      const now = video.currentTime;
      this.filmTime = now;
      if (!this.scrubbing) this.showTime(now);
      if (this.lastTime !== null) {
        const step = now - this.lastTime;
        // Real seconds of the film, whatever the speed: 2× plays two of them a second.
        if (step > 0 && step < 1.5 * Math.max(1, video.playbackRate)) {
          this.watched += step;
          this.unreported += step;
        }
      }
      this.lastTime = now;
      if (
        !this.counted &&
        (this.watched >= VIEW_AFTER_SECONDS || (video.duration > 0 && this.watched >= video.duration / 2))
      ) {
        this.counted = true;
        void postJSON(this.film.viewUrl, { viewing: this.film.viewing }).catch(() => (this.counted = false));
      }
      if (Date.now() - this.lastSaved > SAVE_EVERY_MS && !video.paused) this.savePosition();
      // "Næste afsnit" in the last 20 seconds of an episode; gone again if the timeline goes back.
      this.next.hidden = !(
        this.film.next?.kind === "episode" &&
        this.upNext.hidden &&
        video.duration &&
        video.duration - now < 20
      );
    });
    video.addEventListener("pause", () => this.savePosition());
    window.addEventListener("pagehide", () => this.savePosition(true), { signal });
    document.addEventListener(
      "visibilitychange",
      () => {
        if (document.visibilityState === "hidden") this.savePosition(true);
      },
      { signal },
    );

    this.root.addEventListener("mousemove", () => this.wake(), { passive: true });
    this.root.addEventListener("keydown", () => this.wake(), { passive: true });
    this.controls.addEventListener("pointerdown", () => this.wake(), { passive: true });
    this.wake();

    document.addEventListener(
      "keydown",
      (event) => {
        if (event.ctrlKey || event.metaKey || event.altKey) return; // the browser's own shortcuts
        const target = event.target as HTMLElement;
        if (target.closest("input:not([type=range]), textarea, select")) return;
        const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
        // Space and Enter on a button press it; anything else is the player's.
        if ((key === " " || key === "Enter") && target.closest("button, a")) return;
        if (target.closest("input[type=range]") && key.startsWith("Arrow")) return; // the volume's own
        const film = this.phase === "film";
        if (key === " " || key === "k") {
          event.preventDefault();
          this.togglePlay(true);
        } else if (film && (key === "ArrowLeft" || key === "j")) {
          event.preventDefault();
          this.seekBy(-JUMP_SECONDS);
        } else if (film && (key === "ArrowRight" || key === "l")) {
          event.preventDefault();
          this.seekBy(JUMP_SECONDS);
        } else if (key === "ArrowUp" || key === "ArrowDown") {
          event.preventDefault();
          this.setVolume((video.muted ? 0 : video.volume) + (key === "ArrowUp" ? 0.1 : -0.1));
        } else if (key === "f") {
          this.toggleFullscreen();
        } else if (key === "m") {
          video.muted = !video.muted;
        } else if (film && key === "c") {
          this.toggleSubtitles();
        } else if (key === "Escape" && this.closeMenus()) {
          this.toggle.focus({ preventScroll: true });
        } else if (key === "Escape" && !this.upNext.hidden) {
          this.hideUpNext();
        } else if (key === "Escape" && !document.fullscreenElement) {
          this.leave();
        }
      },
      { signal },
    );
  }
}

/** The play page, opened directly. */
function initPlayPage(): void {
  const root = document.querySelector<HTMLElement>("main [data-flix-player]");
  const film = jsonConfig<FilmConfig>("flix-player-config");
  if (!root || !film) return;
  const player = new Player(root, false);
  player.setFilm(film);
  player.playIntro();
}

let overlayPlayer: Player | null = null;

/** "Afspil" anywhere in GAHKFlix: the player opens over the page, the intro inside the click. */
function initPlayLinks(): void {
  const template = document.getElementById("flix-player-template") as HTMLTemplateElement | null;
  if (!template) return;
  document.addEventListener("click", (event) => {
    const link = (event.target as HTMLElement).closest<HTMLAnchorElement>("a[data-flix-play]");
    if (!link || event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; // a new tab: let it go
    event.preventDefault();
    overlayPlayer?.close();
    const root = template.content.firstElementChild!.cloneNode(true) as HTMLElement;
    body.append(root);
    body.classList.add("flix-player-open");
    const player = new Player(root, true);
    overlayPlayer = player;
    player.playIntro();
    history.pushState({ flixPlayer: true }, "", link.href);
    fetchFilm(link.href)
      .then((film) => {
        if (overlayPlayer === player) player.setFilm(film);
      })
      .catch(() => {
        window.location.href = link.href;
      });
  });
  window.addEventListener("popstate", () => {
    if (overlayPlayer && !history.state?.flixPlayer) {
      overlayPlayer.close();
      overlayPlayer = null;
    } else if (!overlayPlayer && history.state?.flixPlayer) {
      // Forward again into a player that was closed: load it as a page.
      window.location.reload();
    }
  });
}

if (body.classList.contains("flix")) {
  initHeader();
  initRows();
  initSeasons();
  initCharts();
  initForms();
  initLiveSearch();
  initLiveLists();
  initUpload();
  initProcessing();
  initDetailsKeeper(); // before the editors, which read what it puts back
  initPremiere();
  initDateGuess();
  initCastEditor();
  initTagEditor();
  initNumberPickers();
  initDigitFields();
  initNewSeries();
  initNewFrames();
  initScrubber();
  initPlayPage();
  initPlayLinks();
}
