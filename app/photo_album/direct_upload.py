"""Large files go straight into the bucket, not through the app server.

A body posted to the app passes Coolify's Traefik on the way, and Traefik cuts any request whose
body takes longer than a minute to arrive: the Dropbox album "Bornholm med GAHK - April 06", 3.2 GB,
was dropped six times in a row while every album under 540 MB went through. A resident's video on
dorm wifi meets the same minute. Even with the proxy out of the way, one uvicorn process would
receive the gigabytes, spool them to disk twice and copy them to the bucket before answering.
Arkiv went direct for the same reason (arkiv/uploads.py).

So the file goes to the bucket as an S3 multipart upload, in parts of PART_SIZE, each tried again
on its own; then it is submitted with the ticket the upload was given, and handled as if it had
arrived with the request: a ZIP is queued for import, a photo or video is checked and stored like
any upload. There are two ways to drive the upload:

  * The import script asks `begin`: the server starts the multipart upload and presigns every
    part, the script PUTs them, and `finish` completes the upload from their ETags.
  * The browser uses Uppy (@uppy/aws-s3), which drives the upload itself — create, the parts,
    complete, or abort when cancelled — and asks `sign` for each of those requests in turn. The
    ticket comes from `ticket`, and `finish` then only checks what arrived.

**The ticket is signed**, so the key it names is the server's own: the client picks it no more
than it picks anything else. It names what the upload is for, so an import's ticket cannot be spent
on an album upload or the other way round; a browser's also names who it was given to, and `sign`
signs nothing else, for nobody else. The size announced is checked against what the bucket holds.

A browser needs the bucket's CORS rule to allow POST, PUT and DELETE from the site and to expose
ETag (DEPLOY.md 4c). An upload abandoned halfway is aborted by the bucket's lifecycle rule
(`abort-incomplete-uploads`). Without a bucket — dev, CI — there is no direct upload, and the file
is sent with the request as before.
"""

import math
from datetime import datetime
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
# How long a ticket, and the import script's part URLs, stay good: a whole album over a slow line.
TTL = 24 * 60 * 60
# How long one URL `sign` gives the browser stays good: it asks again for every request.
SIGNED_TTL = 60 * 60
_SALT = "photo_album.direct_upload"

IMPORT = "import"
MEDIA = "media"

_UNKNOWN = "Uploaden er ukendt eller for gammel. Send filen igen."


def _storage() -> PhotoAlbumS3Storage | None:
    storage = get_photo_album_storage()
    return storage if isinstance(storage, PhotoAlbumS3Storage) else None


def available() -> bool:
    return _storage() is not None


def _part_size(size: int) -> int:
    return max(PART_SIZE, math.ceil(size / MAX_PARTS))


def _parts(size: int) -> int:
    return max(1, math.ceil(size / _part_size(size)))


IMPORT_PREFIX = "photo-album-imports/"


def import_key(name: str) -> str:
    """Where an archive to import goes: imports keep their archive under here until they are done."""
    return f"{IMPORT_PREFIX}{uuid4()}/{Path(name).name}"


def begin(
    key: str, size: int, *, purpose: str, content_type: str = "application/octet-stream", **extra: str | int
) -> dict[str, Any] | None:
    """For the import script: start a multipart upload of `size` bytes to `key`; None without a bucket.

    Returns the ticket to submit it with, the part size, and one presigned URL per part. `extra` is
    signed into the ticket with the key, for what the upload is submitted to.
    """
    storage = _storage()
    if storage is None:
        return None
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
        for number in range(1, _parts(size) + 1)
    ]
    ticket = signing.dumps(
        {**extra, "key": key, "upload": upload_id, "size": size, "purpose": purpose}, salt=_SALT
    )
    return {"ticket": ticket, "partSize": _part_size(size), "urls": urls}


def ticket(
    key: str, size: int, *, purpose: str, owner: int, content_type: str, **extra: str | int
) -> dict[str, Any] | None:
    """For the browser: a ticket for an upload of `size` bytes to `key` by resident `owner`, which
    Uppy then drives itself through `sign`; None without a bucket. Nothing is started in the bucket
    until Uppy asks."""
    if _storage() is None:
        return None
    data = {
        **extra,
        "key": key,
        "size": size,
        "purpose": purpose,
        "owner": owner,
        "contentType": content_type,
    }
    return {"ticket": signing.dumps(data, salt=_SALT), "partSize": _part_size(size)}


def _load(ticket: str, *, purpose: str | None = None) -> dict[str, Any]:
    try:
        upload = signing.loads(ticket, salt=_SALT, max_age=TTL)
    except signing.BadSignature as exc:
        raise ValidationError(_UNKNOWN) from exc
    if purpose is not None and upload.get("purpose") != purpose:
        raise ValidationError(_UNKNOWN)
    return dict(upload)


