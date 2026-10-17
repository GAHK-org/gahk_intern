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

type UploadConfig = { beginUrl: string; commitUrl: string; video: number | null; maxBytes: number };
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

function initUpload(): void {
  const root = document.querySelector<HTMLElement>("[data-flix-upload]");
  const config = jsonConfig<UploadConfig>("flix-upload-config");
  if (!root || !config) return;
  const drop = root.querySelector<HTMLElement>("[data-flix-drop]")!;
  const input = root.querySelector<HTMLInputElement>("[data-flix-file]")!;
  const progress = root.querySelector<HTMLElement>("[data-flix-progress]")!;
  const meter = root.querySelector<HTMLElement>("[data-flix-meter]")!;
  const status = root.querySelector<HTMLElement>("[data-flix-upload-status]")!;
  let busy = false;

  const say = (text: string, error = false) => {
    status.textContent = text;
    status.classList.toggle("is-error", error);
  };
  const guard = (event: BeforeUnloadEvent) => {
    event.preventDefault();
  };

  async function upload(file: File): Promise<void> {
    if (busy) return;
    if (!window.crypto?.subtle) {
      say("Din browser kan ikke uploade her. Brug en opdateret browser over https.", true);
      return;
    }
    if (file.size > config!.maxBytes) {
      say(`Videoen er for stor (${formatBytes(file.size)}; højst ${formatBytes(config!.maxBytes)}).`, true);
      return;
    }
    busy = true;
    drop.hidden = true;
    progress.hidden = false;
    progress.querySelector("[data-flix-filename]")!.textContent = file.name;
    progress.querySelector("[data-flix-filesize]")!.textContent = formatBytes(file.size);
    meter.style.width = "0%";
    window.addEventListener("beforeunload", guard);
    try {
      say("Tjekker filen …");
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
        meter.style.width = `${(fraction * 100).toFixed(1)}%`;
        const seconds = (performance.now() - started) / 1000;
        const rate = loaded / Math.max(seconds, 0.001);
        const left = rate > 0 ? (total - loaded) / rate : 0;
        say(
          `Sender … ${Math.floor(fraction * 100)} % · ${formatBytes(rate)}/s` +
            (fraction < 1 && seconds > 3 ? ` · ca. ${formatSeconds(left)} tilbage` : ""),
        );
      });
      if (statusCode < 200 || statusCode >= 300) throw new Error("Lageret afviste filen. Prøv igen.");

      say("Gemmer …");
      const committed = await postJSON(config!.commitUrl, { token });
      if (!committed.ok) throw new Error(await errorOf(committed, "Uploaden kunne ikke gemmes."));
      const { editUrl } = await committed.json();
      window.removeEventListener("beforeunload", guard);
      say("Færdig. Videoen behandles nu …");
      window.location.href = editUrl;
    } catch (error) {
      window.removeEventListener("beforeunload", guard);
      busy = false;
      drop.hidden = false;
      progress.hidden = true;
      input.value = "";
      say(error instanceof Error ? error.message : "Uploaden mislykkedes.", true);
    }
  }

  input.addEventListener("change", () => {
    const file = input.files?.[0];
    if (file) void upload(file);
  });
  drop.addEventListener("dragover", (event) => {
    event.preventDefault();
    drop.classList.add("is-over");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("is-over");
    const file = event.dataTransfer?.files?.[0];
    if (file) void upload(file);
  });
}

