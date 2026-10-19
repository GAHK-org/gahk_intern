// Large files straight into the bucket, in parts, past the proxy that cuts any upload taking more
// than a minute (photo_album/direct_upload.py). The server hands out a ticket and one presigned
// URL per part; each part is PUT on its own and tried again on its own; the ticket and the parts'
// ETags are then submitted where the file itself would have been posted.
//
// The bucket must let the site PUT and read the ETag header (its CORS rule, DEPLOY.md 4c). Where it
// does not, the first part fails before anything is sent, and the caller posts the file the old way.

import { csrfToken } from "./htmx-csrf"

export interface DirectUpload {
  ticket: string
  partSize: number
  urls: string[]
}

/** A refusal the server explained, to be shown as it is. */
export class UploadRefused extends Error {}

/** The bucket could not be reached at all — no CORS rule, most likely. Post the file instead. */
export class DirectUnavailable extends Error {}

const RETRIES = 4

export function refusal(body: unknown): string {
  const errors = (body as { errors?: Record<string, { message: string }[]> } | null)?.errors ?? {}
  return Object.values(errors).flat().map((error) => error.message).join(" ")
}

/** Ask the server for a direct upload of `fields`; null when it takes the file with the request. */
export async function beginDirect(url: string, fields: Record<string, string>): Promise<DirectUpload | null> {
  const body = new FormData()
  for (const [key, value] of Object.entries(fields)) body.append(key, value)
  const response = await fetch(url, {
    method: "POST",
    body,
    credentials: "same-origin",
    headers: { Accept: "application/json", "X-CSRFToken": csrfToken() },
  })
  if (response.status === 400) throw new UploadRefused(refusal(await response.json().catch(() => null)))
  if (!response.ok) return null
  const data = (await response.json()) as { direct: boolean } & DirectUpload
  return data.direct ? data : null
}

const putPart = (url: string, part: Blob, onProgress: (loaded: number) => void): Promise<string> =>
  new Promise((resolve, reject) => {
    const request = new XMLHttpRequest()
    request.open("PUT", url)
    request.upload.addEventListener("progress", (event) => onProgress(event.loaded))
    request.addEventListener("load", () => {
      const etag = request.getResponseHeader("ETag")
      if (request.status >= 200 && request.status < 300 && etag) resolve(etag)
      else reject(Object.assign(new Error(`HTTP ${request.status}`), { status: request.status }))
    })
    request.addEventListener("error", () => reject(Object.assign(new Error("no answer"), { status: 0 })))
    request.send(part)
  })

const wait = (milliseconds: number): Promise<void> => new Promise((resolve) => window.setTimeout(resolve, milliseconds))

/** PUT `file` to the upload's part URLs, a part at a time; returns their ETags in order.
 * `onProgress` is told how many bytes of the whole file are in. */
export async function sendParts(file: Blob, upload: DirectUpload, onProgress: (sent: number) => void): Promise<string[]> {
  const etags: string[] = []
  for (const [index, url] of upload.urls.entries()) {
    const start = index * upload.partSize
    const part = file.slice(start, Math.min(start + upload.partSize, file.size))
    for (let attempt = 1; ; attempt += 1) {
      try {
        etags.push(await putPart(url, part, (loaded) => onProgress(start + loaded)))
        break
      } catch (error) {
        const status = (error as { status?: number }).status ?? 0
        // Nothing went through yet and the bucket did not answer: it does not take uploads from here.
        if (index === 0 && status === 0 && attempt === 2) throw new DirectUnavailable(String(error))
        if ((status !== 0 && status < 500) || attempt === RETRIES) throw error
        await wait(2 ** attempt * 1000)
      }
    }
  }
  onProgress(file.size)
  return etags
}
