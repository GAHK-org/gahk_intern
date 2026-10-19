import { beginDirect, DirectUnavailable, type DirectUpload, refusal, sendToBucket, UploadRefused } from "./directupload"
import { csrfToken } from "./htmx-csrf"

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
const uploadNotes = document.querySelector<HTMLUListElement>("[data-album-upload-notes]")
const zipImportForm = document.querySelector<HTMLFormElement>("[data-album-zip-import]")
const zipImportJob = document.querySelector<HTMLElement>("[data-album-import-job]")

document.querySelectorAll<HTMLElement>("[data-album-folder-selector]").forEach((selector) => {
  const choice = selector.querySelector<HTMLSelectElement>("[data-album-folder-choice]")!
  const input = selector.querySelector<HTMLInputElement>("[data-album-folder-input]")!
  const currentValue = selector.dataset.currentValue ?? "Andet"
  choice.value = Array.from(choice.options).some((option) => option.value === currentValue)
    ? currentValue
    : "__new__"

  const syncFolderInput = (): void => {
    const isNewFolder = choice.value === "__new__"
    if (isNewFolder) {
      input.value = ""
      input.focus()
    } else {
      input.value = choice.value
    }
  }

  choice.addEventListener("change", syncFolderInput)
  syncFolderInput()
})

