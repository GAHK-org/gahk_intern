import { previewImage, thumbnailImage } from './imageupload'

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

  // The derived sizes the server asked for: a 320px thumbnail for the listing and a 1600px preview
  // for the viewer. It offers only the ones actually missing, so the second copy of a photograph
  // somebody uploaded last year sends neither.
  //
  // Best effort on purpose, and per size. A browser that cannot decode the image, or a POST that
  // fails, must not cost the resident the upload they actually came to make - and failing to make
  // the preview must not cost them the thumbnail either. commit asks the STORE which sizes
  // arrived, so both flags stay honest whatever happens here.
  for (const [kind, maker] of [
    ['thumbnail', thumbnailImage],
    ['preview', previewImage],
  ] as const) {
    const slot = plan.derived?.[kind]
    if (!slot) continue
    try {
      const image = await maker(file)
      if (image) await sendDerived(slot, direct, sha256, kind, image)
    } catch {
      // ignored: the file is already stored, and a missing size degrades rather than breaks - no
      // thumbnail is a file icon, and no preview means the viewer serves the original instead.
    }
  }

  const done = await postJSON(commit, { sha256, name: file.name })
  if (!done.ok) throw new Error(await errorFrom(done))
}

async function sendDerived(
  plan: { mode: string; url?: string; fields?: Record<string, string> },
  direct: string,
  sha256: string,
  kind: string,
  image: Blob,
): Promise<void> {
  const form = new FormData()
  if (plan.mode === 's3') {
    // The policy already names the key and the content type; the browser only supplies the bytes.
    for (const [k, v] of Object.entries(plan.fields ?? {})) form.append(k, v)
    form.append('file', image)
    await fetch(plan.url!, { method: 'POST', body: form })
    return
  }
  form.append('sha256', sha256)
  form.append('derived', kind)
  form.append('file', image, `${kind}.jpg`)
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
 * Selecting files: long-press on a phone, drag across a run, shift-click a range.
 *
 * ONE IMPLEMENTATION FOR BOTH LAYOUTS. A photo folder is a grid and a document folder is a list
 * (see views.is_gallery), but both render `[data-selectable]` rows holding a `[data-batch-pick]`
 * checkbox, and nothing below knows which it is looking at. The rows are in DOM order, which is the
 * server's ordering, so "the range between these two" is just a slice.
 *
 * All of it is an enhancement over checkboxes that already work. With the bundle dead every box is
 * visible and tickable and the form still posts; what this adds is not having to hit two hundred of
 * them one at a time.
 *
 * SELECTION MODE exists because a tap on a tile has two plausible meanings. Out of mode a tap opens
 * the picture, which is what you want ninety-nine folders out of a hundred. A long press - or the
 * "Vælg" button, or ticking any box - turns tapping into selecting, the way a phone's photo app
 * does, and Escape or clearing the selection turns it back.
 */
const scope = document.querySelector<HTMLElement>('[data-selection-scope]')
const rows = scope ? Array.from(scope.querySelectorAll<HTMLElement>('[data-selectable]')) : []
const boxes = rows.map((row) => row.querySelector<HTMLInputElement>('[data-batch-pick]'))

let selecting = false
let anchor = -1
/** The drag in progress: where it started, what was selected before it, and which way it paints. */
let drag: { from: number; base: Set<number>; additive: boolean } | null = null
let pressTimer = 0
let pressAt: { x: number; y: number; index: number } | null = null
/** Set when a drag ends, so the click browsers fire afterwards does not undo its own gesture. */
let swallowClick = false

const selectionCount = (): number => boxes.filter((b) => b?.checked).length

function setMode(on: boolean): void {
  selecting = on
  scope?.classList.toggle('arkiv-selecting', on)
  document.querySelector('[data-select-mode]')?.setAttribute('aria-pressed', String(on))
}

function indexAt(x: number, y: number): number {
  // elementFromPoint rather than the event target: during a drag the pointer is captured by the
  // element it started on, so the target never changes and every other row would be unreachable.
  const el = document.elementFromPoint(x, y)
  const row = el?.closest<HTMLElement>('[data-selectable]')
  return row ? rows.indexOf(row) : -1
}

function applyDrag(to: number): void {
  if (!drag || to < 0) return
  const [lo, hi] = drag.from < to ? [drag.from, to] : [to, drag.from]
  rows.forEach((_, i) => {
    const box = boxes[i]
    if (!box) return
    // Outside the run the baseline wins, so a drag never disturbs a selection made before it, and
    // dragging back over your own path undoes it rather than leaving a trail.
    box.checked = i >= lo && i <= hi ? drag!.additive : drag!.base.has(i)
  })
  announce()
}

function beginDrag(index: number): void {
  if (index < 0) return
  setMode(true)
  const base = new Set<number>()
  boxes.forEach((b, i) => {
    if (b?.checked) base.add(i)
  })
  // Painted the opposite of where it began: starting on an unselected tile selects the run,
  // starting on a selected one clears it. The same rule a spreadsheet uses.
  drag = { from: index, base, additive: !boxes[index]?.checked }
  scope?.classList.add('arkiv-dragging')
  applyDrag(index)
}

function endDrag(): void {
  if (drag) {
    anchor = drag.from
    swallowClick = true
    drag = null
    scope?.classList.remove('arkiv-dragging')
  }
  window.clearTimeout(pressTimer)
  pressAt = null
}

function announce(): void {
  const event = new CustomEvent('arkiv:selection')
  document.dispatchEvent(event)
}

if (scope && rows.length > 0) {
  scope.addEventListener('pointerdown', (event) => {
    const index = indexAt(event.clientX, event.clientY)
    if (index < 0) return
    pressAt = { x: event.clientX, y: event.clientY, index }

    if (event.pointerType === 'touch') {
      // Long press. Cancelled below if the finger moves first, which is what a scroll looks like.
      pressTimer = window.setTimeout(() => beginDrag(index), 450)
      return
    }
    // A mouse needs no long press: it has a button, so press-and-move is already a distinct
    // gesture from a click. The drag waits for real movement, which is what keeps an ordinary
    // click opening the picture - and it starts anywhere on a tile, not only on the checkbox,
    // because "drag across the ones I want" is the whole request and reaching for a 22px box
    // first would defeat it.
    //
    // Nothing else claims that gesture: images are draggable by default in HTML, which would
    // otherwise start a native file drag half way through, so the markup turns that off.
  })

  scope.addEventListener('pointermove', (event) => {
    if (drag) {
      applyDrag(indexAt(event.clientX, event.clientY))
      return
    }
    if (!pressAt) return
    const far = Math.hypot(event.clientX - pressAt.x, event.clientY - pressAt.y) > 8
    if (!far) return
    if (event.pointerType === 'touch') {
      // Moved before the press was long enough: the reader is scrolling, not selecting.
      window.clearTimeout(pressTimer)
      pressAt = null
    } else {
      beginDrag(pressAt.index)
    }
  })

  // Non-passive, and only while dragging: once a long press has committed to selecting, the same
  // finger must not also scroll the folder away underneath it. touch-action alone cannot do this -
  // the gesture is already in flight by the time the class lands, and the browser does not
  // re-read it.
  scope.addEventListener(
    'touchmove',
    (event) => {
      if (drag) event.preventDefault()
    },
    { passive: false },
  )

  for (const type of ['pointerup', 'pointercancel', 'pointerleave'] as const) {
    scope.addEventListener(type, endDrag)
  }

  boxes.forEach((box, index) => {
    box?.addEventListener('click', (event) => {
      if (event.shiftKey && anchor >= 0) {
        // A range, the way every file manager does it. Extends rather than replaces: shift-clicking
        // a second run somewhere else keeps the first.
        const [lo, hi] = anchor < index ? [anchor, index] : [index, anchor]
        for (let i = lo; i <= hi; i++) {
          const b = boxes[i]
          if (b) b.checked = true
        }
      }
      anchor = index
      setMode(true)
      announce()
    })
  })

  function clearSelection(): void {
    boxes.forEach((b) => {
      if (b) b.checked = false
    })
    setMode(false)
    announce()
  }

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !selecting || drag) return
    clearSelection()
  })

  // CAPTURE PHASE, and it owns two things that both have to happen before anything else sees the
  // click - in particular before imageviewer.ts, whose delegated listener would otherwise open the
  // picture when the tap was meant to select it.
  //
  // First, the click a drag leaves behind. This used to be checked inside the anchor's own handler,
  // which meant the flag was only cleared when that stray click happened to land on a picture. Land
  // it anywhere else - the page background, the toolbar - and it stayed set for the life of the
  // page, silently disabling the blank-area dismissal from the first drag onwards.
  //
  // Second, a tap while selecting means "this one too", not "show me this one" - the phone photo
  // app rule. Out of selection mode the click falls through untouched and the viewer opens it,
  // which is what almost every visit wants.
  document.addEventListener(
    'click',
    (event) => {
      if (swallowClick) {
        swallowClick = false
        event.stopPropagation()
        event.preventDefault()
        return
      }
      if (!selecting) return
      const row = (event.target as HTMLElement | null)?.closest<HTMLElement>('[data-selectable]')
      if (!row) return
      const box = row.querySelector<HTMLInputElement>('[data-batch-pick]')
      // The checkbox itself already toggles, and has its own listener for shift-ranges. Claiming
      // that click here as well would toggle it twice and leave it exactly as it started.
      if (!box || (event.target as HTMLElement).closest('[data-batch-pick]')) return
      event.stopPropagation()
      event.preventDefault()
      box.checked = !box.checked
      anchor = rows.indexOf(row)
      announce()
    },
    true,
  )

  // Tapping the empty part of the page means "never mind" - the same as Escape, which a phone does
  // not have. Without it, leaving selection mode on a touch device meant finding the "Vælg" button
  // again, and until you did, every tap on a picture selected it instead of opening it.
  //
  // Clears as well as exits, deliberately: a mode that ends with a selection still standing is a
  // state where the count says "12 valgt" and tapping a photo opens it, which is two rules at once.
  // The cost is that a stray tap on a gap loses the selection - acceptable because the gesture is
  // deliberate, the count is in view the whole time, and in a grid the tiles cover most of the area
  // anyway.
  document.addEventListener('click', (event) => {
    if (!selecting || drag) return
    const target = event.target as HTMLElement
    // Anything that is part of the mechanism, or is interactive in its own right, is not "blank".
    // The viewer counts too: it sits over the page, and a click inside it is aimed at the picture.
    if (
      target.closest(
        '[data-selectable], [data-arkiv-batch], .arkiv-viewer, a, button, input, label, select, textarea',
      )
    ) {
      return
    }
    clearSelection()
  })

  const modeButton = document.querySelector<HTMLButtonElement>('[data-select-mode]')
  if (modeButton) {
    modeButton.hidden = false
    modeButton.addEventListener('click', () => {
      if (selecting) {
        clearSelection()
        return
      }
      setMode(true)
      announce()
    })
  }
}


