"""Getting bytes into the archive without routing them through gunicorn.

    THE FILE NEVER TOUCHES THE APP SERVER IN PRODUCTION.

This is the one place in the project where direct-to-object-storage earns its complexity, and it is
worth being clear about why, because the media path deliberately does the opposite. Opslag images
are capped at 5 MB and already downscaled in the browser, so posting them through Django costs
nothing. Arkiv holds the video somebody took at sommerfest: 2 GB, uploaded from a phone on dorm
wifi, against three synchronous gunicorn workers with a 60-second timeout. That upload cannot go
through the app - not slowly, not at all.

**Two steps, so the database never holds a row for bytes that never arrived.**

    begin   the server checks access, decides the key, and returns a presigned POST policy
    (PUT)   the browser sends the file straight to Hetzner
    commit  the server HEADs the object and only then creates the ArchiveFile row

An upload abandoned halfway leaves an object nobody references, which the lifecycle rule's
`AbortIncompleteMultipartUpload` and a future audit sweep clean up. The reverse - a row pointing at
bytes that are not there - would be a broken file in a listing with nothing to explain it, so the
order matters.

**The HEAD is the real check.** The policy carries `content-length-range`, which stops a 40 GB
upload before it starts, and the client's declared content type is a hint the client controls. What
the row records is what the bucket actually has.

**The key is decided by the server, never by the client**, and it is derived from a hash the client
supplies. A client that lies about the hash gets an object stored under the wrong key and a row that
points at it consistently - it can corrupt its own upload, and nothing else, because the key space
is a flat hash namespace under `arkiv/` with no traversal to exploit.

Dev and CI have no bucket, so `begin` says so and the browser posts the file to `upload_direct`
instead. Same two views, same access checks, one branch in the JavaScript - the same shape as
core.media.serve_media, and for the same reason: a path that only works in production is a path no
test covers.

**THE BUCKET NEEDS A CORS RULE, AND ONLY PRODUCTION WILL NOTICE.** The browser POSTs from
https://gahk.dk to https://<bucket>.<loc>.your-objectstorage.com, which is cross-origin; without an
allowed origin the browser refuses the request before it is sent. Nothing in dev or CI can catch
this, because the local path never leaves the app. Reading is unaffected - a download is a top-level
navigation to a redirect, not a fetch - so this became necessary the day upload landed and not
before. DEPLOY.md 4c carries the rule.
"""

from typing import Any

from django.conf import settings

from .models import ARCHIVE_PREFIX, object_key, preview_key, thumbnail_key
from .storage import S3ArchiveStore, get_store

# What a resident may put in the archive in one go. Generous on purpose - the point of this feature
# is the things that did not fit anywhere else - but not unbounded, because the policy below is what
# stands between one mistyped `dd` and the month's storage bill.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024

# How long the browser has to start the upload. Not how long it has to finish: S3 checks the policy
# when the request arrives, so a slow 2 GB upload that began in time completes fine.
UPLOAD_TTL = 3600

# A 320px JPEG is a few tens of KB. Half a megabyte is generous and still small enough that the
# thumbnail slot cannot be used to smuggle a full-size second upload past the cap above.
MAX_THUMBNAIL_BYTES = 512 * 1024

# The viewer size. A 1600px JPEG is usually a few hundred KB; four megabytes is room for a
# detailed photograph at that size and still nothing like a way round MAX_UPLOAD_BYTES.
MAX_PREVIEW_BYTES = 4 * 1024 * 1024

# The two sizes the BROWSER produces alongside the original, by name: where each one lives and what
# it may weigh. Keyed by the string the client sends, so adding a third size is a line here, a line
# in imageupload.ts, and nothing else.
#
# Both are made client-side for the same reason the project has no Celery: the alternative is
# Pillow in the production image and a scheduled sweep, which would also leave a window where a
# just-uploaded photograph has no preview - and the person who just uploaded is precisely the one
# about to open the folder and look. `make_arkiv_thumbnails` stays the one-off for the imported
# backlog and the hand-run net for anything a browser could not decode.
DERIVED_SIZES: dict[str, tuple[Any, int]] = {
    "thumbnail": (thumbnail_key, MAX_THUMBNAIL_BYTES),
    "preview": (preview_key, MAX_PREVIEW_BYTES),
}