const pollZipImport = (root: HTMLElement, statusUrl: string, resultUrl: string): void => {
  const label = root.querySelector<HTMLElement>("[data-album-import-progress-label]")!
  window.setTimeout(() => {
    void fetch(statusUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then((response) => response.json() as Promise<{ state: string; error: string; errors?: string[] }>)
      .then((job) => {
        if (job.state === "ready") {
          window.location.assign(resultUrl)
          return
        }
        if (job.state === "failed") {
          // The result page lists every reason the archive was refused, not just the summary.
          if (job.errors?.length) {
            window.location.assign(resultUrl)
            return
          }
          root.classList.remove("is-working")
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
  const showPercent = (percent: number): void => {
    if (percent < 100) {
      progressBar.value = percent
      label.textContent = `Uploader ZIP-fil: ${percent}%`
      return
    }
    // Every byte is sent, but the server still stores the archive before it answers.
    progressBar.removeAttribute("value")
    progress.classList.add("is-working")
    submit.textContent = "Behandler …"
    label.textContent = "ZIP-filen er uploadet. Serveren behandler den …"
  }
  const failed = (reason = ""): void => {
    progress.classList.remove("is-working")
    label.textContent = `ZIP-filen kunne ikke uploades. ${reason || "Genindlæs siden for at prøve igen."}`
  }
  // The import itself: with the ZIP in it, or with the ticket of one already in the bucket.
  const post = (data: FormData): void => {
    const request = new XMLHttpRequest()
    request.open("POST", zipImportForm.action)
    request.setRequestHeader("Accept", "application/json")
    request.upload.addEventListener("progress", (progressEvent) => {
      if (progressEvent.lengthComputable) showPercent(Math.round((progressEvent.loaded / progressEvent.total) * 100))
    })
    request.addEventListener("load", () => {
      if (request.status !== 202) {
        let reason = ""
        try {
          reason = refusal(JSON.parse(request.responseText))
        } catch {
          // Not JSON — a proxy error page, most likely.
        }
        failed(reason)
        return
      }
      const response = JSON.parse(request.responseText) as { statusUrl: string; resultUrl: string }
      progressBar.removeAttribute("value")
      progress.classList.add("is-working")
      submit.textContent = "Importerer …"
      label.textContent = "ZIP-filen er uploadet. Serveren importerer nu filerne …"
      pollZipImport(progress, response.statusUrl, response.resultUrl)
    })
    request.addEventListener("error", () => failed())
    request.send(data)
  }
  // Straight to the bucket when the server has one: through the app, the proxy cuts any upload
  // that takes more than a minute. False when it has none, or the bucket cannot be reached from here.
  const sendDirect = async (file: File, fields: FormData): Promise<boolean> => {
    const { directBeginUrl: beginUrl, directSignUrl: signUrl } = zipImportForm.dataset
    if (!beginUrl || !signUrl) return false
    let upload: DirectUpload | null = null
    try {
      upload = await beginDirect(beginUrl, { name: file.name, size: String(file.size) })
    } catch (error) {
      if (!(error instanceof UploadRefused)) return false
      failed(error.message)
      return true
    }
    if (!upload) return false
    try {
      await sendToBucket(file, upload, signUrl, (sent) => showPercent(Math.min(99, Math.round((sent / file.size) * 100))))
      fields.append("ticket", upload.ticket)
      post(fields)
    } catch (error) {
      if (error instanceof DirectUnavailable) return false
      failed(error instanceof UploadRefused ? error.message : "")
    }
    return true
  }
  zipImportForm.addEventListener("submit", (event) => {
    const file = archive.files?.[0]
    if (!file) return
    event.preventDefault()
    const data = new FormData(zipImportForm) // before the form is disabled, which leaves its fields out
    setFormDisabled(true)
    zipImportForm.classList.add("is-submitted")
    submit.textContent = "Upload i gang …"
    progress.hidden = false
    label.textContent = "Uploader ZIP-fil: 0%"
    const fields = new FormData() // the form without its file, for an import of one already in the bucket
    for (const [key, value] of data.entries()) if (key !== "archive") fields.append(key, value)
    void sendDirect(file, fields).then((handled) => {
      if (!handled) post(data)
    })
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

interface UploadResult {
  uploaded: number
  skipped: string[]
  warnings: string[]
}

// From this size a photo or video goes straight to the bucket. Below it, a file is sent with the
// form as it always was: it is in well within the proxy's minute even on a slow line, and a phone's
// photos need no extra round trips.
const DIRECT_FROM_BYTES = 8 * 1024 * 1024
// Once the bucket has not answered this browser, the rest of the upload goes with the form.
let directBlocked = false

if (uploadDialog && uploadForm && uploadInput && uploadProgress && uploadProgressBar && uploadProgressLabel && uploadSubmit) {
  const openUpload = document.querySelector<HTMLButtonElement>("[data-album-upload-open]")
  let finished = false
  const closeUpload = (): void => {
    uploadDialog.close()
    // Whatever went up is in the album now; show it.
    if (finished) window.location.reload()
  }

  openUpload?.addEventListener("click", () => uploadDialog.showModal())
  uploadDialog.querySelectorAll<HTMLElement>("[data-album-upload-close]").forEach((button) => button.addEventListener("click", closeUpload))
  uploadDialog.addEventListener("click", (event) => {
    if (event.target === uploadDialog) closeUpload()
  })

  uploadForm.addEventListener("submit", (event) => {
    const files = Array.from(uploadInput.files ?? [])
    if (!files.length) return
    event.preventDefault()
    if (finished) {
      window.location.reload()
      return
    }

    const totalBytes = files.reduce((sum, file) => sum + file.size, 0)
    let completedBytes = 0
    let fileIndex = 0
    let uploaded = 0
    const notes: string[] = []
    const formData = new FormData(uploadForm)
    const title = formData.get("title")?.toString() ?? ""
    const csrf = formData.get("csrfmiddlewaretoken")?.toString() ?? ""
    uploadSubmit.disabled = true
    uploadProgress.hidden = false
    uploadNotes?.replaceChildren()

    const setProgress = (loadedBytes: number): void => {
      const percent = totalBytes ? Math.round(((completedBytes + loadedBytes) / totalBytes) * 100) : 100
      uploadProgressBar.value = percent
      uploadProgressLabel.textContent = `Uploader fil ${fileIndex + 1} af ${files.length}: ${percent}%`
    }

    const finish = (): void => {
      if (!notes.length) {
        window.location.reload()
        return
      }
      // Skipped duplicates and "also in another album" are worth reading before the page reloads.
      finished = true
      uploadProgressLabel.textContent = `${uploaded} af ${files.length} filer er uploadet.`
      uploadNotes?.replaceChildren(...notes.map((note) => {
        const item = document.createElement("li")
        item.textContent = note
        return item
      }))
      uploadSubmit.disabled = false
      uploadSubmit.textContent = "Færdig"
    }

    // One file is in: on to the next. Or it was refused, and the queue stops there, as it always has.
    const stored = (file: File, result: UploadResult | null): void => {
      uploaded += result ? result.uploaded : 1
      if (result) notes.push(...result.skipped, ...result.warnings)
      completedBytes += file.size
      fileIndex += 1
      setProgress(0)
      uploadNext()
    }
    const refused = (file: File, reason: string): void => {
      uploadSubmit.disabled = false
      uploadProgressLabel.textContent = `Kunne ikke uploade ${file.name}. ${reason || "Prøv igen."}`
    }

    const sendWithForm = (file: File): void => {
      const data = new FormData()
      data.append("uploads", file)
      data.append("title", title)
      // The camera's own clock is not in every file; the file's date is the next best thing.
      data.append("last_modified", String(file.lastModified))
      data.append("csrfmiddlewaretoken", csrf)
      const request = new XMLHttpRequest()
      request.open("POST", uploadForm.action)
      request.setRequestHeader("Accept", "application/json")
      request.upload.addEventListener("progress", (progressEvent) => {
        if (progressEvent.lengthComputable) setProgress(progressEvent.loaded)
      })
      request.addEventListener("load", () => {
        if (request.status >= 200 && request.status < 300) {
          let result: UploadResult | null = null
          try {
            result = JSON.parse(request.responseText) as UploadResult
          } catch {
            // Stored, by the status; nothing to note.
          }
          stored(file, result)
          return
        }
        let reason = ""
        try {
          reason = refusal(JSON.parse(request.responseText))
        } catch {
          // Not JSON — a proxy error page, most likely. The generic message below covers it.
        }
        refused(file, reason)
      })
      request.addEventListener("error", () => refused(file, "Kontrollér din forbindelse og prøv igen."))
      request.send(data)
    }

    // A large file goes straight to the bucket in parts — through the app, the proxy cuts any upload
    // that takes more than a minute — and is then checked and stored as the form would. False when the
    // server has no bucket or this browser cannot reach it: the file is then sent with the form.
    const sendDirect = async (file: File): Promise<boolean> => {
      const { directBeginUrl: beginUrl, directSignUrl: signUrl, directCommitUrl: commitUrl } = uploadForm.dataset
      if (!beginUrl || !signUrl || !commitUrl || directBlocked || file.size < DIRECT_FROM_BYTES) return false
      let upload: DirectUpload | null
      try {
        upload = await beginDirect(beginUrl, { name: file.name, size: String(file.size), content_type: file.type })
      } catch (error) {
        if (!(error instanceof UploadRefused)) return false
        refused(file, error.message)
        return true
      }
      if (!upload) return false
      try {
        await sendToBucket(file, upload, signUrl, setProgress)
        uploadProgressLabel.textContent = `Kontrollerer ${file.name} …`
        const data = new FormData()
        data.append("ticket", upload.ticket)
        data.append("title", title)
        data.append("last_modified", String(file.lastModified))
        const response = await fetch(commitUrl, {
          method: "POST",
          body: data,
          credentials: "same-origin",
          headers: { Accept: "application/json", "X-CSRFToken": csrf },
        })
        const body: unknown = await response.json().catch(() => null)
        if (response.ok) stored(file, body as UploadResult)
        else refused(file, refusal(body))
      } catch (error) {
        if (error instanceof DirectUnavailable) {
          directBlocked = true
          return false
        }
        refused(file, error instanceof UploadRefused ? error.message : "Kontrollér din forbindelse og prøv igen.")
      }
      return true
    }

    const uploadNext = (): void => {
      const file = files[fileIndex]
      if (!file) {
        finish()
        return
      }
      void sendDirect(file).then((handled) => {
        if (!handled) sendWithForm(file)
      })
    }

    setProgress(0)
    uploadNext()
  })
}

// --- Paging ----------------------------------------------------------------------------------
//
// The grid starts as one page around wherever the reader arrived (the top, a year, a deep-linked
// photo). A sentinel at either end says where the next page in that direction comes from; walking
// into it fetches that page as a fragment of tiles (photo_album/_media_tiles.html). Pages far
// behind the reader are dropped again, so a long scroll through the timeline does not keep
// thousands of images decoded.

const MAX_TILES = 480
const tileSelector = "article.album-media-item"

const pageUrl = (direction: "before" | "after", pk: string): string => {
  const url = new URL(window.location.href)
  for (const key of ["after", "before", "media", "year"]) url.searchParams.delete(key)
  url.searchParams.set(direction, pk)
  return `${url.pathname}?${url.searchParams.toString()}`
}

const sentinel = (direction: "before" | "after", url: string): HTMLElement => {
  const element = document.createElement("div")
  element.className = "album-page-sentinel"
  element.setAttribute("aria-hidden", "true")
  element.dataset[direction === "before" ? "pageBefore" : "pageAfter"] = url
  return element
}

const monthRuler = (tile: HTMLElement): HTMLElement => {
  const ruler = document.createElement("h2")
  ruler.className = "album-month-ruler"
  ruler.dataset.month = tile.dataset.month ?? ""
  ruler.textContent = tile.dataset.monthLabel ?? ""
  return ruler
}

/** A ruler above the first tile of every month, on the timeline. Pages arrive and leave at both
 * ends, and a page can start or end mid-month, so the rulers are squared up against the tiles after
 * every change: one where the month changes (and above the first tile), none anywhere else — not
 * between two tiles of one month where two pages meet, nor left behind by tiles that were dropped. */
const markMonths = (gallery: HTMLElement): void => {
  if (!gallery.hasAttribute("data-album-months")) return
  let previous: string | undefined
  let ruler: HTMLElement | undefined
  for (const child of Array.from(gallery.children) as HTMLElement[]) {
    if (child.matches(".album-month-ruler")) {
      ruler?.remove()
      ruler = child
    } else if (child.matches(tileSelector)) {
      const month = child.dataset.month ?? ""
      if (month === previous) ruler?.remove()
      else if (ruler?.dataset.month !== month) {
        ruler?.remove()
        child.before(monthRuler(child))
      }
      ruler = undefined
      previous = month
    }
  }
  ruler?.remove()
}

/** Keep `anchor` exactly where it is on screen while `change` adds or removes things above it. */
const holdPosition = (anchor: Element | null, change: () => void): void => {
  const before = anchor?.getBoundingClientRect().top
  change()
  if (anchor && before !== undefined && anchor.isConnected) {
    window.scrollBy(0, anchor.getBoundingClientRect().top - before)
  }
}

const setupPaging = (gallery: HTMLElement, onTiles: (tiles: HTMLElement[]) => void, canUnload: () => boolean): {
  loadAfter: () => Promise<boolean>
} => {
  const loading = new Set<string>()

  const tiles = (): HTMLElement[] => Array.from(gallery.querySelectorAll<HTMLElement>(tileSelector))

  const fetchPage = async (url: string): Promise<DocumentFragment | null> => {
    const response = await fetch(url, { credentials: "same-origin" })
    if (!response.ok) return null
    const template = document.createElement("template")
    template.innerHTML = await response.text()
    return template.content
  }

  const unload = (from: "top" | "bottom"): void => {
    if (!canUnload()) return
    const all = tiles()
    const excess = all.length - MAX_TILES
    if (excess <= 0) return
    if (from === "top") {
      const doomed = all.slice(0, excess)
      const firstKept = all[excess]
      holdPosition(firstKept, () => {
        gallery.querySelector("[data-page-before]")?.remove()
        doomed.forEach((tile) => tile.remove())
        gallery.prepend(sentinel("before", pageUrl("before", firstKept.dataset.mediaId ?? "")))
        markMonths(gallery)
      })
      observeSentinels()
    } else {
      const lastKept = all[all.length - excess - 1]
      gallery.querySelector("[data-page-after]")?.remove()
      all.slice(all.length - excess).forEach((tile) => tile.remove())
      gallery.append(sentinel("after", pageUrl("after", lastKept.dataset.mediaId ?? "")))
      markMonths(gallery)
      observeSentinels()
    }
  }

  const load = async (element: HTMLElement): Promise<boolean> => {
    const before = element.dataset.pageBefore
    const url = before ?? element.dataset.pageAfter
    if (!url || loading.has(url)) return false
    loading.add(url)
    element.classList.add("is-loading")
    try {
      const fragment = await fetchPage(url)
      if (!fragment || !element.isConnected) return false
      const added = Array.from(fragment.querySelectorAll<HTMLElement>(tileSelector))
      if (before) {
        holdPosition(gallery.querySelector(tileSelector), () => {
          element.replaceWith(fragment)
          markMonths(gallery)
        })
        onTiles(added)
        unload("bottom")
      } else {
        element.replaceWith(fragment)
        markMonths(gallery)
        onTiles(added)
        unload("top")
      }
      observeSentinels()
      return added.length > 0
    } catch {
      element.classList.remove("is-loading")
      return false
    } finally {
      loading.delete(url)
    }
  }

  const observer = new IntersectionObserver((records) => {
    for (const record of records) {
      if (record.isIntersecting) void load(record.target as HTMLElement)
    }
  }, { rootMargin: "1200px 0px" })

  const observeSentinels = (): void => {
    observer.disconnect()
    gallery.querySelectorAll<HTMLElement>(".album-page-sentinel").forEach((element) => observer.observe(element))
  }
  markMonths(gallery)
  observeSentinels()

  return {
    loadAfter: async () => {
      const element = gallery.querySelector<HTMLElement>("[data-page-after]")
      return element ? load(element) : false
    },
  }
}

// --- Selection -------------------------------------------------------------------------------

const setupSelection = (gallery: HTMLElement): { isSelecting: () => boolean; prepare: (tiles: HTMLElement[]) => void } => {
  const toggle = document.querySelector<HTMLButtonElement>("[data-album-select-toggle]")
  const form = document.querySelector<HTMLFormElement>("[data-album-selection]")
  const count = form?.querySelector<HTMLElement>("[data-album-selection-count]")
  const chosen = new Set<string>()
  let selecting = false

  const refresh = (): void => {
    if (count) count.textContent = `${chosen.size} valgt`
    form?.querySelectorAll<HTMLButtonElement>("button[type=submit]").forEach((button) => {
      button.disabled = chosen.size === 0
    })
  }

  const prepare = (tiles: HTMLElement[]): void => {
    for (const tile of tiles) {
      const label = tile.querySelector<HTMLElement>(".album-select")
      const box = tile.querySelector<HTMLInputElement>("[data-album-select]")
      if (label) label.hidden = !selecting
      if (box) box.checked = chosen.has(box.value)
      tile.classList.toggle("is-selected", Boolean(box?.checked))
    }
  }

  const setSelecting = (value: boolean): void => {
    selecting = value
    if (!value) chosen.clear()
    gallery.classList.toggle("is-selecting", value)
    if (form) form.hidden = !value
    toggle?.setAttribute("aria-pressed", String(value))
    prepare(Array.from(gallery.querySelectorAll<HTMLElement>(tileSelector)))
    refresh()
  }

  toggle?.addEventListener("click", () => setSelecting(!selecting))
  form?.querySelector("[data-album-selection-cancel]")?.addEventListener("click", () => setSelecting(false))

  gallery.addEventListener("change", (event) => {
    const box = event.target as HTMLInputElement
    if (!box.matches("[data-album-select]")) return
    if (box.checked) chosen.add(box.value)
    else chosen.delete(box.value)
    box.closest(tileSelector)?.classList.toggle("is-selected", box.checked)
    refresh()
  })

  form?.addEventListener("submit", (event) => {
    const submitter = (event as SubmitEvent).submitter as HTMLButtonElement | null
    if (submitter?.value === "delete" && !window.confirm(`Fjern ${chosen.size} medier?`)) {
      event.preventDefault()
      return
    }
    form.querySelectorAll("input[name=media]").forEach((input) => input.remove())
    for (const pk of chosen) {
      const input = document.createElement("input")
      input.type = "hidden"
      input.name = "media"
      input.value = pk
      form.append(input)
    }
  })

  return { isSelecting: () => selecting, prepare }
}

// --- Where in time the reader is, on the timeline ----------------------------------------------

/** Scroll the year strip sideways so `link` is in view — and nothing else. NOT scrollIntoView: that
 * scrolls every ancestor, the page included, and for an element inside the sticky year bar Chromium
 * aims at where the bar sits when it is not stuck. Called on every scroll frame, it dragged the page
 * straight back there, so the timeline could not be scrolled at all. */
const revealInStrip = (strip: HTMLElement, link: HTMLElement): void => {
  const bounds = strip.getBoundingClientRect()
  const box = link.getBoundingClientRect()
  if (box.left < bounds.left) strip.scrollLeft -= bounds.left - box.left
  else if (box.right > bounds.right) strip.scrollLeft += box.right - bounds.right
}

/** A rail down the right edge with every year on it, newest at the top, and a dot for where the
 * reader is: each year gets an equal stretch, split into its twelve months. While scrolling, the dot
 * names the month. The year links are the year bar's own, so the rail is hidden from screen readers
 * and the keyboard, which have the bar. */
const timeRail = (yearLinks: HTMLAnchorElement[]): { rail: HTMLElement; thumb: HTMLElement; label: HTMLElement } => {
  const rail = document.createElement("div")
  rail.className = "album-time-rail"
  rail.setAttribute("aria-hidden", "true")
  yearLinks.forEach((link, index) => {
    const tick = document.createElement("a")
    tick.href = link.href
    tick.tabIndex = -1
    tick.textContent = link.textContent
    tick.style.top = `${(index / yearLinks.length) * 100}%`
    rail.append(tick)
  })
  const thumb = document.createElement("span")
  thumb.className = "album-time-rail-thumb"
  const label = document.createElement("span")
  thumb.append(label)
  rail.append(thumb)
  document.body.append(rail)
  return { rail, thumb, label }
}

const setupTimePosition = (gallery: HTMLElement): void => {
  const years = document.querySelector<HTMLElement>("[data-album-years]")
  if (!years) return
  const yearLinks = Array.from(years.querySelectorAll<HTMLAnchorElement>("a[data-year-link]"))
  const order = new Map(yearLinks.map((link, index) => [link.dataset.yearLink ?? "", index]))
  const { rail, thumb, label } = timeRail(yearLinks)
  let scheduled = false
  let shownYear = ""
  let shownMonth = ""
  let quiet: number | undefined
  const update = (): void => {
    scheduled = false
    // The first row clear of the sticky year bar, not one scrolled up behind it. Should the bar not
    // be stuck, it has scrolled away, and the rows are measured from the top of the window instead.
    const below = Math.max(years.getBoundingClientRect().bottom, 0) + 20
    const visible = Array.from(gallery.querySelectorAll<HTMLElement>(tileSelector)).find(
      (tile) => tile.getBoundingClientRect().bottom > below,
    )
    const year = visible?.dataset.year
    if (year && year !== shownYear) {
      shownYear = year
      years.querySelectorAll<HTMLAnchorElement>("a").forEach((link) => {
        if (link.dataset.yearLink === year) link.setAttribute("aria-current", "true")
        else link.removeAttribute("aria-current")
      })
      const link = years.querySelector<HTMLElement>(`[data-year-link="${year}"]`)
      if (link) revealInStrip(years, link)
    }
    const month = visible?.dataset.month
    const index = order.get(year ?? "")
    if (!month || month === shownMonth || index === undefined) return
    shownMonth = month
    const within = (12 - Number(month.slice(5)) + 0.5) / 12
    thumb.style.top = `${((index + within) / yearLinks.length) * 100}%`
    label.textContent = visible?.dataset.monthLabel ?? ""
    rail.classList.add("is-placed")
  }
  update()
  window.addEventListener("scroll", () => {
    rail.classList.add("is-scrolling")
    window.clearTimeout(quiet)
    quiet = window.setTimeout(() => rail.classList.remove("is-scrolling"), 1200)
    if (scheduled) return
    scheduled = true
    window.requestAnimationFrame(update)
  }, { passive: true })
}

// --- Tags ------------------------------------------------------------------------------------
//
// One field for every place a tag is chosen: tagging an album or an item, and filtering the
// timeline. It searches the tags already in use as the reader types (views.tag_search), and opens
// on focus listing them all, so picking the existing spelling is easier than inventing a new one.

interface TagEntry {
  name: string
  url: string
  inherited: boolean
  from: string
}

// Mirrors photo_album.models.validate_tag_name, to answer before the round trip. The server decides.
const TAG_PATTERN = /^[\p{L}\p{N}_.-]+$/u
const TAG_MAX_LENGTH = 30

const tagProblem = (name: string): string => {
  if (name.length > TAG_MAX_LENGTH) return `Et tag må højst være ${TAG_MAX_LENGTH} tegn.`
  if (!TAG_PATTERN.test(name)) return "Et tag må kun indeholde bogstaver, tal, punktum, bindestreg og understregning."
  return ""
}

const setupTagInput = (
  input: HTMLInputElement,
  options: HTMLUListElement,
  searchUrl: string,
  taken: () => string[],
  choose: (name: string) => void,
): void => {
  let request = 0
  let timer: number | undefined
  let active = -1

  const items = (): HTMLLIElement[] => Array.from(options.querySelectorAll<HTMLLIElement>("li"))

  const close = (): void => {
    request++
    options.hidden = true
    options.replaceChildren()
    active = -1
    input.setAttribute("aria-expanded", "false")
    input.removeAttribute("aria-activedescendant")
  }

  const move = (delta: 1 | -1): void => {
    const all = items()
    if (!all.length) return
    active = active === -1 ? (delta === 1 ? 0 : all.length - 1) : (active + delta + all.length) % all.length
    all.forEach((item, index) => item.setAttribute("aria-selected", String(index === active)))
    input.setAttribute("aria-activedescendant", all[active].id)
    all[active].scrollIntoView({ block: "nearest" })
  }

  const search = async (): Promise<void> => {
    const mine = ++request
    const url = new URL(searchUrl, window.location.href)
    url.searchParams.set("q", input.value.trim())
    try {
      const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      if (!response.ok) return
      const { tags } = (await response.json()) as { tags: string[] }
      // Typed on, or left the field, while this was in flight.
      if (mine !== request || document.activeElement !== input) return
      const skip = new Set(taken().map((name) => name.toLowerCase()))
      const names = tags.filter((name) => !skip.has(name.toLowerCase()))
      options.replaceChildren(...names.map((name, index) => {
        const item = document.createElement("li")
        item.id = `${options.id}-${index}`
        item.setAttribute("role", "option")
        item.setAttribute("aria-selected", "false")
        item.textContent = name
        return item
      }))
      active = -1
      options.hidden = names.length === 0
      input.setAttribute("aria-expanded", String(names.length > 0))
      input.removeAttribute("aria-activedescendant")
    } catch {
      // No suggestions; typing a tag still works.
    }
  }

  const pick = (name: string): void => {
    close()
    if (name) choose(name)
  }

  input.addEventListener("focus", () => void search())
  input.addEventListener("input", () => {
    window.clearTimeout(timer)
    timer = window.setTimeout(() => void search(), 150)
  })
  input.addEventListener("blur", close)
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault()
      if (options.hidden) void search()
      else move(event.key === "ArrowDown" ? 1 : -1)
    } else if (event.key === "Escape" && !options.hidden) {
      // Closes the dropdown only, not the viewer around it.
      event.preventDefault()
      close()
    } else if (event.key === "Enter") {
      event.preventDefault()
      pick((items()[active]?.textContent ?? input.value).trim())
    }
  })
  // Keep the focus in the field, so the click lands before blur closes the list.
  options.addEventListener("mousedown", (event) => event.preventDefault())
  options.addEventListener("click", (event) => {
    const item = (event.target as Element).closest("li")
    if (item) pick(item.textContent ?? "")
  })
}

const renderTagList = (list: HTMLElement, tags: TagEntry[], editable: boolean): void => {
  list.replaceChildren(...tags.map((tag) => {
    const item = document.createElement("li")
    item.className = tag.inherited ? "album-tag is-inherited" : "album-tag"
    item.dataset.tagName = tag.name
    const link = document.createElement("a")
    link.href = tag.url
    link.textContent = tag.name
    if (tag.inherited) link.title = `Fra albummet ${tag.from}`
    item.append(link)
    if (editable && !tag.inherited) {
      const remove = document.createElement("button")
      remove.type = "button"
      remove.dataset.tagRemove = ""
      remove.setAttribute("aria-label", `Fjern tagget ${tag.name}`)
      remove.textContent = "×"
      item.append(remove)
    }
    return item
  }))
}

/**
 * A tag list (photo_album/_tags.html) whose tags can be added and removed when it has a URL to post
 * to. `onChange` hears about every change that went through, with the URL it was made at — the
 * viewer may have moved on to another item before the answer arrived.
 */
const setupTagEditor = (
  root: HTMLElement,
  onChange: (tags: TagEntry[], url: string) => void,
): { show: (tags: TagEntry[], url: string) => void } => {
  const list = root.querySelector<HTMLElement>("[data-tag-list]")!
  const add = root.querySelector<HTMLElement>("[data-tag-add]")
  const input = root.querySelector<HTMLInputElement>("[data-tag-input]")
  const options = root.querySelector<HTMLUListElement>("[data-tag-options]")
  const error = root.querySelector<HTMLElement>("[data-tag-error]")
  let url = root.dataset.tagUrl ?? ""

  const showError = (message: string): void => {
    if (!error) return
    error.textContent = message
    error.hidden = !message
  }

  const render = (tags: TagEntry[]): void => renderTagList(list, tags, Boolean(url))

  const change = async (action: "add" | "remove", name: string): Promise<void> => {
    const target = url
    const body = new FormData()
    body.set("action", action)
    body.set("name", name)
    try {
      const response = await fetch(target, {
        method: "POST",
        body,
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-CSRFToken": csrfToken() },
      })
      const result = (await response.json()) as { tags?: TagEntry[]; error?: string }
      if (target === url) showError(response.ok ? "" : (result.error ?? "Tagget kunne ikke gemmes."))
      if (!response.ok || !result.tags) return
      if (target === url) render(result.tags)
      onChange(result.tags, target)
    } catch {
      if (target === url) showError("Tagget kunne ikke gemmes. Kontrollér din forbindelse og prøv igen.")
    }
  }

  list.addEventListener("click", (event) => {
    const button = (event.target as Element).closest("[data-tag-remove]")
    const name = button?.closest<HTMLElement>("[data-tag-name]")?.dataset.tagName
    if (name && url) void change("remove", name)
  })

  if (input && options) {
    const taken = (): string[] =>
      Array.from(list.querySelectorAll<HTMLElement>("[data-tag-name]")).map((item) => item.dataset.tagName ?? "")
    setupTagInput(input, options, root.dataset.tagSearchUrl ?? "", taken, (name) => {
      const problem = tagProblem(name)
      showError(problem)
      if (problem) return
      input.value = ""
      void change("add", name)
    })
  }

  return {
    show: (tags, newUrl) => {
      if (newUrl !== url) {
        if (input) input.value = ""
        showError("")
      }
      url = newUrl
      if (add) add.hidden = !url
      render(tags)
      root.hidden = !url && tags.length === 0
    },
  }
}

// The album page's own tags. Its media inherit them, so the viewer's cached details go stale.
document.querySelectorAll<HTMLElement>("[data-tag-editor]").forEach((root) => {
  if (root.closest("[data-album-viewer]")) return
  setupTagEditor(root, () => document.dispatchEvent(new CustomEvent("photo-album:tags-changed")))
})

// The timeline's filter: choosing a tag adds it to the ones already filtered on.
const tagFilter = document.querySelector<HTMLFormElement>("[data-tag-filter]")
if (tagFilter) {
  const input = tagFilter.querySelector<HTMLInputElement>("[data-tag-input]")
  const options = tagFilter.querySelector<HTMLUListElement>("[data-tag-options]")
  if (input && options) {
    const taken = (): string[] =>
      Array.from(tagFilter.querySelectorAll<HTMLInputElement>("input[type=hidden][name=tag]")).map((field) => field.value)
    setupTagInput(input, options, tagFilter.dataset.tagSearchUrl ?? "", taken, (name) => {
      input.value = name
      tagFilter.submit()
    })
  }
}

// --- The viewer ------------------------------------------------------------------------------

interface MediaDetail {
  kind: "image" | "video"
  title: string
  filename: string
  full: string
  album: string
  albumUrl: string
  downloadUrl: string
  deleteUrl: string
  viewUrl: string
  mapUrl: string
  uploadedBy: string
  uploadedAt: string
  capturedAt: string
  // "ca. september 2004" and why, when the item has no date of its own to be placed by
  approximateDate: string
  approximateDateBasis: string
  metadata: Record<string, string>
  tags: TagEntry[]
  tagUrl: string
  uniqueViews: number
  totalViews: number
}

const gallery = document.querySelector<HTMLElement>("[data-album-gallery]")
const dialog = document.querySelector<HTMLDialogElement>("[data-album-viewer]")

if (gallery && dialog) {
  const image = dialog.querySelector<HTMLImageElement>("[data-gallery-image]")!
  const video = dialog.querySelector<HTMLVideoElement>("[data-gallery-video]")!
  const title = dialog.querySelector<HTMLElement>("[data-gallery-title]")!
  const metadata = dialog.querySelector<HTMLDListElement>("[data-gallery-metadata]")!
  const menuMetadata = dialog.querySelector<HTMLDListElement>("[data-gallery-menu-metadata]")!
  const views = dialog.querySelector<HTMLElement>("[data-gallery-views]")
  const menuViews = dialog.querySelector<HTMLElement>("[data-gallery-menu-views]")
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
  // The bin shows binned items through the same viewer; looking at those is not a view.
  const countsViews = !gallery.hasAttribute("data-album-no-views")
  let current: HTMLButtonElement | undefined
  let touchStart: { x: number; y: number } | undefined
  let zoomScale = 1
  let pinchStartDistance = 0
  let pinchStartScale = 1

  // The aside on a desktop, the kebab menu on a phone: both show the same tags.
  const tagEditors: { show: (tags: TagEntry[], url: string) => void }[] = []
  const tagsChanged = (tags: TagEntry[], url: string): void => {
    // Cheaper than working out which cached items share the tag: they are fetched again on demand.
    details.clear()
    tagEditors.forEach((editor) => editor.show(tags, url))
  }
  dialog.querySelectorAll<HTMLElement>("[data-tag-editor]").forEach((root) => {
    tagEditors.push(setupTagEditor(root, (tags, url) => {
      if (current && currentTagUrl === url) tagsChanged(tags, url)
      else details.clear()
    }))
  })
  let currentTagUrl = ""
  document.addEventListener("photo-album:tags-changed", () => details.clear())

  const selection = setupSelection(gallery)
  const paging = setupPaging(gallery, selection.prepare, () => !dialog.open)
  setupTimePosition(gallery)

  // Read fresh every time: the grid gains and loses pages as the reader scrolls.
  const entries = (): HTMLButtonElement[] =>
    Array.from(gallery.querySelectorAll<HTMLButtonElement>(".album-media-tile[data-detail-url]"))

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

  const definition = (term: string, value: string, href = ""): HTMLElement[] => {
    const dt = document.createElement("dt")
    dt.textContent = term
    const dd = document.createElement("dd")
    if (href) {
      const link = document.createElement("a")
      link.href = href
      link.textContent = value
      if (href.startsWith("http")) {
        link.target = "_blank"
        link.rel = "noopener"
      }
      dd.append(link)
    } else {
      dd.textContent = value
    }
    return [dt, dd]
  }

  const renderMetadata = (detail: MediaDetail): void => {
    const { Sted: location, ...rest } = detail.metadata
    const album = detail.album ? definition("Album", detail.album, detail.albumUrl) : []
    album[1]?.firstElementChild?.setAttribute("data-gallery-album", "")
    const nodes = [
      ...album,
      ...(detail.filename ? definition("Filnavn", detail.filename) : []),
      ...(detail.capturedAt ? definition("Optaget", detail.capturedAt) : []),
      ...(detail.approximateDate
        ? definition("Anslået dato", `${detail.approximateDate}, ${detail.approximateDateBasis}`)
        : []),
      ...(location ? definition("Sted", location, detail.mapUrl) : []),
      ...definition("Uploadet af", detail.uploadedBy),
      ...definition("Uploadet", detail.uploadedAt),
      ...Object.entries(rest).flatMap(([key, value]) => definition(key, String(value))),
    ]
    metadata.replaceChildren(...nodes)
    menuMetadata.replaceChildren(...nodes.map((node) => node.cloneNode(true)))
  }

  const renderViews = (unique: number, total: number): void => {
    const text = total
      ? `Set ${total} ${total === 1 ? "gang" : "gange"} af ${unique} ${unique === 1 ? "person" : "personer"}`
      : "Ikke set endnu"
    if (views) views.textContent = text
    if (menuViews) menuViews.textContent = text
  }

  // Reported once the full-size file has actually arrived, so a thumbnail flicked past is not a view.
  const reportView = (entry: HTMLButtonElement, detail: MediaDetail): void => {
    if (!countsViews || !detail.viewUrl || current !== entry) return
    void fetch(detail.viewUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "X-CSRFToken": csrfToken() },
    })
      .then((response) => (response.ok ? (response.json() as Promise<{ uniqueViews: number; totalViews: number }>) : null))
      .then((counts) => {
        if (!counts) return
        detail.uniqueViews = counts.uniqueViews
        detail.totalViews = counts.totalViews
        if (current === entry) renderViews(counts.uniqueViews, counts.totalViews)
      })
      .catch(() => undefined)
  }

  const mediaIdOf = (entry: HTMLElement): string => entry.closest<HTMLElement>(tileSelector)?.dataset.mediaId ?? ""

  const setAddress = (pk: string): void => {
    const url = new URL(window.location.href)
    if (pk) url.searchParams.set("media", pk)
    else url.searchParams.delete("media")
    window.history.replaceState(window.history.state, "", url)
  }

  const updateArrows = (): void => {
    const all = entries()
    const index = current ? all.indexOf(current) : -1
    previous.disabled = index <= 0
    next.disabled = index === -1 || (index === all.length - 1 && !gallery.querySelector("[data-page-after]"))
  }

  const show = (entry: HTMLButtonElement): void => {
    current = entry
    resetZoom()
    entry.scrollIntoView({ block: "nearest" })

    // The thumbnail is already decoded in the grid, so showing it stretched is instant and the
    // viewer never opens on a blank stage. The real image replaces it when the fetch lands.
    const thumbnail = entry.querySelector("img")
    image.hidden = false
    video.hidden = true
    video.removeAttribute("src")
    image.onload = null
    video.onloadeddata = null
    image.src = thumbnail?.src ?? ""
    image.alt = thumbnail?.alt ?? ""
    title.textContent = thumbnail?.alt ?? ""
    mobileDate.textContent = ""
    metadata.replaceChildren()
    menuMetadata.replaceChildren()
    if (views) views.textContent = ""
    if (menuViews) menuViews.textContent = ""
    deleteForm.hidden = true
    menuDeleteForm.hidden = true
    currentTagUrl = ""
    tagEditors.forEach((editor) => editor.show([], ""))
    menu.hidden = true
    menuButton.setAttribute("aria-expanded", "false")
    updateArrows()
    // The address bar always holds a link to what is on screen, ready to be shared.
    const pk = mediaIdOf(entry)
    if (pk) setAddress(pk)

    void detailFor(entry).then((detail) => {
      // Paged on while this was in flight: the answer is about a different photograph now.
      if (!detail || current !== entry) return
      const isVideo = detail.kind === "video"
      image.hidden = isVideo
      video.hidden = !isVideo
      if (isVideo) {
        image.removeAttribute("src")
        video.onloadeddata = () => reportView(entry, detail)
        video.src = detail.full
      } else if (detail.full) {
        image.onload = () => {
          if (image.src.endsWith(detail.full)) reportView(entry, detail)
        }
        image.src = detail.full
      }
      image.alt = detail.title
      title.textContent = detail.title
      mobileDate.textContent = detail.capturedAt || detail.approximateDate || detail.uploadedAt
      download.href = detail.downloadUrl
      menuDownload.href = detail.downloadUrl
      deleteForm.hidden = !detail.deleteUrl
      deleteForm.action = detail.deleteUrl
      menuDeleteForm.hidden = !detail.deleteUrl
      menuDeleteForm.action = detail.deleteUrl
      renderMetadata(detail)
      currentTagUrl = detail.tagUrl
      tagEditors.forEach((editor) => editor.show(detail.tags, detail.tagUrl))
      renderViews(detail.uniqueViews, detail.totalViews)
      // Warm the neighbours, so paging with the arrows or a swipe does not wait on the network.
      const all = entries()
      const index = all.indexOf(entry)
      for (const neighbour of [all[index - 1], all[index + 1]]) {
        if (neighbour) void detailFor(neighbour)
      }
    })
  }

  const step = async (direction: -1 | 1): Promise<void> => {
    if (!current) return
    let all = entries()
    let index = all.indexOf(current) + direction
    if (index >= all.length && direction === 1) {
      // At the last loaded tile: fetch the next page and carry on into it.
      const from = current
      if (!(await paging.loadAfter()) || current !== from) return
      all = entries()
      index = all.indexOf(from) + 1
    }
    if (all[index]) show(all[index])
  }

  const open = (entry: HTMLButtonElement): void => {
    if (!dialog.open) dialog.showModal()
    show(entry)
  }

  gallery.addEventListener("click", (event) => {
    const entry = (event.target as Element).closest<HTMLButtonElement>(".album-media-tile[data-detail-url]")
    if (!entry) return
    if (selection.isSelecting()) {
      // In selection mode a tap on the picture ticks it, as in every phone gallery.
      const box = entry.closest(tileSelector)?.querySelector<HTMLInputElement>("[data-album-select]")
      if (box) {
        box.checked = !box.checked
        box.dispatchEvent(new Event("change", { bubbles: true }))
      }
      return
    }
    open(entry)
  })
  dialog.querySelector("[data-gallery-close]")?.addEventListener("click", () => dialog.close())
  dialog.addEventListener("close", () => {
    current = undefined
    video.pause()
    setAddress("")
  })
  menuButton.addEventListener("click", () => {
    menu.hidden = !menu.hidden
    menuButton.setAttribute("aria-expanded", String(!menu.hidden))
  })
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close()
    // The album name leaves the viewer for the album; on that album already, closing is the trip.
    const album = (event.target as Element).closest<HTMLAnchorElement>("a[data-gallery-album]")
    if (!album) return
    if (album.pathname === window.location.pathname) event.preventDefault()
    dialog.close()
  })
  previous.addEventListener("click", () => void step(-1))
  next.addEventListener("click", () => void step(1))
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
    void step(horizontalDistance < 0 ? 1 : -1)
  }, { passive: true })
  stage.addEventListener("touchend", () => {
    if (pinchStartDistance > 0) pinchStartDistance = 0
  }, { passive: true })
  document.addEventListener("keydown", (event) => {
    if (!dialog.open) return
    // Typing a tag: the arrows move the cursor, not to the next photograph.
    if ((event.target as Element | null)?.closest?.("input, textarea")) return
    if (event.key === "ArrowLeft") void step(-1)
    if (event.key === "ArrowRight") void step(1)
  })

  // A shared link (`?media=<pk>`): the page was rendered around that item, so open it.
  const linked = new URL(window.location.href).searchParams.get("media")
  if (linked) {
    const entry = gallery.querySelector<HTMLButtonElement>(
      `${tileSelector}[data-media-id="${CSS.escape(linked)}"] .album-media-tile[data-detail-url]`,
    )
    if (entry) {
      entry.scrollIntoView({ block: "center" })
      open(entry)
    }
  }
}
