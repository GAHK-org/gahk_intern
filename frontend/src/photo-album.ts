const gallery = document.querySelector<HTMLElement>("[data-album-gallery]")
const dialog = document.querySelector<HTMLDialogElement>("[data-album-viewer]")
const uploadInput = document.querySelector<HTMLInputElement>("[data-album-upload-input]")
const uploadSelection = document.querySelector<HTMLElement>("[data-album-upload-selection]")
const uploadCount = document.querySelector<HTMLElement>("[data-album-upload-count]")
const uploadFiles = document.querySelector<HTMLUListElement>("[data-album-upload-files]")
const uploadDialog = document.querySelector<HTMLDialogElement>("[data-album-upload-dialog]")
const uploadForm = document.querySelector<HTMLFormElement>("[data-album-upload-form]")
const uploadProgress = document.querySelector<HTMLElement>("[data-album-upload-progress]")
const uploadProgressBar = document.querySelector<HTMLProgressElement>("[data-album-upload-progress-bar]")
const uploadProgressLabel = document.querySelector<HTMLElement>("[data-album-upload-progress-label]")
const uploadSubmit = document.querySelector<HTMLButtonElement>("[data-album-upload-submit]")
const zipImportForm = document.querySelector<HTMLFormElement>("[data-album-zip-import]")
const zipImportJob = document.querySelector<HTMLElement>("[data-album-import-job]")

const pollZipImport = (root: HTMLElement, statusUrl: string, resultUrl: string): void => {
  const label = root.querySelector<HTMLElement>("[data-album-import-progress-label]")!
  window.setTimeout(() => {
    void fetch(statusUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then((response) => response.json() as Promise<{ state: string; error: string }>)
      .then((job) => {
        if (job.state === "ready") {
          window.location.assign(resultUrl)
          return
        }
        if (job.state === "failed") {
          label.textContent = `Importen mislykkedes: ${job.error || "ukendt fejl"}`
          return
        }
        label.textContent = job.state === "building"
          ? "ZIP-filen er uploadet. Serveren importerer nu filerne …"
          : "ZIP-filen er uploadet. Serveren venter på at starte importen …"
        pollZipImport(root, statusUrl, resultUrl)
      })
      .catch(() => {
        label.textContent = "Kunne ikke hente importens status. Prøver igen …"
        pollZipImport(root, statusUrl, resultUrl)
      })
  }, 1500)
}

if (zipImportJob) pollZipImport(zipImportJob, zipImportJob.dataset.statusUrl!, zipImportJob.dataset.resultUrl!)

if (zipImportForm) {
  const archive = zipImportForm.querySelector<HTMLInputElement>("[name=archive]")!
  const submit = zipImportForm.querySelector<HTMLButtonElement>("[data-album-zip-import-submit]")!
  const progress = zipImportForm.querySelector<HTMLElement>("[data-album-import-progress]")!
  const progressBar = progress.querySelector<HTMLProgressElement>("[data-album-import-progress-bar]")!
  const label = progress.querySelector<HTMLElement>("[data-album-import-progress-label]")!
  const setFormDisabled = (disabled: boolean): void => {
    zipImportForm.querySelectorAll<HTMLInputElement | HTMLButtonElement>("input, button").forEach((control) => {
      control.disabled = disabled
    })
  }
  zipImportForm.addEventListener("submit", (event) => {
    if (!archive.files?.length) return
    event.preventDefault()
    const data = new FormData(zipImportForm)
    const request = new XMLHttpRequest()
    setFormDisabled(true)
    zipImportForm.classList.add("is-submitted")
    submit.textContent = "Upload i gang …"
    progress.hidden = false
    label.textContent = "Uploader ZIP-fil: 0%"
    request.open("POST", zipImportForm.action)
    request.setRequestHeader("Accept", "application/json")
    request.upload.addEventListener("progress", (progressEvent) => {
      if (!progressEvent.lengthComputable) return
      const percent = Math.round((progressEvent.loaded / progressEvent.total) * 100)
      progressBar.value = percent
      label.textContent = `Uploader ZIP-fil: ${percent}%`
    })
    request.addEventListener("load", () => {
      if (request.status !== 202) {
        label.textContent = "ZIP-filen kunne ikke uploades. Genindlæs siden for at prøve igen."
        return
      }
      const response = JSON.parse(request.responseText) as { statusUrl: string; resultUrl: string }
      progressBar.removeAttribute("value")
      submit.textContent = "Importerer …"
      label.textContent = "ZIP-filen er uploadet. Serveren importerer nu filerne …"
      pollZipImport(progress, response.statusUrl, response.resultUrl)
    })
    request.addEventListener("error", () => {
      label.textContent = "ZIP-filen kunne ikke uploades. Genindlæs siden for at prøve igen."
    })
    request.send(data)
  })
}
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

if (uploadDialog && uploadForm && uploadInput && uploadProgress && uploadProgressBar && uploadProgressLabel && uploadSubmit) {
  const openUpload = document.querySelector<HTMLButtonElement>("[data-album-upload-open]")
  const closeUpload = (): void => uploadDialog.close()

  openUpload?.addEventListener("click", () => uploadDialog.showModal())
  uploadDialog.querySelectorAll<HTMLElement>("[data-album-upload-close]").forEach((button) => button.addEventListener("click", closeUpload))
  uploadDialog.addEventListener("click", (event) => {
    if (event.target === uploadDialog) closeUpload()
  })

  uploadForm.addEventListener("submit", (event) => {
    const files = Array.from(uploadInput.files ?? [])
    if (!files.length) return
    event.preventDefault()

    const totalBytes = files.reduce((sum, file) => sum + file.size, 0)
    let completedBytes = 0
    let fileIndex = 0
    const formData = new FormData(uploadForm)
    const title = formData.get("title")?.toString() ?? ""
    const csrfToken = formData.get("csrfmiddlewaretoken")?.toString() ?? ""
    uploadSubmit.disabled = true
    uploadProgress.hidden = false

    const setProgress = (loadedBytes: number): void => {
      const percent = totalBytes ? Math.round(((completedBytes + loadedBytes) / totalBytes) * 100) : 100
      uploadProgressBar.value = percent
      uploadProgressLabel.textContent = `Uploader fil ${fileIndex + 1} af ${files.length}: ${percent}%`
    }

    const uploadNext = (): void => {
      const file = files[fileIndex]
      if (!file) {
        window.location.reload()
        return
      }
      const data = new FormData()
      data.append("uploads", file)
      data.append("title", title)
      data.append("csrfmiddlewaretoken", csrfToken)
      const request = new XMLHttpRequest()
      request.open("POST", uploadForm.action)
      request.setRequestHeader("Accept", "application/json")
      request.upload.addEventListener("progress", (progressEvent) => {
        if (progressEvent.lengthComputable) setProgress(progressEvent.loaded)
      })
      request.addEventListener("load", () => {
        if (request.status >= 200 && request.status < 300) {
          completedBytes += file.size
          fileIndex += 1
          setProgress(0)
          uploadNext()
          return
        }
        uploadSubmit.disabled = false
        uploadProgressLabel.textContent = `Kunne ikke uploade ${file.name}. Prøv igen.`
      })
      request.addEventListener("error", () => {
        uploadSubmit.disabled = false
        uploadProgressLabel.textContent = `Kunne ikke uploade ${file.name}. Kontrollér din forbindelse og prøv igen.`
      })
      request.send(data)
    }

    setProgress(0)
    uploadNext()
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