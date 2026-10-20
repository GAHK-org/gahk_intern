// Large files straight into the bucket, past the proxy that cuts any upload taking more than a
// minute (photo_album/direct_upload.py), with Uppy (@uppy/aws-s3) doing the uploading: a multipart
// upload in parts of the server's size, each request retried, stalls and going offline handled. The
// server hands out a ticket for the key it chose, and signs each of Uppy's requests for it in turn
// (direct_upload_sign); the ticket is then submitted where the file itself would have been posted.
//
// The bucket must let the site POST, PUT and DELETE and read the ETag header (its CORS rule,
// DEPLOY.md 4c). Where it does not, the very first request fails before anything is sent, and the
// caller posts the file the old way.

import AwsS3 from "@uppy/aws-s3"
import Uppy from "@uppy/core"
import { csrfToken } from "./htmx-csrf"

export interface DirectUpload {
  ticket: string
  partSize: number
}

/** A refusal the server explained, to be shown as it is. */
export class UploadRefused extends Error {
  name = "UploadRefused"
}

/** The bucket could not be reached at all — no CORS rule, most likely. Post the file instead. */
export class DirectUnavailable extends Error {}

export function refusal(body: unknown): string {
  const errors = (body as { errors?: Record<string, { message: string }[]> } | null)?.errors ?? {}
  return Object.values(errors).flat().map((error) => error.message).join(" ")
}

const postForm = async (url: string, fields: Record<string, string>): Promise<{ ok: boolean; body: unknown }> => {
  const body = new FormData()
  for (const [key, value] of Object.entries(fields)) body.append(key, value)
  const response = await fetch(url, {
    method: "POST",
    body,
    credentials: "same-origin",
    headers: { Accept: "application/json", "X-CSRFToken": csrfToken() },
  })
  return { ok: response.ok, body: await response.json().catch(() => null) }
}

/** Ask the server for a direct upload of `fields`; null when it takes the file with the request. */
export async function beginDirect(url: string, fields: Record<string, string>): Promise<DirectUpload | null> {
  const { ok, body } = await postForm(url, fields)
  if (!ok) {
    const reason = refusal(body)
    if (reason) throw new UploadRefused(reason)
    return null // a server older than direct uploads, say
  }
  const data = body as { direct: boolean } & DirectUpload
  return data.direct ? data : null
}

interface Signed {
  url: string
  key?: string
  headers?: Record<string, string>
}

/** Send `file` to the bucket with Uppy, for the upload the server gave `upload` for. `onProgress`
 * is told how many bytes of it are in. Resolves once the bucket has all of it; an upload that fails
 * is aborted in the bucket again, so no half of it is left there. */
export async function sendToBucket(
  file: File,
  upload: DirectUpload,
  signUrl: string,
  onProgress: (sent: number) => void,
): Promise<void> {
  const sign = async (request: { method: string; key: string; uploadId?: string; partNumber?: number }): Promise<Signed> => {
    const { ok, body } = await postForm(signUrl, {
      ticket: upload.ticket,
      method: request.method,
      key: request.key,
      upload_id: request.uploadId ?? "",
      part_number: request.partNumber === undefined ? "" : String(request.partNumber),
    })
    if (!ok) throw new UploadRefused(refusal(body) || "Serveren ville ikke tage imod filen.")
    return body as Signed
  }
  const uppy = new Uppy()
  uppy.use(AwsS3, {
    shouldUseMultipart: true,
    getChunkSize: () => upload.partSize,
    limit: 1,
    signRequest: sign,
  })
  let sent = 0
  let failure: { name: string; message: string } | null = null
  uppy.on("upload-progress", (_file, progress) => {
    sent = progress.bytesUploaded ?? 0
    onProgress(sent)
  })
  uppy.on("upload-error", (_file, error) => {
    failure = error
  })
  const id = uppy.addFile({ name: file.name, type: file.type || "application/octet-stream", data: file })
  try {
    const result = await uppy.upload()
    if (result?.successful?.length) {
      onProgress(file.size)
      return
    }
    // Abort what Uppy keeps for a resume that will not come: the parts in the bucket.
    const started = uppy.getFile(id)?.s3Multipart
    if (started) {
      void sign({ method: "DELETE", key: started.key, uploadId: started.uploadId })
        .then((signed) => fetch(signed.url, { method: "DELETE" }))
        .catch(() => undefined) // the bucket's lifecycle rule aborts it within a week anyway
    }
    const reason = failure as { name: string; message: string } | null
    if (reason?.name === "UploadRefused") throw new UploadRefused(reason.message)
    if (reason?.name === "S3NetworkError" && sent === 0) throw new DirectUnavailable(reason.message)
    throw new Error(reason?.message ?? "upload failed")
  } finally {
    uppy.destroy()
  }
}
