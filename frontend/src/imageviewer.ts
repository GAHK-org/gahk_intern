/**
 * The full-screen picture viewer, shared by Arkiv, opslagstavlen and Den Hurtige.
 *
 * IT EXISTS SO A LONG PRESS CAN SAVE THE PHOTOGRAPH. That is the whole reason it is shared rather
 * than living in Arkiv. On a phone, a long press on a bare <img> is how a picture gets into the
 * photo library - iOS offers "Føj til Fotos", Android "Download image" - with no permission prompt
 * and no second copy of the bytes, because the browser already has them. But a long press on an
 * image wrapped in a LINK gets the link's menu instead ("Download Linked File", which lands in
 * Files, not Photos), and both a Den Hurtige message photo and an opslagstavle comment photo were
 * exactly that: <a href=image><img></a>, so the picture could be opened but never really saved.
 *
 * So the viewer puts a bare image under the finger. The anchor stays in the markup and still works
 * with the bundle dead - it opens the picture on its own page, where it is also bare - and when the
 * script is alive a tap opens this instead.
 *
 * DELEGATED, not bound per element, because Den Hurtige morphs its feed every five seconds. Any
 * listener attached to a message at load is gone the next time htmx swaps it in, and a gallery that
 * silently stops working on the newest messages is worse than one that never worked.
 */

interface Slide {
  /** What to show first. The cheap one where there are two sizes, the only one where there is not. */
  url: string
  name: string
  /** Full resolution. Where the anchor pointed before the viewer took the click. */
  download: string
}

const GROUP = 'a[data-viewer]'

function slides(): Slide[] {
  // Read at open time, not at load. The feed's contents change under us, and a list captured once
  // would page to pictures that are no longer on the page.
  return Array.from(document.querySelectorAll<HTMLAnchorElement>(GROUP)).map((a) => ({
    url: a.dataset.preview ?? a.getAttribute('href') ?? '',
    name: a.dataset.name ?? '',
    download: a.getAttribute('href') ?? '',
  }))
}

let overlay: HTMLElement | null = null
let img: HTMLImageElement
let caption: HTMLElement
let save: HTMLAnchorElement
let hint: HTMLElement
let current: Slide[] = []
let at = 0
let generation = 0
let restoreFocusTo: HTMLElement | null = null

/** Touch only: a long press is the gesture this serves, and a desktop has the save link instead. */
const touch = matchMedia('(hover: none)').matches

function build(): HTMLElement {
  const el = document.createElement('div')
  el.className = 'imgviewer'
  el.hidden = true
  // A dialog to the accessibility tree, not just a dark div: focus moves here on open and the
  // label is read out, so a screen-reader user is told what happened rather than left on a page
  // whose links have silently stopped responding.
  el.setAttribute('role', 'dialog')
  el.setAttribute('aria-modal', 'true')
  el.setAttribute('aria-label', 'Billedvisning')
  el.tabIndex = -1
  el.innerHTML = `
    <button type="button" class="imgviewer-close" aria-label="Luk">&times;</button>
    <button type="button" class="imgviewer-nav imgviewer-prev" aria-label="Forrige">&lsaquo;</button>
    <figure class="imgviewer-stage">
      <img alt="">
      <figcaption></figcaption>
      <p class="imgviewer-actions">
        <a class="imgviewer-save" download>Hent original</a>
        <span class="imgviewer-hint" hidden>Hold fingeren på billedet for at gemme det i Fotos</span>
      </p>
    </figure>
    <button type="button" class="imgviewer-nav imgviewer-next" aria-label="Næste">&rsaquo;</button>`
  document.body.append(el)

  img = el.querySelector('img')!
  caption = el.querySelector('figcaption')!
  save = el.querySelector<HTMLAnchorElement>('.imgviewer-save')!
  hint = el.querySelector<HTMLElement>('.imgviewer-hint')!
  if (touch) hint.hidden = false

  el.querySelector('.imgviewer-close')!.addEventListener('click', close)
  el.querySelector('.imgviewer-prev')!.addEventListener('click', () => show(at - 1))
  el.querySelector('.imgviewer-next')!.addEventListener('click', () => show(at + 1))
  el.addEventListener('click', (event) => {
    // Only the backdrop itself. A click that landed on the image or a button is not "outside".
    if (event.target === el) close()
  })

  document.addEventListener('keydown', (event) => {
    if (el.hidden) return
    if (event.key === 'Escape') close()
    else if (event.key === 'ArrowRight') show(at + 1)
    else if (event.key === 'ArrowLeft') show(at - 1)
    else return
    event.preventDefault()
  })

  // Swipe, because this is mostly read on a phone. Horizontal only and past a threshold, so it
  // does not fight a vertical scroll or fire on a tap that wandered a pixel.
  let startX = 0
  let startY = 0
  el.addEventListener(
    'touchstart',
    (event) => {
      startX = event.changedTouches[0].clientX
      startY = event.changedTouches[0].clientY
    },
    { passive: true },
  )
  el.addEventListener(
    'touchend',
    (event) => {
      const dx = event.changedTouches[0].clientX - startX
      const dy = event.changedTouches[0].clientY - startY
      if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) show(at + (dx < 0 ? 1 : -1))
    },
    { passive: true },
  )
  return el
}