/** The edit page while a file is processed: follow its progress, reload when it is done. */
function initProcessing(): void {
  const panel = document.querySelector<HTMLElement>("[data-flix-processing]");
  if (!panel) return;
  const meter = panel.querySelector<HTMLElement>("[data-flix-meter]");
  const text = panel.querySelector<HTMLElement>("[data-flix-processing-status]");
  const poll = async () => {
    try {
      const response = await fetch(panel.dataset.flixProcessing!, { credentials: "same-origin" });
      if (response.ok) {
        const data = (await response.json()) as { state: string; progress: number };
        if (data.state === "ready" || data.state === "failed" || data.state === "") {
          window.location.reload();
          return;
        }
        if (meter) meter.style.width = `${data.progress}%`;
        if (text) text.textContent = data.state === "queued" ? "I kø …" : `${data.progress} % færdig`;
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

// --- The player -----------------------------------------------------------------------------------

type PlayerConfig = {
  streams: Record<string, string>;
  viewUrl: string;
  positionUrl: string;
  nextUrl: string;
  backUrl: string;
};

const QUALITY_KEY = "gahkflix-quality";
// A view counts once this much of the film has actually played (or half of a very short one).
const VIEW_AFTER_SECONDS = 5;
const SAVE_EVERY_MS = 15000;

function storedQuality(): string | null {
  try {
    return window.localStorage.getItem(QUALITY_KEY);
  } catch {
    return null;
  }
}

function initPlayer(): void {
  const root = document.querySelector<HTMLElement>("[data-flix-player]");
  const config = jsonConfig<PlayerConfig>("flix-player-config");
  const video = root?.querySelector<HTMLVideoElement>("video");
  if (!root || !config || !video) return;
  const skip = root.querySelector<HTMLButtonElement>("[data-flix-skip]")!;
  const start = root.querySelector<HTMLButtonElement>("[data-flix-start]")!;
  const errorBox = root.querySelector<HTMLElement>("[data-flix-error]")!;
  const next = root.querySelector<HTMLAnchorElement>("[data-flix-next]");
  const qualityBar = root.querySelector<HTMLElement>("[data-flix-quality]");
  const qualities = Object.keys(config.streams);
  const narrow = window.matchMedia("(max-width: 900px)").matches;
  let quality = storedQuality() ?? "";
  if (!qualities.includes(quality)) quality = narrow && qualities.includes("hd") ? "hd" : qualities[0];

  let phase: "intro" | "film" = "intro";
  let watched = 0;
  let lastTime: number | null = null;
  let counted = false;
  let retries = 0;
  let lastSaved = 0;
  let filmTime = Number(video.dataset.start || 0);

  const tryPlay = () => video.play().catch(() => (start.hidden = false));
  start.addEventListener("click", () => {
    start.hidden = true;
    void video.play();
  });

  const markQuality = () => {
    for (const button of qualityBar?.querySelectorAll<HTMLButtonElement>("[data-quality]") ?? []) {
      button.setAttribute("aria-pressed", String(button.dataset.quality === quality));
    }
  };

  const load = (at: number, play: boolean) => {
    video.src = config.streams[quality];
    video.addEventListener(
      "loadedmetadata",
      () => {
        if (at > 1 && (!video.duration || at < video.duration - 2)) video.currentTime = at;
      },
      { once: true },
    );
    markQuality();
    if (play) tryPlay();
  };

  const startFilm = () => {
    if (phase === "film") return;
    phase = "film";
    skip.hidden = true;
    if (qualityBar) qualityBar.hidden = false;
    video.controls = true;
    load(filmTime, true);
  };

  const savePosition = (keepalive = false) => {
    if (phase !== "film" || !video.duration) return;
    lastSaved = Date.now();
    void postJSON(config.positionUrl, { position: video.currentTime }, keepalive).catch(() => undefined);
  };

  // The intro, unless it cannot be had — a missing file must never stand between anyone and the film.
  video.controls = false;
  video.src = video.dataset.intro ?? "";
  skip.hidden = false;
  skip.addEventListener("click", startFilm);
  tryPlay();

  video.addEventListener("ended", () => {
    if (phase === "intro") {
      startFilm();
      return;
    }
    savePosition();
    if (next && config.nextUrl) {
      let seconds = 8;
      next.hidden = false;
      const tick = () => {
        next.textContent = `Næste afsnit om ${seconds} …`;
        if (seconds-- <= 0) window.location.href = config.nextUrl;
        else window.setTimeout(tick, 1000);
      };
      tick();
    }
  });

  video.addEventListener("error", () => {
    if (phase === "intro") {
      startFilm();
      return;
    }
    // Most likely the signed URL ran out during a long film: ask for a fresh one, where we were.
    if (retries++ < 3) {
      load(filmTime, true);
      return;
    }
    errorBox.textContent = "Videoen kunne ikke afspilles. Prøv at genindlæse siden.";
    errorBox.hidden = false;
  });

  video.addEventListener("seeking", () => (lastTime = null));
  video.addEventListener("timeupdate", () => {
    if (phase !== "film") return;
    const now = video.currentTime;
    filmTime = now;
    if (lastTime !== null) {
      const step = now - lastTime;
      if (step > 0 && step < 1.5) watched += step;
    }
    lastTime = now;
    if (!counted && (watched >= VIEW_AFTER_SECONDS || (video.duration > 0 && watched >= video.duration / 2))) {
      counted = true;
      void postJSON(config.viewUrl, {}).catch(() => (counted = false));
    }
    if (Date.now() - lastSaved > SAVE_EVERY_MS && !video.paused) savePosition();
    if (next && config.nextUrl && video.duration && video.duration - now < 20) next.hidden = false;
  });
  video.addEventListener("pause", () => savePosition());
  window.addEventListener("pagehide", () => savePosition(true));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") savePosition(true);
  });

  qualityBar?.addEventListener("click", (event) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-quality]");
    if (!button || button.dataset.quality === quality || phase !== "film") return;
    quality = button.dataset.quality!;
    try {
      window.localStorage.setItem(QUALITY_KEY, quality);
    } catch {
      // Private mode: the choice just is not remembered.
    }
    load(video.currentTime, !video.paused);
  });

  // The title bar and the quality switch fade out while the film plays undisturbed.
  let idleTimer = 0;
  const wake = () => {
    root.classList.remove("is-idle");
    window.clearTimeout(idleTimer);
    idleTimer = window.setTimeout(() => {
      if (!video.paused) root.classList.add("is-idle");
    }, 3000);
  };
  for (const name of ["mousemove", "touchstart", "keydown"]) root.addEventListener(name, wake, { passive: true });
  video.addEventListener("pause", wake);
  wake();

  document.addEventListener("keydown", (event) => {
    if ((event.target as HTMLElement).closest("input, textarea, select, button")) return;
    if (event.key === " " || event.key === "k") {
      event.preventDefault();
      if (video.paused) void video.play();
      else video.pause();
    } else if (event.key === "f") {
      if (document.fullscreenElement) void document.exitFullscreen();
      else void root.requestFullscreen?.().catch(() => undefined);
    } else if (phase === "film" && (event.key === "ArrowRight" || event.key === "ArrowLeft")) {
      if (event.target === video) return; // the native controls already do it
      video.currentTime = Math.max(0, video.currentTime + (event.key === "ArrowRight" ? 10 : -10));
    } else if (event.key === "Escape" && !document.fullscreenElement) {
      window.location.href = config.backUrl;
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
  initUpload();
  initProcessing();
  initCastEditor();
  initTagEditor();
  initPlayer();
}
