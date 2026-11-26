/**
 * Zoom for a picture in a viewer: pinch, drag and double-tap on touch; wheel, double-click and drag
 * with a pointer. Shared by the site-wide picture viewer (imageviewer.ts) and the photo album's.
 *
 * A CSS transform on the element rather than a bigger source: it costs no bytes and stays sharp up
 * to the file's own resolution.
 *
 * ZOOMS ABOUT THE FINGERS. Zooming about the middle instead slides the detail you aimed at off the
 * screen, which on a photograph magnified four times means hunting for it again after every step.
 *
 * A RESTING FINGER STILL BELONGS TO THE LONG PRESS - how a phone saves a picture to its photo
 * library. Nothing here calls preventDefault on a single-finger touchstart, which is what would
 * take it away; the pan claims the touch only once it has moved, and only while zoomed in.
 */

const MAX_ZOOM = 4
/** Where a double-tap lands. Enough to read a whiteboard, not so far that you lose the frame. */
const TAP_ZOOM = 2.5

export interface Zoom {
  readonly zoomed: boolean
  reset(animate?: boolean): void
  /** For the keyboard, which has no cursor to zoom towards: about the middle of the frame. */
  zoomBy(factor: number): void
  /** Whether a mouse drag just ended. The click it ends with is the end of a pan, not a click. */
  takeDrag(): boolean
}

export interface ZoomOptions {
  /** Told whenever the element goes from fitted to zoomed in, or back. */
  onChange?: (zoomed: boolean) => void
  /** Only pinch and the drag that follows it. A video's controls take taps, clicks and the wheel. */
  pinchOnly?: boolean
}

export function bindZoom(element: HTMLImageElement | HTMLVideoElement, options: ZoomOptions = {}): Zoom {
  // Typed as a plain element for the listeners: on the union, TypeScript loses each event's type.
  const surface: HTMLElement = element
  let scale = 1
  let tx = 0
  let ty = 0
  let wasZoomed = false
  /** A mouse drag just ended, so the click it ends on must not reach the backdrop and close. */
  let dragged = false

  /** The picture's own size on screen, unzoomed. Smaller than the box when object-fit letterboxes it. */
  const pictureSize = (): { width: number; height: number } => {
    const box = { width: element.clientWidth, height: element.clientHeight }
    const natural =
      element instanceof HTMLImageElement
        ? { width: element.naturalWidth, height: element.naturalHeight }
        : { width: element.videoWidth, height: element.videoHeight }
    if (!natural.width || !natural.height || !box.width || !box.height) return box
    const fit = Math.min(box.width / natural.width, box.height / natural.height)
    return { width: natural.width * fit, height: natural.height * fit }
  }

  /** How far the picture may be dragged: out to the box's edges, never past them into bare backdrop. */
  const panLimit = (): { x: number; y: number } => {
    const picture = pictureSize()
    return {
      x: Math.max(0, (picture.width * scale - element.clientWidth) / 2),
      y: Math.max(0, (picture.height * scale - element.clientHeight) / 2),
    }
  }

  const apply = (animate = false): void => {
    const limit = panLimit()
    tx = Math.min(limit.x, Math.max(-limit.x, tx))
    ty = Math.min(limit.y, Math.max(-limit.y, ty))
    element.style.transition = animate ? "transform .18s ease-out" : ""
    // Cleared rather than written as scale(1): an untouched picture then carries no transform at all
    // and keeps the browser's plain rendering path for it.
    element.style.transform = scale === 1 ? "" : `translate(${tx}px, ${ty}px) scale(${scale})`
    if (scale > 1 !== wasZoomed) {
      wasZoomed = scale > 1
      options.onChange?.(wasZoomed)
    }
  }

  /** Zoom towards `next`, holding whatever sits under (px, py) - the fingers, the cursor - in place. */
  const zoomAbout = (next: number, px: number, py: number, animate = false): void => {
    const to = Math.min(MAX_ZOOM, Math.max(1, next))
    const rect = element.getBoundingClientRect()
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
    apply(animate)
  }

  const reset = (animate = false): void => {
    scale = 1
    tx = 0
    ty = 0
    apply(animate)
  }

  const spread = (t: TouchList): number => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY)

  // A phone can synthesise a dblclick from a double-tap, which would undo the tap handler below
  // the moment it fired. The timestamp is what tells the two apart.
  let lastTouchEnd = 0

  if (!options.pinchOnly) {
    surface.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault()
        // Exponential, so one notch of the wheel feels the same at 1x as at 3x.
        zoomAbout(scale * Math.exp(-event.deltaY * 0.0025), event.clientX, event.clientY)
      },
      { passive: false },
    )

    surface.addEventListener("dblclick", (event) => {
      if (Date.now() - lastTouchEnd < 700) return
      event.preventDefault()
      if (scale > 1) reset(true)
      else zoomAbout(TAP_ZOOM, event.clientX, event.clientY, true)
    })

    surface.addEventListener("mousedown", (event) => {
      if (scale === 1 || event.button !== 0) return
      event.preventDefault() // otherwise the drag becomes the browser dragging the image away
      const fromX = event.clientX - tx
      const fromY = event.clientY - ty
      const move = (e: MouseEvent): void => {
        tx = e.clientX - fromX
        ty = e.clientY - fromY
        dragged = true
        apply()
      }
      const up = (): void => {
        removeEventListener("mousemove", move)
        removeEventListener("mouseup", up)
      }
      addEventListener("mousemove", move)
      addEventListener("mouseup", up)
    })
  }

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

  surface.addEventListener(
    "touchstart",
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

  surface.addEventListener(
    "touchmove",
    (event) => {
      if (event.touches.length === 2 && pinchFrom > 0) {
        event.preventDefault()
        const t = event.touches
        zoomAbout(pinchScale * (spread(t) / pinchFrom), (t[0].clientX + t[1].clientX) / 2, (t[0].clientY + t[1].clientY) / 2)
      } else if (panning && event.touches.length === 1) {
        event.preventDefault()
        tx = event.touches[0].clientX - panFromX
        ty = event.touches[0].clientY - panFromY
        moved = true
        apply()
      }
    },
    { passive: false },
  )

  surface.addEventListener("touchend", (event) => {
    if (event.touches.length === 1) {
      // A pinch losing one finger: the one left carries on as a pan, from where it is now rather
      // than from where it came down, or the picture would jump to meet it.
      panning = scale > 1
      panFromX = event.touches[0].clientX - tx
      panFromY = event.touches[0].clientY - ty
      pinchFrom = 0
      return
    }
    if (event.touches.length > 0) return
    lastTouchEnd = Date.now()
    pinchFrom = 0
    panning = false
    if (moved) {
      moved = false
      return
    }
    if (options.pinchOnly) return
    const t = event.changedTouches[0]
    const now = Date.now()
    // Two taps, close together in both time and place. A wandering second tap is a new first tap.
    if (now - lastTap < 300 && Math.hypot(t.clientX - lastTapX, t.clientY - lastTapY) < 30) {
      if (scale > 1) reset(true)
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
  addEventListener("resize", () => {
    if (scale > 1) apply()
  })

  return {
    get zoomed() {
      return scale > 1
    },
    reset,
    zoomBy: (factor) => {
      const rect = element.getBoundingClientRect()
      // The untransformed box's middle: where the frame is, not where the picture has been dragged.
      zoomAbout(scale * factor, rect.left + rect.width / 2 - tx, rect.top + rect.height / 2 - ty, true)
    },
    takeDrag: () => {
      const was = dragged
      dragged = false
      return was
    },
  }
}
