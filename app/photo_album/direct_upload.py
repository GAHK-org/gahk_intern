"""Large files go straight into the bucket, not through the app server.

A body posted to the app passes Coolify's Traefik on the way, and Traefik cuts any request whose
body takes longer than a minute to arrive: the Dropbox album "Bornholm med GAHK - April 06", 3.2 GB,
was dropped six times in a row while every album under 540 MB went through. A resident's video on
dorm wifi meets the same minute. Even with the proxy out of the way, one uvicorn process would
receive the gigabytes, spool them to disk twice and copy them to the bucket before answering.
Arkiv went direct for the same reason (arkiv/uploads.py).

So the client — the import script, or the browser for a ZIP import or a large photo or video —
asks for an upload, sends the file to the bucket as a multipart upload (parts of PART_SIZE, each to
a presigned URL and each tried again on its own), and then submits it with the ticket it was given.
Only then is the upload completed and checked, and handled as if the file had arrived with the
request: a ZIP is queued for import, a photo or video is checked and stored like any upload.

**The ticket is signed**, so the key and the upload it names are the server's own: the client picks
neither. It also names what the upload is for, so an import's ticket cannot be spent on an album
upload or the other way round. The size announced is checked against what the bucket holds once
completed.

A browser needs the bucket's CORS rule to allow PUT from the site (DEPLOY.md 4c). An upload
abandoned halfway is aborted by the bucket's lifecycle rule (`abort-incomplete-uploads`). Without a
bucket — dev, CI — there is no direct upload, and the file is sent with the request as before.
"""

import math
from pathlib import Path
from typing import IO, Any
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from django.core import signing
from django.core.exceptions import ValidationError

from .storage import PhotoAlbumS3Storage, get_photo_album_storage

# Small enough that a part goes in well under a minute on a slow line, and that a failure costs
# only that part; large enough that a 5 GB file is under a hundred of them.
PART_SIZE = 64 * 1024 * 1024
MAX_PARTS = 10_000  # S3's own limit
# How long the part URLs and the ticket stay good: a whole album sent over a slow line.
TTL = 24 * 60 * 60
_SALT = "photo_album.direct_upload"

IMPORT = "import"
MEDIA = "media"


def _storage() -> PhotoAlbumS3Storage | None:
    storage = get_photo_album_storage()
    return storage if isinstance(storage, PhotoAlbumS3Storage) else None


def available() -> bool:
    return _storage() is not None


def begin(
    key: str, size: int, *, purpose: str, content_type: str = "application/octet-stream", **extra: str | int
) -> dict[str, Any] | None:
    """Start a multipart upload of `size` bytes to `key`; None without a bucket.

    Returns the ticket to submit it with, the part size, and one presigned URL per part. `extra` is
    signed into the ticket with the key, for what the upload is submitted to.
    """
    storage = _storage()
    if storage is None:
        return None
    part_size = max(PART_SIZE, math.ceil(size / MAX_PARTS))
    bucket, object_key = storage.bucket_name, storage._normalize_name(key)
    upload_id = storage.connection.meta.client.create_multipart_upload(
        Bucket=bucket, Key=object_key, ContentType=content_type
    )["UploadId"]
    presign = storage.public_connection.meta.client.generate_presigned_url
    urls = [
        presign(
            "upload_part",
            Params={"Bucket": bucket, "Key": object_key, "UploadId": upload_id, "PartNumber": number},
            ExpiresIn=TTL,
        )
        for number in range(1, max(1, math.ceil(size / part_size)) + 1)
    ]
    ticket = signing.dumps(
        {**extra, "key": key, "upload": upload_id, "size": size, "purpose": purpose}, salt=_SALT
    )
    return {"ticket": ticket, "partSize": part_size, "urls": urls}


def begin_import(name: str, size: int) -> dict[str, Any] | None:
    """An upload for a ZIP to import, where imports keep their archive until they are done."""
    return begin(
        f"photo-album-imports/{uuid4()}/{Path(name).name}",
        size,
        purpose=IMPORT,
        content_type="application/zip",
        name=name,
    )


def finish(ticket: str, etags: list[str], *, purpose: str) -> dict[str, Any]:
    """Complete the upload a ticket names, from its parts' ETags in order; returns what was signed
    into the ticket (`key`, `size` and the extras `begin` was given).

    Raises ValidationError for a ticket this server did not sign for `purpose` (or one past TTL),
    for parts the bucket does not take, and for an object of another size than was announced.
    """
    try:
        upload = signing.loads(ticket, salt=_SALT, max_age=TTL)
    except signing.BadSignature as exc:
        raise ValidationError("Uploaden er ukendt eller for gammel. Send filen igen.") from exc
    if upload.get("purpose") != purpose:
        raise ValidationError("Uploaden er ukendt eller for gammel. Send filen igen.")
    storage = _storage()
    if storage is None:
        raise ValidationError("Serveren tager ikke imod uploads direkte til lageret.")
    client = storage.connection.meta.client
    bucket, object_key = storage.bucket_name, storage._normalize_name(upload["key"])
    try:
        client.complete_multipart_upload(
            Bucket=bucket,
            Key=object_key,
            UploadId=upload["upload"],
            MultipartUpload={"Parts": [{"ETag": etag, "PartNumber": n} for n, etag in enumerate(etags, 1)]},
        )
        stored = client.head_object(Bucket=bucket, Key=object_key)["ContentLength"]
    except (BotoCoreError, ClientError) as exc:
        raise ValidationError(f"Uploaden kunne ikke afsluttes ({exc}). Send filen igen.") from exc
    if stored != upload["size"]:
        delete(upload["key"])
        raise ValidationError(
            f"Lageret fik {stored} bytes, men filen var på {upload['size']}. Send filen igen."
        )
    return dict(upload)


def read_into(key: str, target: IO[bytes]) -> None:
    """Copy an uploaded object into `target`, a local file, to be checked like any upload."""
    storage = _storage()
    if storage is None:
        raise ValidationError("Serveren tager ikke imod uploads direkte til lageret.")
    body = storage.connection.meta.client.get_object(
        Bucket=storage.bucket_name, Key=storage._normalize_name(key)
    )["Body"]
    while chunk := body.read(1024 * 1024):
        target.write(chunk)
    target.flush()
    target.seek(0)


def delete(key: str) -> None:
    """Remove an uploaded object that turned out not to be wanted."""
    storage = _storage()
    if storage is not None:
        storage.connection.meta.client.delete_object(
            Bucket=storage.bucket_name, Key=storage._normalize_name(key)
        )
