// Drive's uploads (drive/views.py). Each file asks the server first (upload/begin), which answers
// with a ticket for the bucket, `{"direct": false}` when there is no bucket and the file goes with
// the request instead, or a 409 when a file of that name is there already. Then the bytes go
// straight to the bucket in parts (directupload.ts, shared with the photo album), and commit makes
// the rows. No-op on pages without [data-drive-upload].

import { DirectUnavailable, UploadRefused, sendToBucket, type DirectUpload } from "./directupload"
import { csrfToken } from "./htmx-csrf"

type Config = { beginUrl: string; signUrl: string; commitUrl: string; directUrl: string; maxBytes: number }

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

/** Ask what to do when the name is taken: "replace" (a new version), "keep_both", or null to skip. */
function askConflict(dialog: HTMLDialogElement, name: string): Promise<string | null> {
  dialog.querySelector("[data-conflict-text]")!.textContent = `Der er allerede en fil, der hedder "${name}".`
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

async function uploadOne(
  config: Config,
  dialog: HTMLDialogElement,
  file: File,
  say: (text: string) => void,
): Promise<string> {
  if (file.size > config.maxBytes) return `${file.name}: for stor (højst ${formatBytes(config.maxBytes)}).`
  let onConflict = ""
  for (;;) {
    const begun = await post(config.beginUrl, { name: file.name, size: String(file.size), type: file.type, on_conflict: onConflict })
    if (begun.status === 409 && begun.body.conflict && !onConflict) {
      const choice = await askConflict(dialog, file.name)
      if (choice === null) return `${file.name}: sprunget over.`
      onConflict = choice
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
    const sent = await post(config.directUrl, { file, on_conflict: onConflict })
    return sent.status < 300 ? "" : `${file.name}: ${String(sent.body.error ?? "kunne ikke lægges op.")}`
  }
}

function init(): void {
  const root = document.querySelector<HTMLElement>("[data-drive-upload]")
  const raw = document.getElementById("drive-upload-config")?.textContent
  if (!root || !raw) return
  const config = JSON.parse(raw) as Config
  const input = root.querySelector<HTMLInputElement>("[data-drive-files]")!
  const status = root.querySelector<HTMLElement>("[data-upload-status]")!
  const dialog = root.querySelector<HTMLDialogElement>("[data-drive-conflict]")!
  const say = (text: string) => {
    status.textContent = text
  }
  const guard = (event: BeforeUnloadEvent) => event.preventDefault()

  input.addEventListener("change", async () => {
    const files = Array.from(input.files ?? [])
    if (!files.length) return
    input.disabled = true
    window.addEventListener("beforeunload", guard)
    const problems: string[] = []
    for (const [index, file] of files.entries()) {
      say(`${index + 1} af ${files.length}: ${file.name} …`)
      const problem = await uploadOne(config, dialog, file, say)
      if (problem) problems.push(problem)
    }
    window.removeEventListener("beforeunload", guard)
    if (problems.length) {
      input.disabled = false
      input.value = ""
      say(problems.join(" "))
      if (problems.length < files.length) window.setTimeout(() => window.location.reload(), 4000)
      return
    }
    window.location.reload()
  })
}

init()