def sign(
    ticket: str, *, owner: int, method: str, key: str, upload_id: str = "", part_number: int | None = None
) -> dict[str, Any]:
    """A presigned URL for one of Uppy's requests for the upload a ticket names (@uppy/aws-s3's
    `signRequest`): start it, send a part, complete it, or abort it.

    Raises ValidationError for anything else — another resident's ticket, another key, a part past
    what the announced size needs, a single PUT or a DELETE of the object itself.
    """
    upload = _load(ticket)
    storage = _storage()
    if storage is None or upload.get("owner") != owner:
        raise ValidationError(_UNKNOWN)
    starting = method == "POST" and not upload_id
    if not starting and key != upload["key"]:
        raise ValidationError("Uploaden hører til en anden fil.")
    params: dict[str, Any] = {"Bucket": storage.bucket_name, "Key": storage._normalize_name(upload["key"])}
    headers: dict[str, str] = {}
    if starting:
        operation = "create_multipart_upload"
        params["ContentType"] = headers["Content-Type"] = upload["contentType"]
    elif method == "PUT" and upload_id and part_number is not None:
        if not 1 <= part_number <= _parts(upload["size"]):
            raise ValidationError("Filen er større end anmeldt.")
        operation = "upload_part"
        params.update(UploadId=upload_id, PartNumber=part_number)
    elif method == "POST":
        operation = "complete_multipart_upload"
        params["UploadId"] = upload_id
    elif method == "DELETE" and upload_id:
        operation = "abort_multipart_upload"
        params["UploadId"] = upload_id
    else:
        raise ValidationError("Den forespørgsel underskrives ikke.")
    url = storage.public_connection.meta.client.generate_presigned_url(
        operation, Params=params, ExpiresIn=SIGNED_TTL
    )
    return {"url": url, "key": upload["key"], **({"headers": headers} if headers else {})}


def finish(ticket: str, etags: list[str] | None, *, purpose: str, owner: int | None = None) -> dict[str, Any]:
    """Make sure the upload a ticket names is in the bucket, whole; returns what was signed into the
    ticket (`key`, `size` and the extras it was given).

    The import script's upload is completed here from its parts' ETags, in order; Uppy completes
    its own (etags None). Raises ValidationError for a ticket this server did not sign for
    `purpose` (or `owner`), past TTL, for parts the bucket does not take, and for an object of
    another size than was announced.
    """
    upload = _load(ticket, purpose=purpose)
    if "owner" in upload and upload["owner"] != owner:
        raise ValidationError(_UNKNOWN)
    storage = _storage()
    if storage is None:
        raise ValidationError("Serveren tager ikke imod uploads direkte til lageret.")
    client = storage.connection.meta.client
    bucket, object_key = storage.bucket_name, storage._normalize_name(upload["key"])
    try:
        if "upload" in upload:
            client.complete_multipart_upload(
                Bucket=bucket,
                Key=object_key,
                UploadId=upload["upload"],
                MultipartUpload={
                    "Parts": [{"ETag": etag, "PartNumber": n} for n, etag in enumerate(etags or [], 1)]
                },
            )
        stored = client.head_object(Bucket=bucket, Key=object_key)["ContentLength"]
    except (BotoCoreError, ClientError) as exc:
        raise ValidationError(f"Uploaden kunne ikke afsluttes ({exc}). Send filen igen.") from exc
    if stored != upload["size"]:
        delete(upload["key"])
        raise ValidationError(
            f"Lageret fik {stored} bytes, men filen var på {upload['size']}. Send filen igen."
        )
    return upload


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


def purge_orphaned_imports(referenced: set[str], *, older_than: datetime) -> list[str]:
    """Delete the archives under IMPORT_PREFIX that no import refers to and that were stored before
    `older_than`; returns their keys.

    What leaves one there: an upload completed but never submitted — a browser closed between the
    two, or a script stopped — and, before views._queue_import learnt to delete it, an import that
    could not be created. A completed object is not what the bucket's lifecycle rule cleans up.
    """
    storage = _storage()
    if storage is None:
        return []
    client = storage.connection.meta.client
    prefix = storage._normalize_name(IMPORT_PREFIX)
    orphans = [
        item["Key"]
        for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=storage.bucket_name, Prefix=prefix
        )
        for item in page.get("Contents", [])
        if item["LastModified"] < older_than and item["Key"] not in referenced
    ]
    for key in orphans:
        client.delete_object(Bucket=storage.bucket_name, Key=key)
    return orphans


def delete(key: str) -> None:
    """Remove an uploaded object that turned out not to be wanted."""
    storage = _storage()
    if storage is not None:
        storage.connection.meta.client.delete_object(
            Bucket=storage.bucket_name, Key=storage._normalize_name(key)
        )
