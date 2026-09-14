// Downscale/recompress image uploads in the browser before the form submits, so room-inspection
// photos (F-005) stay small — big phone photos become ~a few hundred KB instead of multiple MB.
// No-op on pages without an image file input. Server keeps a hard size cap as a backstop.
const MAX_DIM = 1600; // longest edge, px
const QUALITY = 0.82; // JPEG quality
// Arkiv grid previews. 320px covers a 2x display at the ~160px the listing renders them at, and
// a lower quality is invisible at that size while roughly halving the bytes again.
const THUMB_DIM = 320;
const THUMB_QUALITY = 0.7;
// Arkiv's viewer size. Deliberately the same 1600/0.82 as MAX_DIM/QUALITY above - a photograph
// large enough to fill any screen in the building and small enough that paging through a folder of
// them is not somebody's mobile data. Mirrors PREVIEW_DIM/PREVIEW_QUALITY in
// arkiv/management/commands/make_arkiv_thumbnails.py, which makes the same size server-side for
// the imported backlog: if one changes, change both, or a folder shows previews at two sizes
// depending on how its files happened to arrive.
const PREVIEW_DIM = 1600;
const PREVIEW_QUALITY = 0.82;

/** Downscale one image to MAX_DIM/QUALITY, returning the original if there is nothing to gain.
 *
 * Exported because opslagstavlen uploads via fetch rather than a form submit, so it cannot use
 * hookImageForms below — and a second canvas downscaler with its own parameters is exactly the
 * drift worth avoiding. */
async function redraw(file: File, maxDim: number, quality: number): Promise<Blob | null> {
  // The shared canvas step. Extracted when Arkiv wanted a second size: one downscaler with a
  // parameter, rather than two with their own quirks, is exactly the drift the note above warns
  // about. Returns null when the image cannot be decoded or the canvas is unavailable.
  let bmp: ImageBitmap;
  try {
    bmp = await createImageBitmap(file);
  } catch {
    return null; // unsupported/undecodable → let the caller decide
  }
  const scale = Math.min(1, maxDim / Math.max(bmp.width, bmp.height));
  const w = Math.round(bmp.width * scale);
  const h = Math.round(bmp.height * scale);
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.drawImage(bmp, 0, 0, w, h);
  return new Promise<Blob | null>((res) => canvas.toBlob(res, "image/jpeg", quality));
}

export async function downscaleImage(file: File): Promise<File> {
  if (!file.type.startsWith("image/")) return file;
  const bmp = await createImageBitmap(file).catch(() => null);
  if (!bmp) return file;
  const scale = Math.min(1, MAX_DIM / Math.max(bmp.width, bmp.height));
  if (scale === 1 && file.size < 800_000) return file; // already small enough
  const blob = await redraw(file, MAX_DIM, QUALITY);
  if (!blob || blob.size >= file.size) return file; // no gain → keep original
  return new File([blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" });
}

/** A grid-sized preview, or null when the file is not a decodable image.
 *
 * ALWAYS returns the small version when it can, unlike downscaleImage: a thumbnail that falls back
 * to the original would defeat its own purpose, which is that opening a folder of 200 party
 * photographs costs a few hundred KB rather than a gigabyte of someone's mobile data. */
export async function thumbnailImage(file: File): Promise<Blob | null> {
  if (!file.type.startsWith("image/")) return null;
  return redraw(file, THUMB_DIM, THUMB_QUALITY);
}

/** The viewer-sized version, or null when the file is not a decodable image.
 *
 * Made here rather than on the server for the same reason the thumbnail is: the alternative is
 * Pillow in the production image and a scheduled sweep to catch up with uploads, and this project
 * has deliberately no worker and no Celery. It also means the preview exists the moment the photo
 * does - a sweep would leave a window in which the newest photograph is the one that has no
 * preview, and the person who just uploaded it is the one about to go and look at it.
 *
 * Unlike downscaleImage this never falls back to the original: a preview that is the original is
 * not a preview. The caller treats null as "no preview", which the server then confirms by asking
 * the store rather than believing the browser. */
export async function previewImage(file: File): Promise<Blob | null> {
  if (!file.type.startsWith("image/")) return null;
  return redraw(file, PREVIEW_DIM, PREVIEW_QUALITY);
}

// ONE delegated listener on the document, in the CAPTURE phase. Both halves of that matter.
//
// Capture, because htmx also listens for `submit` — on the form itself — as soon as a form carries
// hx-post, which Den Hurtige's reply form now does. Two at-target listeners fire in registration
// order, and htmx processes the node when it enters the DOM while this module runs at import: htmx
// would win, and send the full-size phone photo before the downscale ever ran. A capture listener
// on an ancestor always precedes at-target listeners, whoever registered first.
//
// Delegated, because the forms are rebuilt constantly — the feed morphs every five seconds and the
// thread panel swaps in and out. Per-form hooking needed a re-arm after every swap plus a
// data-img-hooked marker to stop listeners stacking up, and the marker then had to be protected
// from the morph. A document-level listener sees forms that did not exist when it was registered,
// so all of that machinery is gone.
document.addEventListener(
  "submit",
  async (event: SubmitEvent) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.imgReady === "1") {
      // Second pass, after the downscale. Clear the flag rather than leaving it set: a form that
      // survives its own submit — the thread panel's, which htmx posts and then resets — would
      // otherwise skip downscaling for every photo after the first.
      delete form.dataset.imgReady;
      return;
    }
    const inputs = Array.from(
      form.querySelectorAll<HTMLInputElement>('input[type="file"][accept^="image"]'),
    );
    if (!inputs.some((i) => i.files && i.files.length)) return; // nothing picked
    event.preventDefault();
    event.stopPropagation(); // keep htmx from posting this pass
    for (const input of inputs) {
      if (!input.files || !input.files.length) continue;
      const dt = new DataTransfer();
      for (const f of Array.from(input.files)) dt.items.add(await downscaleImage(f));
      input.files = dt.files;
    }
    form.dataset.imgReady = "1";
    form.requestSubmit();
  },
  true,
);

/** Kept as a no-op for callers that used to re-arm forms after a swap.
 *
 * The delegated listener above needs no rescan, so there is nothing to do — but the export stays
 * so an out-of-tree caller does not break, and so this note is findable from the call site. */
export function hookImageForms(_root: ParentNode = document): void {
  /* nothing to do: submit is handled by the delegated capture listener above */
}
