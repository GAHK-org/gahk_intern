"""The import script's archive goes straight into the bucket, not through the app server.

A ZIP posted to the app passes Coolify's Traefik on the way, and Traefik cuts any request whose
body takes longer than a minute to arrive: the Dropbox album "Bornholm med GAHK - April 06", 3.2 GB,
was dropped six times in a row while every album under 540 MB went through. Even with the proxy out
of the way, one uvicorn process would receive the gigabytes, spool them to disk twice and copy them
to the bucket before answering. Arkiv went direct for the same reason (arkiv/uploads.py).

So the script asks for an upload, sends the archive to the bucket as a multipart upload — parts of
PART_SIZE, each to a presigned URL and each tried again on its own — and then submits the import
with the ticket it was given. Only then is the upload completed and checked, and the import queued
against that object, as if the archive had arrived with the request.

**The ticket is signed**, so the key and the upload it names are the server's own: the script picks
neither. The size it announced is checked against what the bucket holds once completed.

An upload abandoned halfway is aborted by the bucket's lifecycle rule (DEPLOY.md,
`abort-incomplete-uploads`). Without a bucket — dev, CI — there is no direct upload, and the script
sends the archive with its request as before.
"""

import math
from pathlib import Path
from typing import Any
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from django.core import signing
from django.core.exceptions import ValidationError

from .storage import PhotoAlbumS3Storage, get_photo_album_storage

# Small enough that a part goes in well under a minute on a slow line, and that a failure costs
# only that part; large enough that a 5 GB archive is under a hundred of them.
PART_SIZE = 64 * 1024 * 1024
MAX_PARTS = 10_000  # S3's own limit
# How long the part URLs and the ticket stay good: a whole album sent over a slow line.
TTL = 24 * 60 * 60
_SALT = "photo_album.direct_upload"


def _storage() -> PhotoAlbumS3Storage | None:
    storage = get_photo_album_storage()
    return storage if isinstance(storage, PhotoAlbumS3Storage) else None


def begin(name: str, size: int) -> dict[str, Any] | None:
    """Start a multipart upload of `size` bytes for an archive called `name`; None without a bucket.

    Returns the ticket to submit the import with, the part size, and one presigned URL per part.
    """
    storage = _storage()
    if storage is None:
        return None
    key = f"photo-album-imports/{uuid4()}/{Path(name).name}"
    part_size = max(PART_SIZE, math.ceil(size / MAX_PARTS))
    bucket, object_key = storage.bucket_name, storage._normalize_name(key)
    upload_id = storage.connection.meta.client.create_multipart_upload(
        Bucket=bucket, Key=object_key, ContentType="application/zip"
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
    ticket = signing.dumps({"key": key, "upload": upload_id, "name": name, "size": size}, salt=_SALT)
    return {"ticket": ticket, "partSize": part_size, "urls": urls}


def finish(ticket: str, etags: list[str]) -> tuple[str, str]:
    """Complete the upload a ticket names, from its parts' ETags in order; returns (key, name).

    Raises ValidationError for a ticket this server did not sign (or one past TTL), for parts the
    bucket does not take, and for an object of another size than was announced.
    """
    try:
        upload = signing.loads(ticket, salt=_SALT, max_age=TTL)
    except signing.BadSignature as exc:
        raise ValidationError("Uploaden er ukendt eller for gammel. Send ZIP-filen igen.") from exc
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
        raise ValidationError(f"Uploaden kunne ikke afsluttes ({exc}). Send ZIP-filen igen.") from exc
    if stored != upload["size"]:
        client.delete_object(Bucket=bucket, Key=object_key)
        raise ValidationError(
            f"Lageret fik {stored} bytes, men ZIP-filen var på {upload['size']}. Send ZIP-filen igen."
        )
    return upload["key"], upload["name"]
