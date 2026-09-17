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
 * IT ALSO ZOOMS. A photograph in Den Hurtige is as often a whiteboard, a receipt or a notice on a
 * door as it is a face, and fitted to a phone screen the part worth reading is unreadable. The
 * browser's own pinch is no help: the overlay is fixed to the viewport, so pinching magnifies the
 * backdrop along with the picture and springs back on release. See the zoom section below.
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

/* ZOOM ----------------------------------------------------------------------------------------
 *
 * Pinch, double-tap and drag on touch; wheel, double-click and drag with a pointer. A CSS
 * transform on the <img> rather than a bigger source: it costs no bytes and stays sharp up to the
 * original's own resolution, which on touch is the full-size file because upgrade() has already
 * swapped it in.
 *
 * A RESTING FINGER STILL BELONGS TO THE LONG PRESS - the save gesture this whole file exists for.
 * Nothing here calls preventDefault on a single-finger touchstart, which is what would take it
 * away; the pan claims the touch only once it has moved, and only while zoomed in. */

const MAX_ZOOM = 4
/** Where a double-tap lands. Enough to read a whiteboard, not so far that you lose the frame. */
const TAP_ZOOM = 2.5

let scale = 1
let tx = 0
let ty = 0
/** A second finger came down in this sequence, so its release is a pinch ending, not a swipe. */
let multiTouch = false
/** A mouse drag just ended, so the click it ends on must not reach the backdrop and close. */
let swallowClick = false

/** How far the picture may be dragged: out to its own edges, never past them into bare backdrop. */
function panLimit(): { x: number; y: number } {
  return { x: (img.clientWidth * (scale - 1)) / 2, y: (img.clientHeight * (scale - 1)) / 2 }
}

function applyZoom(animate = false): void {
  const limit = panLimit()
  tx = Math.min(limit.x, Math.max(-limit.x, tx))
  ty = Math.min(limit.y, Math.max(-limit.y, ty))
  img.style.transition = animate ? 'transform .18s ease-out' : ''
  // Cleared rather than written as scale(1): an untouched picture then carries no transform at all
  // and keeps the browser's plain rendering path for it.
  img.style.transform = scale === 1 ? '' : `translate(${tx}px, ${ty}px) scale(${scale})`
  overlay?.classList.toggle('imgviewer-zoomed', scale > 1)
}

/**
 * Zoom towards `next`, holding whatever sits under (px, py) - the fingers, the cursor - in place.
 * Zooming about the middle instead would slide the detail you aimed at off the screen, which on a
 * photograph magnified four times means hunting for it again after every step.
 */
function zoomAbout(next: number, px: number, py: number, animate = false): void {
  const to = Math.min(MAX_ZOOM, Math.max(1, next))
  const rect = img.getBoundingClientRect()
  // The rect already includes the current transform, so its centre is the on-screen centre and the
  // shift that pins (px, py) in place falls out of the ratio between the two scales.
  const factor = 1 - to / scale
  tx += (px - (rect.left + rect.width / 2)) * factor
  ty += (py - (rect.top + rect.height / 2)) * factor
  scale = to
  if (scale === 1) {
    tx = 0
    ty = 0
  }
  applyZoom(animate)
}

function resetZoom(animate = false): void {
  scale = 1
  tx = 0
  ty = 0
  applyZoom(animate)
}

/**
 * Every zoom gesture, bound once to the image itself - it is built once and reused for every
 * picture, so there is nothing to rebind - and never to the backdrop, which stays a way out.
 */
