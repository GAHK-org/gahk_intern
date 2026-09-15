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
  const entries = Array.from(gallery.querySelectorAll<HTMLButtonElement>(".album-media-tile"))
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

  const metadataFor = (entry: HTMLButtonElement): Record<string, string> => {
    try {
      return JSON.parse(entry.dataset.metadata ?? "{}") as Record<string, string>
    } catch {
      return {}
    }
  }

  const show = (index: number): void => {
    if (index < 0 || index >= entries.length) return
    current = index
    const entry = entries[current]
    const isVideo = entry.dataset.kind === "video"
    image.hidden = isVideo
    video.hidden = !isVideo
    image.src = isVideo ? "" : (entry.dataset.full ?? "")
    image.alt = entry.dataset.title ?? ""
    video.src = isVideo ? (entry.dataset.full ?? "") : ""
    title.textContent = entry.dataset.title ?? ""
    mobileDate.textContent = entry.dataset.capturedAt ?? entry.dataset.uploadedAt ?? ""
    download.href = entry.dataset.downloadUrl ?? ""
    menuDownload.href = entry.dataset.downloadUrl ?? ""
    deleteForm.hidden = !entry.dataset.deleteUrl
    deleteForm.action = entry.dataset.deleteUrl ?? ""
    menuDeleteForm.hidden = !entry.dataset.deleteUrl
    menuDeleteForm.action = entry.dataset.deleteUrl ?? ""
    menu.hidden = true
    menuButton.setAttribute("aria-expanded", "false")
    previous.disabled = current === 0
    next.disabled = current === entries.length - 1
    const values = {
      ...(entry.dataset.album ? { Album: entry.dataset.album } : {}),
      ...(entry.dataset.capturedAt ? { "Optaget": entry.dataset.capturedAt } : {}),
      "Uploadet af": entry.dataset.uploadedBy ?? "",
      Uploadet: entry.dataset.uploadedAt ?? "",
      ...metadataFor(entry),
    }
    const metadataEntries = Object.entries(values).flatMap(([key, value]) => {
      const term = document.createElement("dt")
      term.textContent = key
      const detail = document.createElement("dd")
      detail.textContent = value
      return [term, detail]
    })
    metadata.replaceChildren(...metadataEntries)
    menuMetadata.replaceChildren(...metadataEntries.map((entry) => entry.cloneNode(true)))
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
    const touch = event.touches[0]
    if (window.matchMedia("(max-width: 720px)").matches && touch) {
      touchStart = { x: touch.clientX, y: touch.clientY }
    }
  }, { passive: true })
  stage.addEventListener("touchend", (event) => {
    const touch = event.changedTouches[0]
    if (!touchStart || !touch || !window.matchMedia("(max-width: 720px)").matches) return
    const horizontalDistance = touch.clientX - touchStart.x
    const verticalDistance = touch.clientY - touchStart.y
    touchStart = undefined
    if (Math.abs(horizontalDistance) < 48 || Math.abs(horizontalDistance) <= Math.abs(verticalDistance)) return
    show(horizontalDistance < 0 ? current + 1 : current - 1)
  }, { passive: true })
  document.addEventListener("keydown", (event) => {
    if (!dialog.open) return
    if (event.key === "ArrowLeft") show(current - 1)
    if (event.key === "ArrowRight") show(current + 1)
  })
}