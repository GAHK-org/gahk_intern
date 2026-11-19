// Drive's uploads (drive/views.py). Each file asks the server first (upload/begin), which answers
// with a ticket for the bucket, `{"direct": false}` when there is no bucket and the file goes with
// the request instead, or a 409 when a file of that name is there already. Then the bytes go
// straight to the bucket in parts (directupload.ts, shared with the photo album), and commit makes
// the rows. On a file's page (`versionOf`) the picked file becomes that file's next version,
// whatever it is called. Replacing a file somebody else changed after this resident downloaded it
// is a second 409 (`stale`), which asks again.
//
// Also: the bar for ticked items in a listing (move, copy, delete), and a confirm() before any
// form marked [data-drive-confirm].

import { DirectUnavailable, UploadRefused, sendToBucket, type DirectUpload } from "./directupload"
import { csrfToken } from "./htmx-csrf"

type Config = {
  beginUrl: string
  signUrl: string
  commitUrl: string
  directUrl: string
  maxBytes: number
  versionOf: string
  // On `Uden periode` / `Uden årstal`: the current period or year, offered first.
  instead?: { name: string; url: string; beginUrl: string; directUrl: string; createUrl: string } | null
}

type Dialogs = { conflict: HTMLDialogElement; stale: HTMLDialogElement }

const formatBytes = (bytes: number): string => {
  const units = ["B", "KB", "MB", "GB"]
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

async function post(url: string, fields: Record<string, string | Blob>): Promise<{ status: number; body: Record<string, unknown> }> {
  const body = new FormData()
  for (const [key, value] of Object.entries(fields)) body.append(key, value)
  const response = await fetch(url, {
    method: "POST",
    body,
    credentials: "same-origin",
    headers: { Accept: "application/json", "X-CSRFToken": csrfToken() },
  })
  return { status: response.status, body: ((await response.json().catch(() => null)) ?? {}) as Record<string, unknown> }
}

/** Directly in `Uden periode` / `Uden årstal`, or in the current period or year? "instead", "here"
 * or null to cancel. */
function askTimeless(): Promise<string | null> {
  const dialog = document.querySelector<HTMLDialogElement>("[data-drive-timeless]")
  if (!dialog) return Promise.resolve("here")
  dialog.returnValue = ""
  dialog.showModal()
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue || null), { once: true })
  })
}

/** Ask what to do: "replace" (a new version), "keep_both", or null to skip. */
function ask(dialog: HTMLDialogElement, text: string): Promise<string | null> {
  dialog.querySelector("p")!.textContent = text
  dialog.returnValue = ""
  dialog.showModal()
  return new Promise((resolve) => {
    dialog.addEventListener(
      "close",
      () => resolve(["replace", "keep_both"].includes(dialog.returnValue) ? dialog.returnValue : null),
      { once: true },
    )
  })
}

async function uploadOne(config: Config, dialogs: Dialogs, file: File, say: (text: string) => void): Promise<string> {
  if (file.size > config.maxBytes) return `${file.name}: for stor (højst ${formatBytes(config.maxBytes)}).`
  const name = config.versionOf || file.name
  let onConflict = config.versionOf ? "replace" : ""
  let ignoreStale = ""
  for (;;) {
    const begun = await post(config.beginUrl, {
      name,
      size: String(file.size),
      type: file.type,
      on_conflict: onConflict,
      ignore_stale: ignoreStale,
    })
    if (begun.status === 409 && begun.body.conflict && !onConflict) {
      const choice = await ask(dialogs.conflict, `Der er allerede en fil, der hedder "${name}".`)
      if (choice === null) return `${name}: sprunget over.`
      onConflict = choice
      continue
    }
    if (begun.status === 409 && begun.body.stale && !ignoreStale) {
      const choice = await ask(dialogs.stale, String(begun.body.error))
      if (choice === null) return `${name}: annulleret.`
      onConflict = choice
      ignoreStale = "1"
      continue
    }
    if (begun.status >= 400) return `${file.name}: ${String(begun.body.error ?? "kunne ikke lægges op.")}`
    if (begun.body.direct) {
      const upload = begun.body as unknown as DirectUpload
      try {
        await sendToBucket(file, upload, config.signUrl, (sent) =>
          say(`${file.name}: ${Math.floor((sent / Math.max(file.size, 1)) * 100)} %`),
        )
      } catch (error) {
        if (error instanceof UploadRefused) return `${file.name}: ${error.message}`
        if (error instanceof DirectUnavailable) return `${file.name}: lageret kunne ikke nås.`
        return `${file.name}: uploaden mislykkedes.`
      }
      const committed = await post(config.commitUrl, { ticket: upload.ticket })
      return committed.status < 300 ? "" : `${file.name}: ${String(committed.body.error ?? "kunne ikke gemmes.")}`
    }
    say(`${file.name}: sender …`)
    const sent = await post(config.directUrl, { file, name, on_conflict: onConflict, ignore_stale: ignoreStale })
    return sent.status < 300 ? "" : `${file.name}: ${String(sent.body.error ?? "kunne ikke lægges op.")}`
  }
}