/**
 * Swap the preview for the original once it has arrived, so the long press saves the real thing.
 *
 * Preview first, original second, deliberately: showing the original immediately would mean
 * staring at a blank frame while tens of megabytes arrive over dorm wifi, whereas the preview is
 * there at once and the swap, when it lands, is the same picture at higher resolution.
 *
 * Skipped when the two are the same URL, which is the case everywhere except Arkiv - an
 * opslagstavle photo is served at full size already, so there is nothing to upgrade to and no
 * second request to make.
 *
 * Touch-only, because the cost is real: a photograph that is looked at gets fetched twice, and
 * egress is the one line of the Hetzner bill that scales with use. A desktop has the save link.
 */
function upgrade(slide: Slide, forGeneration: number): void {
  if (!touch || !slide.download || slide.download === slide.url) return
  hint.textContent = 'Henter fuld opløsning…'
  const full = new Image()
  full.onload = () => {
    // Paging is faster than a large download, so by the time this lands the reader may be two
    // pictures on. The generation check is what stops the wrong photograph appearing in the frame.
    if (forGeneration !== generation) return
    img.src = slide.download
    hint.textContent = 'Hold fingeren på billedet for at gemme det i Fotos'
  }
  full.onerror = () => {
    // The preview stays on screen and remains saveable. Worse quality, still a picture.
    if (forGeneration === generation) hint.textContent = 'Hold fingeren nede for at gemme (preview)'
  }
  full.src = slide.download
}

function show(index: number): void {
  if (current.length === 0) return
  // Wraps, so the end of a folder rolls round rather than dead-ending on a button that does
  // nothing. The remainder is taken twice because JavaScript's keeps the sign of the dividend.
  at = ((index % current.length) + current.length) % current.length
  const slide = current[at]
  generation += 1
  img.src = slide.url
  img.alt = slide.name
  caption.textContent = slide.name
    ? `${slide.name}${current.length > 1 ? ` · ${at + 1}/${current.length}` : ''}`
    : current.length > 1
      ? `${at + 1}/${current.length}`
      : ''
  upgrade(slide, generation)
  save.href = slide.download
  // Names the saved file, so a bare hash in a presigned URL does not become the filename. The
  // server sets Content-Disposition too; this covers the local store in dev, which has no
  // presigned URL to put it on.
  // An empty `download` still downloads - the browser falls back to the filename in the URL, which
  // is right for a /media/ path. Removing the attribute would turn the link into a navigation.
  save.setAttribute('download', slide.name)

  // Warm the neighbours so paging feels immediate.
  if (current.length > 1) {
    for (const step of [1, -1]) {
      const near = current[((at + step) % current.length + current.length) % current.length]
      new Image().src = near.url
    }
  }
}

function close(): void {
  if (!overlay) return
  generation += 1 // any upgrade still in flight is for a picture nobody is looking at
  overlay.hidden = true
  document.body.classList.remove('imgviewer-open')
  // Drop the bytes; paging a folder end to end would otherwise leave the last one decoded in
  // memory for as long as the page lives.
  img.removeAttribute('src')
  restoreFocusTo?.focus()
}

export function openViewer(link: HTMLAnchorElement): void {
  overlay ??= build()
  current = slides()
  // A lone photograph - a Den Hurtige message, most opslagstavle comments - has nowhere to page to,
  // and two arrows that do nothing are a worse answer than no arrows.
  for (const nav of overlay.querySelectorAll<HTMLElement>('.imgviewer-nav')) {
    nav.hidden = current.length < 2
  }
  const index = Array.from(document.querySelectorAll<HTMLAnchorElement>(GROUP)).indexOf(link)
  restoreFocusTo = document.activeElement as HTMLElement | null
  overlay.hidden = false
  document.body.classList.add('imgviewer-open')
  show(index < 0 ? 0 : index)
  overlay.focus()
}

// One delegated listener for every picture on the site. Bubble phase, so anything that wants to
// claim the click first - Arkiv's selection mode, which toggles a tile instead of opening it - can
// stop it in capture.
document.addEventListener('click', (event) => {
  const link = (event.target as HTMLElement | null)?.closest<HTMLAnchorElement>(GROUP)
  if (!link) return
  // Leave the modified clicks alone: ctrl/cmd/shift/middle-click all mean "I want the file", and
  // hijacking them is the thing that makes a gallery infuriating.
  if (event.defaultPrevented || (event as MouseEvent).button !== 0) return
  const e = event as MouseEvent
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
  event.preventDefault()
  openViewer(link)
})
