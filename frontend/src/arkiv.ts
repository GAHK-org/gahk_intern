import { thumbnailImage } from './imageupload'

/**
 * Uploading into Arkiv.
 *
 * THE FILE DOES NOT GO THROUGH DJANGO IN PRODUCTION. The server hands back a policy, the browser
 * POSTs the bytes straight to Hetzner, and only then does the server create a row. See
 * arkiv/uploads.py for why: this feature holds the 2 GB video from sommerfest, and three
 * synchronous gunicorn workers with a 60-second timeout cannot carry that.
 *
 * The hash is computed here, before anything is sent, and it is what the object is keyed by. That
 * costs a full read of the file in the browser - a few seconds for 2 GB - and buys deduplication
 * (the second copy of a photograph uploads nothing at all) and a restartable, idempotent upload.
 *
 * Deliberately no drag-and-drop and no progress bar in this pass. A plain <input type="file"> works
 * on every phone in the house, is what people already recognise, and needs no keyboard or
 * screen-reader story of its own.
 */

/** SHA-256 of a file, hex, via SubtleCrypto. */
async function hashFile(file: File): Promise<string> {
  // digest() takes one buffer, so the whole file goes to it at once. Fine to ~2 GB; the streaming
  // alternative needs a userland SHA-256 and would be slower for every ordinary photograph.
  const buffer = await file.arrayBuffer()
  const digest = await crypto.subtle.digest('SHA-256', buffer)
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

function csrf(): string {
  const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/)
  return m ? decodeURIComponent(m[1]) : ''
}

async function postJSON(url: string, body: unknown): Promise<Response> {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
    body: JSON.stringify(body),
  })
}

async function errorFrom(response: Response): Promise<string> {
  try {
    const data = await response.json()
    return data.error || 'Upload mislykkedes.'
  } catch {
    return 'Upload mislykkedes.'
  }
}

async function uploadOne(root: HTMLElement, file: File, status: HTMLElement): Promise<void> {
  const begin = root.dataset.beginUrl!
  const direct = root.dataset.directUrl!
  const commit = root.dataset.commitUrl!

  status.textContent = `Beregner kontrolsum for ${file.name}…`
  const sha256 = await hashFile(file)

  status.textContent = `Sender ${file.name}…`
  const started = await postJSON(begin, {
    sha256,
    name: file.name,
    size: file.size,
    content_type: file.type,
  })
  if (!started.ok) throw new Error(await errorFrom(started))
  const plan = await started.json()

  // already_stored: these exact bytes are in the bucket already, from another folder or an
  // interrupted attempt. Nothing to send - go straight to commit.
  if (plan.upload && plan.upload.mode === 's3') {
    const form = new FormData()
    for (const [k, v] of Object.entries(plan.upload.fields as Record<string, string>)) {
      form.append(k, v)
    }
    form.append('file', file)
    const sent = await fetch(plan.upload.url, { method: 'POST', body: form })
    if (!sent.ok) throw new Error('Objektlageret afviste filen.')
  } else if (plan.upload && plan.upload.mode === 'direct') {
    const form = new FormData()
    form.append('sha256', sha256)
    form.append('file', file)
    const sent = await fetch(direct, { method: 'POST', headers: { 'X-CSRFToken': csrf() }, body: form })
    if (!sent.ok) throw new Error(await errorFrom(sent))
  }

  // The preview, if the server offered a slot for one. Best effort on purpose: a browser that
  // cannot decode the image, or a thumbnail POST that fails, must not cost the resident the upload
  // they actually came to make. commit asks the store whether a preview arrived, so the flag stays
  // honest either way.
  if (plan.thumbnail) {
    try {
      const thumb = await thumbnailImage(file)
      if (thumb) await sendThumbnail(plan.thumbnail, direct, sha256, thumb)
    } catch {
      // ignored: the file is already stored, and a missing preview is a file icon, not a failure
    }
  }

  const done = await postJSON(commit, { sha256, name: file.name })
  if (!done.ok) throw new Error(await errorFrom(done))
}

async function sendThumbnail(
  plan: { mode: string; url?: string; fields?: Record<string, string> },
  direct: string,
  sha256: string,
  thumb: Blob,
): Promise<void> {
  const form = new FormData()
  if (plan.mode === 's3') {
    for (const [k, v] of Object.entries(plan.fields ?? {})) form.append(k, v)
    form.append('file', thumb)
    await fetch(plan.url!, { method: 'POST', body: form })
    return
  }
  form.append('sha256', sha256)
  form.append('thumbnail', '1')
  form.append('file', thumb, 'thumb.jpg')
  await fetch(direct, { method: 'POST', headers: { 'X-CSRFToken': csrf() }, body: form })
}

// Top-level with a null guard, like events.ts and reparationer.ts: the bundle runs after the DOM,
// and every page without an upload control is a no-op.
const root = document.querySelector<HTMLElement>('[data-arkiv-upload]')
const input = root?.querySelector<HTMLInputElement>('input[type=file]') ?? null
const status = root?.querySelector<HTMLElement>('[data-upload-status]') ?? null

if (root && input && status) {
  input.addEventListener('change', async () => {
    const files = Array.from(input.files ?? [])
    if (files.length === 0) return
    input.disabled = true

    let done = 0
    for (const file of files) {
      try {
        await uploadOne(root, file, status)
        done += 1
      } catch (err) {
        // Stop on the first failure rather than pressing on: the usual causes (a duplicate name, a
        // dead session, a file over the limit) apply to the whole batch, and a half-finished upload
        // of thirty photographs with no way to tell which is worse than a clear stop.
        status.textContent = err instanceof Error ? err.message : 'Upload mislykkedes.'
        input.disabled = false
        return
      }
    }

    status.textContent = `${done} fil${done === 1 ? '' : 'er'} lagt op. Genindlæser…`
    // A reload rather than inserting rows: the listing is ordered, shared, and already rendered
    // correctly by the server - a second implementation in JavaScript is cheaper to get wrong than
    // to trust.
    window.location.reload()
  })
}
