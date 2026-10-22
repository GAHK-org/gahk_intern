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
    input.addEventListener("input", () => {
      const digits = input.value.replace(/\D+/g, "").slice(0, input.maxLength > 0 ? input.maxLength : 3);
      if (digits !== input.value) input.value = digits;
    });
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
  startAt: number;
  viewUrl: string;
  positionUrl: string;
  playUrl: string;
  backUrl: string;
  next: UpNext | null;
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

function storedQuality(): string | null {
  try {
    return window.localStorage.getItem(QUALITY_KEY);
  } catch {
    return null;
  }
}

function storedAutoplay(): boolean {
  try {
    return window.localStorage.getItem(AUTOPLAY_KEY) !== "off";
  } catch {
    return true;
  }
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

class Player {
  private video: HTMLVideoElement;
  private skip: HTMLButtonElement;
  private start: HTMLButtonElement;
  private errorBox: HTMLElement;
  private next: HTMLAnchorElement;
  private back: HTMLAnchorElement;
  private qualityBar: HTMLElement;
  private autoplay: HTMLInputElement;
  private upNext: HTMLElement;
  private upNextPlay: HTMLButtonElement;
  private upNextCancel: HTMLButtonElement;
  private upNextRing: SVGElement;
  private stillWatching = false;
  private autoplays = 0;
  private film: FilmConfig | null = null;
  private phase: "intro" | "film" = "intro";
  private filmWanted = false;
  private quality = "";
  private filmTime = 0;
  private watched = 0;
  private lastTime: number | null = null;
  private counted = false;
  private retries = 0;
  private lastSaved = 0;
  private countdown = 0;
  private idleTimer = 0;
  private closed = false;
  private listeners = new AbortController();

  constructor(
    private root: HTMLElement,
    private overlay: boolean,
  ) {
    this.video = root.querySelector("video")!;
    this.skip = root.querySelector("[data-flix-skip]")!;
    this.start = root.querySelector("[data-flix-start]")!;
    this.errorBox = root.querySelector("[data-flix-error]")!;
    this.next = root.querySelector("[data-flix-next]")!;
    this.back = root.querySelector("[data-flix-back]")!;
    this.qualityBar = root.querySelector("[data-flix-quality]")!;
    this.autoplay = root.querySelector("[data-flix-autoplay]")!;
    this.autoplay.checked = storedAutoplay();
    this.upNext = root.querySelector("[data-flix-upnext]")!;
    this.upNextPlay = root.querySelector("[data-flix-upnext-play]")!;
    this.upNextCancel = root.querySelector("[data-flix-upnext-cancel]")!;
    this.upNextRing = root.querySelector("[data-flix-upnext-ring]")!;
    this.bind();
  }

  /** The intro. Called straight from the click, so the browser lets it play with sound. */
  playIntro(): void {
    this.phase = "intro";
    this.filmWanted = false;
    this.video.controls = false;
    this.qualityBar.hidden = true;
    this.next.hidden = true;
    this.errorBox.hidden = true;
    this.hideUpNext();
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
    this.lastTime = null;
    this.counted = false;
    this.retries = 0;
    this.root.querySelector("[data-flix-title]")!.textContent = film.title;
    this.root.querySelector("[data-flix-subtitle]")!.textContent = film.subtitle;
    document.title = `${film.subtitle || film.title} – GAHKFlix`;
    if (!this.overlay) this.back.href = film.backUrl;
    const qualities = Object.keys(film.streams);
    this.quality = storedQuality() ?? "";
    if (!qualities.includes(this.quality)) {
      const narrow = window.matchMedia("(max-width: 900px)").matches;
      this.quality = narrow && qualities.includes("hd") ? "hd" : qualities[0];
    }
    this.qualityBar.replaceChildren(
      ...(qualities.length > 1 ? qualities : []).map((quality) => {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.quality = quality;
        button.textContent = film.qualityLabels[quality] ?? quality;
        return button;
      }),
    );
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
    this.qualityBar.hidden = this.qualityBar.childElementCount === 0;
    this.video.controls = true;
    this.load(this.filmTime, true);
  }

  private load(at: number, play: boolean): void {
    if (!this.film) return;
    this.video.src = this.film.streams[this.quality];
    this.video.addEventListener(
      "loadedmetadata",
      () => {
        if (at > 1 && (!this.video.duration || at < this.video.duration - 2)) this.video.currentTime = at;
      },
      { once: true },
    );
    for (const button of this.qualityBar.querySelectorAll<HTMLButtonElement>("[data-quality]")) {
      button.setAttribute("aria-pressed", String(button.dataset.quality === this.quality));
    }
    if (play) this.tryPlay();
  }

  private savePosition(keepalive = false): Promise<unknown> {
    if (this.phase !== "film" || !this.film || !this.video.duration) return Promise.resolve();
    this.lastSaved = Date.now();
    return postJSON(this.film.positionUrl, { position: this.video.currentTime }, keepalive).catch(() => undefined);
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
    this.listeners.abort();
    this.video.pause();
    this.video.removeAttribute("src");
    this.video.load();
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined);
    this.root.remove();
    body.classList.remove("flix-player-open");
  }

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
      try {
        window.localStorage.setItem(AUTOPLAY_KEY, this.autoplay.checked ? "on" : "off");
      } catch {
        // Private mode: the choice just is not remembered.
      }
      if (!this.upNext.hidden) {
        this.hideUpNext();
        this.showUpNext();
      }
    });
    // Anyone pressing anything is still there; a mouse drifting over the screen does not count.
    for (const name of ["keydown", "pointerdown"]) {
      document.addEventListener(name, () => (this.autoplays = 0), { capture: true, passive: true, signal });
    }

    video.addEventListener("ended", () => {
      if (this.phase === "intro") {
        this.startFilm();
        return;
      }
      void this.savePosition();
      // iOS plays fullscreen in its own player, over the page: leave it, or the end card is never seen.
      const ios = video as HTMLVideoElement & { webkitDisplayingFullscreen?: boolean; webkitExitFullscreen?: () => void };
      if (this.film?.next && ios.webkitDisplayingFullscreen) ios.webkitExitFullscreen?.();
      this.showUpNext();
    });

    // The video's own fullscreen button shows the video alone; make it the whole player, end card and all.
    document.addEventListener(
      "fullscreenchange",
      () => {
        if (document.fullscreenElement === video) void this.root.requestFullscreen?.().catch(() => undefined);
      },
      { signal },
    );
    video.addEventListener("play", () => this.hideUpNext());

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
      if (this.lastTime !== null) {
        const step = now - this.lastTime;
        if (step > 0 && step < 1.5) this.watched += step;
      }
      this.lastTime = now;
      if (
        !this.counted &&
        (this.watched >= VIEW_AFTER_SECONDS || (video.duration > 0 && this.watched >= video.duration / 2))
      ) {
        this.counted = true;
        void postJSON(this.film.viewUrl, {}).catch(() => (this.counted = false));
      }
      if (Date.now() - this.lastSaved > SAVE_EVERY_MS && !video.paused) this.savePosition();
      if (this.film.next?.kind === "episode" && this.upNext.hidden && video.duration && video.duration - now < 20) {
        this.next.hidden = false;
      }
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

    this.qualityBar.addEventListener("click", (event) => {
      const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-quality]");
      if (!button || button.dataset.quality === this.quality || this.phase !== "film") return;
      this.quality = button.dataset.quality!;
      try {
        window.localStorage.setItem(QUALITY_KEY, this.quality);
      } catch {
        // Private mode: the choice just is not remembered.
      }
      this.load(video.currentTime, !video.paused);
    });

    // The title bar and the quality switch fade out while the film plays undisturbed.
    const wake = () => {
      this.root.classList.remove("is-idle");
      window.clearTimeout(this.idleTimer);
      this.idleTimer = window.setTimeout(() => {
        if (!video.paused) this.root.classList.add("is-idle");
      }, 3000);
    };
    for (const name of ["mousemove", "touchstart", "keydown"]) this.root.addEventListener(name, wake, { passive: true });
    video.addEventListener("pause", wake);
    wake();

    document.addEventListener(
      "keydown",
      (event) => {
        if ((event.target as HTMLElement).closest("input, textarea, select, button")) return;
        if (event.key === " " || event.key === "k") {
          event.preventDefault();
          if (video.paused) void video.play();
          else video.pause();
        } else if (event.key === "f") {
          if (document.fullscreenElement) void document.exitFullscreen();
          else void this.root.requestFullscreen?.().catch(() => undefined);
        } else if (this.phase === "film" && (event.key === "ArrowRight" || event.key === "ArrowLeft")) {
          if (event.target === video) return; // the native controls already do it
          video.currentTime = Math.max(0, video.currentTime + (event.key === "ArrowRight" ? 10 : -10));
        } else if (event.key === "Escape" && !this.upNext.hidden) {
          this.hideUpNext();
        } else if (event.key === "Escape" && !document.fullscreenElement) {
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
  initUpload();
  initProcessing();
  initCastEditor();
  initTagEditor();
  initNumberPickers();
  initScrubber();
  initPlayPage();
  initPlayLinks();
}
