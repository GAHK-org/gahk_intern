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

/**
 * The viewer, and the selection counter beside "Hent valgte".
 *
 * Both are progressive enhancements over markup that already works. Every image row is an ordinary
 * link to the download view, and every checkbox already belongs to the batch form by `form=`; what
 * follows intercepts a plain left click to show the picture instead, and keeps a count in view.
 * With the bundle dead, clicking an image downloads it and the batch button still posts.
 */

interface Slide {
  url: string
  name: string
}

function slidesFrom(): Slide[] {
  // DOM order is the listing's order, which is the server's ordering by name. No second sort here.
  return Array.from(document.querySelectorAll<HTMLAnchorElement>('a[data-preview]')).map((a) => ({
    url: a.dataset.preview!,
    name: a.dataset.name ?? '',
  }))
}

function buildViewer(): {
  open: (index: number) => void
  close: () => void
} {
  const slides = slidesFrom()
  let at = 0

  const overlay = document.createElement('div')
  overlay.className = 'arkiv-viewer'
  overlay.hidden = true
  // A dialog to the accessibility tree, not just a dark div: focus moves here on open and the
  // label is read out, so a screen-reader user is told what happened rather than left on a page
  // whose links have silently stopped responding.
  overlay.setAttribute('role', 'dialog')
  overlay.setAttribute('aria-modal', 'true')
  overlay.setAttribute('aria-label', 'Billedvisning')
  overlay.tabIndex = -1
  overlay.innerHTML = `
    <button type="button" class="arkiv-viewer-close" aria-label="Luk">&times;</button>
    <button type="button" class="arkiv-viewer-nav arkiv-viewer-prev" aria-label="Forrige">&lsaquo;</button>
    <figure class="arkiv-viewer-stage">
      <img alt="">
      <figcaption></figcaption>
    </figure>
    <button type="button" class="arkiv-viewer-nav arkiv-viewer-next" aria-label="Næste">&rsaquo;</button>`
  document.body.append(overlay)

  const img = overlay.querySelector('img')!
  const caption = overlay.querySelector('figcaption')!
  let restoreFocusTo: HTMLElement | null = null

  function show(index: number): void {
    // Wraps, so the end of a folder rolls round rather than dead-ending on a button that does
    // nothing. `% length` twice because JavaScript's remainder keeps the sign of the dividend.
    at = ((index % slides.length) + slides.length) % slides.length
    const slide = slides[at]
    img.src = slide.url
    img.alt = slide.name
    caption.textContent = `${slide.name} · ${at + 1}/${slides.length}`

    // Warm the neighbours so paging feels immediate. The preview is a few hundred kB and cached
    // for a week, so this costs one request each and only the first time round.
    for (const step of [1, -1]) {
      const near = slides[((at + step) % slides.length + slides.length) % slides.length]
      new Image().src = near.url
    }
  }

  function open(index: number): void {
    restoreFocusTo = document.activeElement as HTMLElement | null
    overlay.hidden = false
    document.body.classList.add('arkiv-viewer-open')
    show(index)
    overlay.focus()
  }

  function close(): void {
    overlay.hidden = true
    document.body.classList.remove('arkiv-viewer-open')
    // Drop the bytes; a folder of two hundred photographs paged end to end would otherwise leave
    // the last one decoded in memory for as long as the page lives.
    img.removeAttribute('src')
    restoreFocusTo?.focus()
  }

  overlay.querySelector('.arkiv-viewer-close')!.addEventListener('click', close)
  overlay.querySelector('.arkiv-viewer-prev')!.addEventListener('click', () => show(at - 1))
  overlay.querySelector('.arkiv-viewer-next')!.addEventListener('click', () => show(at + 1))
  overlay.addEventListener('click', (event) => {
    // Only the backdrop itself. A click that started on the image or a button is not "outside".
    if (event.target === overlay) close()
  })

  document.addEventListener('keydown', (event) => {
    if (overlay.hidden) return
    if (event.key === 'Escape') close()
    else if (event.key === 'ArrowRight') show(at + 1)
    else if (event.key === 'ArrowLeft') show(at - 1)
    else return
    event.preventDefault()
  })

  // Swipe, because this is mostly read on a phone. Horizontal only, and only past a threshold, so
  // it does not fight a vertical scroll or fire on a tap that wandered a pixel.
  let startX = 0
  let startY = 0
  overlay.addEventListener(
    'touchstart',
    (event) => {
      startX = event.changedTouches[0].clientX
      startY = event.changedTouches[0].clientY
    },
    { passive: true },
  )
  overlay.addEventListener(
    'touchend',
    (event) => {
      const dx = event.changedTouches[0].clientX - startX
      const dy = event.changedTouches[0].clientY - startY
      if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) show(at + (dx < 0 ? 1 : -1))
    },
    { passive: true },
  )

  return { open, close }
}

const previewLinks = document.querySelectorAll<HTMLAnchorElement>('a[data-preview]')
if (previewLinks.length > 0) {
  const viewer = buildViewer()
  previewLinks.forEach((link, index) => {
    link.addEventListener('click', (event) => {
      // Leave the modified clicks alone: ctrl/cmd/shift/middle-click all mean "I want the file",
      // and hijacking them is the thing that makes a gallery infuriating.
      if (event.defaultPrevented || event.button !== 0) return
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
      event.preventDefault()
      viewer.open(index)
    })
  })
}

// The count beside "Hent valgte". The button works without it; this only says what will happen.
const batch = document.querySelector<HTMLFormElement>('[data-arkiv-batch]')
const batchCount = batch?.querySelector<HTMLElement>('[data-batch-count]') ?? null
if (batch && batchCount) {
  const picks = Array.from(document.querySelectorAll<HTMLInputElement>('[data-batch-pick]'))
  const update = (): void => {
    const n = picks.filter((p) => p.checked).length
    batchCount.textContent = n === 0 ? '' : `${n} valgt`
  }
  picks.forEach((pick) => pick.addEventListener('change', update))
  update()
}
