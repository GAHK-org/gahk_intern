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
  const download = dialog.querySelector<HTMLAnchorElement>("[data-gallery-download]")!
  const deleteForm = dialog.querySelector<HTMLFormElement>("[data-gallery-delete-form]")!
  let current = 0

  const metadataFor = (entry: HTMLButtonElement): Record<string, string> => {
    try {
      return JSON.parse(entry.dataset.metadata ?? "{}") as Record<string, string>
    } catch {
      return {}
    }
  }

  const show = (index: number): void => {
    current = (index + entries.length) % entries.length
    const entry = entries[current]
    const isVideo = entry.dataset.kind === "video"
    image.hidden = isVideo
    video.hidden = !isVideo
    image.src = isVideo ? "" : (entry.dataset.full ?? "")
    image.alt = entry.dataset.title ?? ""
    video.src = isVideo ? (entry.dataset.full ?? "") : ""
    title.textContent = entry.dataset.title ?? ""
    download.href = entry.dataset.downloadUrl ?? ""
    deleteForm.hidden = !entry.dataset.deleteUrl
    deleteForm.action = entry.dataset.deleteUrl ?? ""
    const values = {
      ...(entry.dataset.album ? { Album: entry.dataset.album } : {}),
      "Uploadet af": entry.dataset.uploadedBy ?? "",
      Uploadet: entry.dataset.uploadedAt ?? "",
      ...metadataFor(entry),
    }
    metadata.replaceChildren(...Object.entries(values).flatMap(([key, value]) => {
      const term = document.createElement("dt")
      term.textContent = key
      const detail = document.createElement("dd")
      detail.textContent = value
      return [term, detail]
    }))
  }

  entries.forEach((entry, index) => entry.addEventListener("click", () => { dialog.showModal(); show(index) }))
  dialog.querySelector("[data-gallery-close]")?.addEventListener("click", () => dialog.close())
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close()
  })
  dialog.querySelector("[data-gallery-previous]")?.addEventListener("click", () => show(current - 1))
  dialog.querySelector("[data-gallery-next]")?.addEventListener("click", () => show(current + 1))
  document.addEventListener("keydown", (event) => {
    if (!dialog.open) return
    if (event.key === "ArrowLeft") show(current - 1)
    if (event.key === "ArrowRight") show(current + 1)
  })
}