function initUpload(): void {
  const root = document.querySelector<HTMLElement>("[data-drive-upload]")
  const raw = document.getElementById("drive-upload-config")?.textContent
  if (!root || !raw) return
  const config = JSON.parse(raw) as Config
  const input = root.querySelector<HTMLInputElement>("[data-drive-files]")!
  const status = root.querySelector<HTMLElement>("[data-upload-status]")!
  const dialogs = {
    conflict: root.querySelector<HTMLDialogElement>("[data-drive-conflict]")!,
    stale: root.querySelector<HTMLDialogElement>("[data-drive-stale]")!,
  }
  const say = (text: string) => {
    status.textContent = text
  }
  const guard = (event: BeforeUnloadEvent) => event.preventDefault()

  input.addEventListener("change", async () => {
    const files = Array.from(input.files ?? [])
    if (!files.length) return
    let target = config
    let after = ""
    if (config.instead) {
      const choice = await askTimeless()
      if (choice === null) {
        input.value = ""
        return
      }
      if (choice === "instead") {
        target = { ...config, beginUrl: config.instead.beginUrl, directUrl: config.instead.directUrl }
        after = config.instead.url
      }
    }
    input.disabled = true
    window.addEventListener("beforeunload", guard)
    const problems: string[] = []
    for (const [index, file] of files.entries()) {
      say(`${index + 1} af ${files.length}: ${file.name} …`)
      const problem = await uploadOne(target, dialogs, file, say)
      if (problem) problems.push(problem)
    }
    window.removeEventListener("beforeunload", guard)
    if (problems.length) {
      input.disabled = false
      input.value = ""
      say(problems.join(" "))
      if (problems.length < files.length) window.setTimeout(() => (after ? window.location.assign(after) : window.location.reload()), 4000)
      return
    }
    if (after) window.location.assign(after)
    else window.location.reload()
  })
}

/** The bar for ticked items: how many, and move, copy or delete them. */
function initSelection(): void {
  const bar = document.querySelector<HTMLElement>("[data-drive-selection]")
  const raw = document.getElementById("drive-selection-config")?.textContent
  if (!bar || !raw) return
  const config = JSON.parse(raw) as { moveUrl: string; deleteUrl: string }
  const ticks = Array.from(document.querySelectorAll<HTMLInputElement>("input[data-drive-tick]"))
  const count = bar.querySelector<HTMLElement>("[data-selection-count]")!
  const ticked = () => ticks.filter((tick) => tick.checked)
  const update = () => {
    const n = ticked().length
    bar.hidden = n === 0
    count.textContent = `${n} valgt`
  }
  for (const tick of ticks) tick.addEventListener("change", update)
  update()

  bar.addEventListener("click", (event) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-selection-op]")
    if (!button) return
    const op = button.dataset.selectionOp!
    const items = ticked().map((tick): [string, string] => [tick.dataset.driveTick!, tick.value])
    if (op === "delete") {
      if (!window.confirm(`Læg ${items.length} i papirkurven?`)) return
      const form = document.createElement("form")
      form.method = "post"
      form.action = config.deleteUrl
      const fields: [string, string][] = [["csrfmiddlewaretoken", csrfToken()], ...items]
      for (const [name, value] of fields) {
        const input = document.createElement("input")
        input.type = "hidden"
        input.name = name
        input.value = value
        form.append(input)
      }
      document.body.append(form)
      form.submit()
      return
    }
    const query = new URLSearchParams([["op", op], ...items])
    window.location.href = `${config.moveUrl}?${query.toString()}`
  })
}

/** Forms that destroy or replace something ask first. */
function initConfirm(): void {
  for (const form of document.querySelectorAll<HTMLFormElement>("form[data-drive-confirm]")) {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.driveConfirm!)) event.preventDefault()
    })
  }
}

/** Narrow a long list of residents to those whose name contains what is typed. */
function initPicker(): void {
  for (const picker of document.querySelectorAll<HTMLElement>("[data-drive-pick]")) {
    const filter = picker.querySelector<HTMLInputElement>("[data-drive-pick-filter]")
    const rows = Array.from(picker.querySelectorAll<HTMLElement>("[data-drive-pick-name]"))
    filter?.addEventListener("input", () => {
      const query = filter.value.trim().toLowerCase()
      for (const row of rows) {
        const checked = row.querySelector<HTMLInputElement>("input:checked") !== null
        row.hidden = query !== "" && !checked && !row.dataset.drivePickName!.includes(query)
      }
    })
  }
}

/** A new folder directly in `Uden periode` / `Uden årstal` asks first, like an upload. */
function initInstead(): void {
  for (const form of document.querySelectorAll<HTMLFormElement>("form[data-drive-instead]")) {
    let asked = false
    form.addEventListener("submit", async (event) => {
      if (asked) return
      event.preventDefault()
      const choice = await askTimeless()
      if (choice === null) return
      if (choice === "instead") form.action = form.dataset.driveInstead!
      asked = true
      form.requestSubmit()
    })
  }
}

initUpload()
initInstead()
initSelection()
initConfirm()
initPicker()