/**
 * The count beside "Hent valgte", and the pending state while the zip is built.
 *
 * Both are enhancements: the form posts and the download works with none of this running.
 *
 * The pending state exists because the server now BUILDS the archive into the bucket before
 * redirecting to it, rather than streaming it out as it goes (see arkiv/views.py). That is what
 * takes the recipient's connection off a gunicorn worker, and the price is a wait with nothing to
 * look at - a second or two for an ordinary selection, closer to ten for a large one. Long enough
 * to read as "the button did nothing" and be clicked again.
 *
 * Knowing when to STOP is the awkward half. A form POST that ends in a download does not navigate:
 * the page stays, the bytes go to the downloads shelf, and no load event fires anywhere. The
 * response is a redirect to Hetzner, whose reply is not ours to see either. So the form carries a
 * nonce, the server echoes it back as a cookie, and this polls for it.
 */
const batch = document.querySelector<HTMLFormElement>('[data-arkiv-batch]')
const batchCount = batch?.querySelector<HTMLElement>('[data-batch-count]') ?? null
if (batch && batchCount) {
  const picks = Array.from(document.querySelectorAll<HTMLInputElement>('[data-batch-pick]'))
  const submit = batch.querySelector<HTMLButtonElement>('[data-batch-submit]')
  const token = batch.querySelector<HTMLInputElement>('[data-batch-token]')

  const update = (): void => {
    const n = picks.filter((p) => p.checked).length
    batchCount.textContent = n === 0 ? '' : `${n} valgt`
  }
  picks.forEach((pick) => pick.addEventListener('change', update))
  // Drag and shift-click set .checked directly, which fires no change event - hence the custom one.
  document.addEventListener('arkiv:selection', update)
  update()

  if (submit && token) {
    const label = submit.textContent ?? 'Hent valgte'
    let waiting = 0

    const finish = (): void => {
      window.clearInterval(waiting)
      document.cookie = 'arkiv_zip_done=; Max-Age=0; Path=/'
      submit.disabled = false
      submit.textContent = label
      update()
    }

    batch.addEventListener('submit', () => {
      // No selection: the server answers with a message on a page that reloads, so leaving the
      // button alone is both correct and less work than predicting that refusal here.
      if (!picks.some((p) => p.checked)) return

      const nonce = Math.random().toString(36).slice(2) + Date.now().toString(36)
      token.value = nonce
      submit.disabled = true
      submit.textContent = 'Pakker filer…'
      batchCount.textContent = 'Det kan tage et øjeblik for mange filer.'

      const started = Date.now()
      waiting = window.setInterval(() => {
        if (document.cookie.includes(`arkiv_zip_done=${nonce}`)) finish()
        // A build that failed sets no cookie, and a button disabled for ever is a worse bug than
        // the one this exists to fix. Past the point where the server would itself have given up,
        // hand it back and let the resident try again.
        else if (Date.now() - started > 120_000) finish()
      }, 250)
    })
  }
}