def derived_limit(kind: str) -> int:
    """What a derived image of this kind may weigh. Unknown kinds get the smallest cap."""
    return DERIVED_SIZES.get(kind, (None, MAX_THUMBNAIL_BYTES))[1]


def derived_key(kind: str, sha256: str) -> str | None:
    """Where a derived image of this kind lives, or None if the kind is not one we make."""
    spec = DERIVED_SIZES.get(kind)
    return None if spec is None else str(spec[0](sha256))


def presigned_derived_post(kind: str, sha256: str) -> dict[str, Any] | None:
    """A policy for one derived size of `sha256`, or None when there is no bucket.

    Capped far lower than the original, per size: a derived image that is not small has no reason to
    exist, and the cap is what stops these slots being used to smuggle a second full-size upload
    past the limit on the real one.
    """
    store = get_store()
    key = derived_key(kind, sha256)
    if key is None or not isinstance(store, S3ArchiveStore):
        return None
    return store.presigned_post(
        key,
        fields={"key": key, "Content-Type": "image/jpeg"},
        conditions=[
            ["content-length-range", 1, derived_limit(kind)],
            {"key": key},
            {"Content-Type": "image/jpeg"},
        ],
        expires_in=UPLOAD_TTL,
    )


def presigned_post(sha256: str, content_type: str) -> dict[str, Any] | None:
    """A policy the browser can POST a file to, or None when there is no bucket (dev, CI).

    `content-length-range` is the part that matters: without it a presigned URL is an open invitation
    to fill the bucket, and the row-level size check at commit would only notice afterwards, once the
    bytes were already paid for.
    """
    store = get_store()
    if not isinstance(store, S3ArchiveStore):
        return None
    key = object_key(sha256)
    conditions: list[Any] = [
        ["content-length-range", 1, MAX_UPLOAD_BYTES],
        {"key": key},
    ]
    fields: dict[str, Any] = {"key": key}
    if content_type:
        fields["Content-Type"] = content_type
        conditions.append({"Content-Type": content_type})
    return store.presigned_post(key, fields=fields, conditions=conditions, expires_in=UPLOAD_TTL)


def stored_object(sha256: str) -> tuple[int, str] | None:
    """`(size, content_type)` of the object behind `sha256`, or None if the bucket has not got it.

    This is what makes commit trustworthy: everything else in the exchange is the client's word.
    """
    store = get_store()
    key = object_key(sha256)
    head = getattr(store, "head", None)
    if head is not None:
        return head(key)
    # Local backend: the file is on disk, so its size is authoritative in the same way.
    if store.exists(key):
        from pathlib import Path

        path = Path(settings.MEDIA_ROOT) / key
        return path.stat().st_size, ""
    return None


def stored_derived(kind: str, sha256: str) -> bool:
    """Whether the store actually holds this derived size. Checked before the flag is set.

    Asked of the store rather than taken from the client, because a flag set on the client's word
    renders a broken image for every file whose derived upload quietly failed.
    """
    key = derived_key(kind, sha256)
    if key is None:
        return False
    store = get_store()
    head = getattr(store, "head", None)
    if head is not None:
        return head(key) is not None
    return store.exists(key)


def valid_hash(value: str) -> bool:
    """A sha256 hex digest and nothing else.

    Checked before the value is ever used to build a key. `object_key` interpolates it into a path,
    so this is what keeps that path a flat namespace under `arkiv/` rather than something a client
    can steer.
    """
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


def upload_prefix() -> str:
    """Exposed so a template or a test can name the prefix without importing the model."""
    return ARCHIVE_PREFIX