function bindZoom(): void {
  const spread = (t: TouchList): number =>
    Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY)

  img.addEventListener(
    'wheel',
    (event) => {
      event.preventDefault()
      // Exponential, so one notch of the wheel feels the same at 1x as at 3x.
      zoomAbout(scale * Math.exp(-event.deltaY * 0.0025), event.clientX, event.clientY)
    },
    { passive: false },
  )

  // A phone can synthesise a dblclick from a double-tap, which would undo the tap handler below
  // the moment it fired. The timestamp is what tells the two apart.
  let lastTouchEnd = 0
  img.addEventListener('dblclick', (event) => {
    if (Date.now() - lastTouchEnd < 700) return
    event.preventDefault()
    if (scale > 1) resetZoom(true)
    else zoomAbout(TAP_ZOOM, event.clientX, event.clientY, true)
  })

  img.addEventListener('mousedown', (event) => {
    if (scale === 1 || event.button !== 0) return
    event.preventDefault() // otherwise the drag becomes the browser dragging the image away
    const fromX = event.clientX - tx
    const fromY = event.clientY - ty
    const move = (e: MouseEvent): void => {
      tx = e.clientX - fromX
      ty = e.clientY - fromY
      swallowClick = true
      applyZoom()
    }
    const up = (): void => {
      removeEventListener('mousemove', move)
      removeEventListener('mouseup', up)
    }
    addEventListener('mousemove', move)
    addEventListener('mouseup', up)
  })

  let pinchFrom = 0
  let pinchScale = 1
  let panFromX = 0
  let panFromY = 0
  let panning = false
  /** Something other than a tap happened, so the release must not be read as a double-tap. */
  let moved = false
  let lastTap = 0
  let lastTapX = 0
  let lastTapY = 0

  img.addEventListener(
    'touchstart',
    (event) => {
      if (event.touches.length === 2) {
        // preventDefault is safe on a SECOND finger: the long press is a one-finger gesture.
        event.preventDefault()
        pinchFrom = spread(event.touches)
        pinchScale = scale
        panning = false
        moved = true
      } else if (event.touches.length === 1) {
        moved = false
        // Armed, not claimed. Nothing is prevented until the finger actually travels.
        panning = scale > 1
        panFromX = event.touches[0].clientX - tx
        panFromY = event.touches[0].clientY - ty
      }
    },
    { passive: false },
  )

  img.addEventListener(
    'touchmove',
    (event) => {
      if (event.touches.length === 2 && pinchFrom > 0) {
        event.preventDefault()
        const t = event.touches
        zoomAbout(
          pinchScale * (spread(t) / pinchFrom),
          (t[0].clientX + t[1].clientX) / 2,
          (t[0].clientY + t[1].clientY) / 2,
        )
      } else if (panning && event.touches.length === 1) {
        event.preventDefault()
        tx = event.touches[0].clientX - panFromX
        ty = event.touches[0].clientY - panFromY
        moved = true
        applyZoom()
      }
    },
    { passive: false },
  )

  img.addEventListener('touchend', (event) => {
    if (event.touches.length > 0) return // a pinch losing one finger; the other is still working
    lastTouchEnd = Date.now()
    pinchFrom = 0
    panning = false
    if (moved) {
      moved = false
      return
    }
    const t = event.changedTouches[0]
    const now = Date.now()
    // Two taps, close together in both time and place. A wandering second tap is a new first tap.
    if (now - lastTap < 300 && Math.hypot(t.clientX - lastTapX, t.clientY - lastTapY) < 30) {
      if (scale > 1) resetZoom(true)
      else zoomAbout(TAP_ZOOM, t.clientX, t.clientY, true)
      lastTap = 0
    } else {
      lastTap = now
      lastTapX = t.clientX
      lastTapY = t.clientY
    }
  })

  // Rotating the phone changes what "out to its own edges" means, and a picture left hanging half
  // off the screen has no gesture that brings it back.
  addEventListener('resize', () => {
    if (scale > 1) applyZoom()
  })
}

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
    // The click that ends a pan lands wherever the mouse let go, which can be the backdrop. Closing
    // the viewer because someone dragged a magnified picture too far is not what they asked for.
    const dragged = swallowClick
    swallowClick = false
    if (dragged) return
    // Only the backdrop itself. A click that landed on the image or a button is not "outside".
    if (event.target === el) close()
  })

  document.addEventListener('keydown', (event) => {
    if (el.hidden) return
    if (event.key === 'Escape') close()
    else if (event.key === 'ArrowRight') show(at + 1)
    else if (event.key === 'ArrowLeft') show(at - 1)
    // A keyboard has no pinch, and no cursor to zoom towards. The middle of the viewport is the
    // middle of the picture, which is where it sits.
    else if (event.key === '+' || event.key === '=')
      zoomAbout(scale * 1.4, innerWidth / 2, innerHeight / 2, true)
    else if (event.key === '-') zoomAbout(scale / 1.4, innerWidth / 2, innerHeight / 2, true)
    else if (event.key === '0') resetZoom(true)
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
      // A lone first finger starts a fresh sequence; anything beyond it makes the sequence a pinch.
      multiTouch = event.touches.length > 1
      startX = event.changedTouches[0].clientX
      startY = event.changedTouches[0].clientY
    },
    { passive: true },
  )
  el.addEventListener(
    'touchend',
    (event) => {
      // Zoomed in, a sideways drag is a pan across the picture, and the two fingers of a pinch
      // rarely lift level - either one read as a swipe pages away from what was just zoomed into.
      if (scale > 1 || multiTouch) return
      const dx = event.changedTouches[0].clientX - startX
      const dy = event.changedTouches[0].clientY - startY
      if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) show(at + (dx < 0 ? 1 : -1))
    },
    { passive: true },
  )
  bindZoom()
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
  // Zoom belongs to the picture it was aimed at, not to the frame: paging on at 4x would land in
  // the corner of the next photograph with no clue why.
  resetZoom()
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
