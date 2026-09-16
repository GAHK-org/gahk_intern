const gallery = document.querySelector<HTMLElement>("[data-album-gallery]")
const dialog = document.querySelector<HTMLDialogElement>("[data-album-viewer]")
const uploadInput = document.querySelector<HTMLInputElement>("[data-album-upload-input]")
const uploadSelection = document.querySelector<HTMLElement>("[data-album-upload-selection]")
const uploadCount = document.querySelector<HTMLElement>("[data-album-upload-count]")
const uploadFiles = document.querySelector<HTMLUListElement>("[data-album-upload-files]")

if (uploadInput && uploadSelection && uploadCount && uploadFiles) {
  uploadInput.addEventListener("change", () => {
    const files = Array.from(uploadInput.files ?? [])
    uploadSelection.hidden = files.length === 0
    uploadCount.textContent = `${files.length} ${files.length === 1 ? "fil valgt" : "filer valgt"}`
    uploadFiles.replaceChildren(...files.map((file) => {
      const item = document.createElement("li")
      item.textContent = file.name
      return item
    }))
  })
}

if (gallery && dialog) {
  const entries = Array.from(
    gallery.querySelectorAll<HTMLButtonElement>(".album-media-tile[data-detail-url]"),
  )
  const image = dialog.querySelector<HTMLImageElement>("[data-gallery-image]")!
  const video = dialog.querySelector<HTMLVideoElement>("[data-gallery-video]")!
  const title = dialog.querySelector<HTMLElement>("[data-gallery-title]")!
  const metadata = dialog.querySelector<HTMLDListElement>("[data-gallery-metadata]")!
  const menuMetadata = dialog.querySelector<HTMLDListElement>("[data-gallery-menu-metadata]")!
  const mobileDate = dialog.querySelector<HTMLElement>("[data-gallery-mobile-date]")!
  const download = dialog.querySelector<HTMLAnchorElement>("[data-gallery-download]")!
  const menuDownload = dialog.querySelector<HTMLAnchorElement>("[data-gallery-menu-download]")!
  const deleteForm = dialog.querySelector<HTMLFormElement>("[data-gallery-delete-form]")!
  const menuDeleteForm = dialog.querySelector<HTMLFormElement>("[data-gallery-menu-delete-form]")!
  const menuButton = dialog.querySelector<HTMLButtonElement>("[data-gallery-menu-button]")!
  const menu = dialog.querySelector<HTMLElement>("[data-gallery-menu]")!
  const stage = dialog.querySelector<HTMLElement>(".album-viewer-stage")!
  const previous = dialog.querySelector<HTMLButtonElement>("[data-gallery-previous]")!
  const next = dialog.querySelector<HTMLButtonElement>("[data-gallery-next]")!
  let current = 0
  let touchStart: { x: number; y: number } | undefined
  let zoomScale = 1
  let pinchStartDistance = 0
  let pinchStartScale = 1

  const touchDistance = (touches: TouchList): number =>
    Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY)

  const resetZoom = (): void => {
    zoomScale = 1
    image.style.transform = ""
    video.style.transform = ""
  }

  const applyZoom = (): void => {
    const transform = zoomScale === 1 ? "" : `scale(${zoomScale})`
    image.style.transform = transform
    video.style.transform = transform
  }

  interface MediaDetail {
    kind: "image" | "video"
    title: string
    full: string
    album: string
    downloadUrl: string
    deleteUrl: string
    uploadedBy: string
    uploadedAt: string
    capturedAt: string
    metadata: Record<string, string>
  }

  // One entry per item, kept for as long as the page lives. An album is browsed by paging through
  // it, so the same handful of items get reopened constantly and re-fetching them would be silly.
  const details = new Map<string, Promise<MediaDetail | null>>()

  const detailFor = (entry: HTMLButtonElement): Promise<MediaDetail | null> => {
    const url = entry.dataset.detailUrl ?? ""
    const cached = details.get(url)
    if (cached) return cached
    const pending = fetch(url, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then((response) => (response.ok ? (response.json() as Promise<MediaDetail>) : null))
      .catch(() => null)
    details.set(url, pending)
    return pending
  }

  const renderMetadata = (detail: MediaDetail): void => {
    const values = {
      ...(detail.album ? { Album: detail.album } : {}),
      ...(detail.capturedAt ? { "Optaget": detail.capturedAt } : {}),
      "Uploadet af": detail.uploadedBy,
      Uploadet: detail.uploadedAt,
      ...detail.metadata,
    }
    const metadataEntries = Object.entries(values).flatMap(([key, value]) => {
      const term = document.createElement("dt")
      term.textContent = key
      const definition = document.createElement("dd")
      definition.textContent = value
      return [term, definition]
    })
    metadata.replaceChildren(...metadataEntries)
    menuMetadata.replaceChildren(...metadataEntries.map((node) => node.cloneNode(true)))
  }

  const show = (index: number): void => {
    if (index < 0 || index >= entries.length) return
    current = index
    resetZoom()
    const entry = entries[current]

    // The thumbnail is already decoded in the grid, so showing it stretched is instant and the
    // viewer never opens on a blank stage. The real image replaces it when the fetch lands.
    const thumbnail = entry.querySelector("img")
    image.hidden = false
    video.hidden = true
    video.src = ""
    image.src = thumbnail?.src ?? ""
    image.alt = thumbnail?.alt ?? ""
    title.textContent = thumbnail?.alt ?? ""
    mobileDate.textContent = ""
    metadata.replaceChildren()
    menuMetadata.replaceChildren()
    deleteForm.hidden = true
    menuDeleteForm.hidden = true
    menu.hidden = true
    menuButton.setAttribute("aria-expanded", "false")
    previous.disabled = current === 0
    next.disabled = current === entries.length - 1

    void detailFor(entry).then((detail) => {
      // Paged on while this was in flight: the answer is about a different photograph now.
      if (!detail || entries[current] !== entry) return
      const isVideo = detail.kind === "video"
      image.hidden = isVideo
      video.hidden = !isVideo
      if (isVideo) {
        image.src = ""
        video.src = detail.full
      } else if (detail.full) {
        image.src = detail.full
      }
      image.alt = detail.title
      title.textContent = detail.title
      mobileDate.textContent = detail.capturedAt || detail.uploadedAt
      download.href = detail.downloadUrl
      menuDownload.href = detail.downloadUrl
      deleteForm.hidden = !detail.deleteUrl
      deleteForm.action = detail.deleteUrl
      menuDeleteForm.hidden = !detail.deleteUrl
      menuDeleteForm.action = detail.deleteUrl
      renderMetadata(detail)
      // Warm the neighbours, so paging with the arrows or a swipe does not wait on the network.
      for (const neighbour of [entries[current - 1], entries[current + 1]]) {
        if (neighbour) void detailFor(neighbour)
      }
    })
  }

  entries.forEach((entry, index) => entry.addEventListener("click", () => { dialog.showModal(); show(index) }))
  dialog.querySelector("[data-gallery-close]")?.addEventListener("click", () => dialog.close())
  menuButton.addEventListener("click", () => {
    menu.hidden = !menu.hidden
    menuButton.setAttribute("aria-expanded", String(!menu.hidden))
  })
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close()
  })
  previous.addEventListener("click", () => show(current - 1))
  next.addEventListener("click", () => show(current + 1))
  stage.addEventListener("touchstart", (event) => {
    if (!window.matchMedia("(max-width: 720px)").matches) return
    if (event.touches.length === 2) {
      pinchStartDistance = touchDistance(event.touches)
      pinchStartScale = zoomScale
      touchStart = undefined
      return
    }
    const touch = event.touches[0]
    if (zoomScale === 1 && touch) {
      touchStart = { x: touch.clientX, y: touch.clientY }
    }
  }, { passive: true })
  stage.addEventListener("touchmove", (event) => {
    if (event.touches.length !== 2 || pinchStartDistance === 0) return
    event.preventDefault()
    zoomScale = Math.min(4, Math.max(1, pinchStartScale * (touchDistance(event.touches) / pinchStartDistance)))
    applyZoom()
  }, { passive: false })
  stage.addEventListener("touchend", (event) => {
    const touch = event.changedTouches[0]
    if (!touchStart || !touch || !window.matchMedia("(max-width: 720px)").matches) return
    const horizontalDistance = touch.clientX - touchStart.x
    const verticalDistance = touch.clientY - touchStart.y
    touchStart = undefined
    if (Math.abs(horizontalDistance) < 48 || Math.abs(horizontalDistance) <= Math.abs(verticalDistance)) return
    show(horizontalDistance < 0 ? current + 1 : current - 1)
  }, { passive: true })
  stage.addEventListener("touchend", () => {
    if (pinchStartDistance > 0) pinchStartDistance = 0
  }, { passive: true })
  document.addEventListener("keydown", (event) => {
    if (!dialog.open) return
    if (event.key === "ArrowLeft") show(current - 1)
    if (event.key === "ArrowRight") show(current + 1)
  })
}