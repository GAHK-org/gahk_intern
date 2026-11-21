// The photo album's approval page (photo_album/approval.html): what is ticked is rejected, the rest
// approved. The tick is the tile's own checkbox, so the form works without this; this keeps the
// count up to date, ticks or clears them all, and for a new album shows the choice made — its name,
// or the album of the chosen folder it goes into, fetched when the folder changes.

const form = document.querySelector<HTMLFormElement>("[data-approval-form]")

if (form) {
  const boxes = (): HTMLInputElement[] => Array.from(form.querySelectorAll<HTMLInputElement>("[data-approval-reject]"))
  const summary = form.querySelector<HTMLElement>("[data-approval-summary]")

  const refresh = (): void => {
    const all = boxes()
    const rejected = all.filter((box) => box.checked).length
    all.forEach((box) => box.closest(".approval-item")?.classList.toggle("is-rejected", box.checked))
    if (!summary) return
    if (!all.length) summary.textContent = ""
    else if (!rejected) summary.textContent = all.length === 1 ? "Godkendes" : `Alle ${all.length} godkendes`
    else summary.textContent = `${all.length - rejected} godkendes, ${rejected} afvises`
  }

  form.addEventListener("change", (event) => {
    if ((event.target as Element).matches("[data-approval-reject]")) refresh()
  })
  form.querySelectorAll<HTMLButtonElement>("[data-approval-pick]").forEach((button) => {
    button.addEventListener("click", () => {
      const on = button.dataset.approvalPick === "all"
      boxes().forEach((box) => (box.checked = on))
      refresh()
    })
  })
  refresh()

  // One block per new album: keep it under a name, or merge it into an album of the folder chosen.
  form.querySelectorAll<HTMLElement>("[data-approval-album]").forEach((album) => {
    const actions = Array.from(album.querySelectorAll<HTMLInputElement>("[data-approval-album-action]"))
    const options = Array.from(album.querySelectorAll<HTMLElement>("[data-approval-album-option]"))
    const target = album.querySelector<HTMLSelectElement>("[data-approval-merge-target]")!
    const folderInput = album.querySelector<HTMLInputElement>("[data-album-folder-input]")!
    const folderChoice = album.querySelector<HTMLSelectElement>("[data-album-folder-choice]")!

    // Only the chosen option's fields are sent and checked: a disabled field is neither.
    const showChoice = (): void => {
      const chosen = actions.find((action) => action.checked)?.value ?? "keep"
      for (const option of options) {
        const on = option.dataset.approvalAlbumOption === chosen
        option.hidden = !on
        option.querySelectorAll<HTMLInputElement | HTMLSelectElement>("input, select").forEach((field) => {
          field.disabled = !on
        })
      }
      target.required = chosen === "merge"
    }
    actions.forEach((action) => action.addEventListener("change", showChoice))
    showChoice()

    let asked = folderInput.value.trim() // the server rendered that folder's albums
    const loadAlbums = async (): Promise<void> => {
      const folder = folderInput.value.trim()
      if (!folder || folder === asked) return
      asked = folder
      const url = new URL(album.dataset.albumsUrl ?? "", window.location.href)
      url.searchParams.set("folder", folder)
      url.searchParams.set("album", album.dataset.albumId ?? "")
      const response = await fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      if (!response.ok || asked !== folder) return
      const { albums } = (await response.json()) as { albums: { id: number; label: string; locked: boolean }[] }
      const previous = target.value
      const placeholder = new Option(albums.length ? "Vælg album" : "Mappen har ingen albummer", "")
      target.replaceChildren(
        placeholder,
        ...albums.map((each) => {
          const option = new Option(each.locked ? `${each.label} (låst)` : each.label, String(each.id))
          option.disabled = each.locked
          return option
        }),
      )
      target.value = albums.some((each) => String(each.id) === previous && !each.locked) ? previous : ""
    }
    let typing: number | undefined
    // After the folder selector's own handler (photo-album.ts) has put the choice into the input.
    folderChoice.addEventListener("change", () => window.setTimeout(() => void loadAlbums()))
    folderInput.addEventListener("input", () => {
      window.clearTimeout(typing)
      typing = window.setTimeout(() => void loadAlbums(), 400)
    })
  })
}
