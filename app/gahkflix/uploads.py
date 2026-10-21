"""Getting a film into GAHKFlix: the browser sends it straight to the bucket, as Arkiv does.

    begin   the server checks the file and the resident, decides the key, signs a token and hands
            back a presigned POST policy (or, with no bucket, "post it to us instead")
    (POST)  the browser sends the file to the bucket
    commit  the server checks the token and asks the bucket what arrived, and only then creates
            the rows and queues the encode

So the database never holds a row for bytes that never arrived, and a refused commit deletes what
did. The token carries everything begin decided, signed, so commit trusts none of the client's
word except which file it was talking about.

Two checks the spec asks for happen at begin, before a byte is sent:

  * ONE UPLOAD AT A TIME: nobody starts another upload while one of theirs is still processing.
  * THE SAME FILE TWICE: the browser hashes the file's size and its first and last 4 MB (cheap even
    for 5 GB, on a phone) and any video with that fingerprint is a duplicate. The worker computes
    the real SHA-256 of the whole file afterwards and refuses a duplicate the fingerprint missed.
"""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core import signing

from photo_album.uploads import UNKNOWN_CONTENT_TYPES, VIDEO_CONTENT_TYPES, VIDEO_TYPES_BY_EXTENSION

# The signing namespace of the token (django.core.signing), not a secret.
TOKEN_SALT = "gahkflix.upload"  # noqa: S105  # nosec B105
# Commit comes when the upload is done, which for a big file on dorm wifi can be hours after begin.
TOKEN_MAX_AGE = 24 * 60 * 60

EXTENSION_HELP = "MP4, MOV, M4V, WebM, AVI, MKV, 3GP, MTS, MPEG, WMV eller OGV"


def max_upload_bytes() -> int:
    return int(settings.GAHKFLIX_MAX_UPLOAD_GB * 1024 * 1024 * 1024)


def content_type_for(name: str) -> str:
    return VIDEO_TYPES_BY_EXTENSION.get(Path(name.lower()).suffix, "")


def check_file(name: str, size: int, content_type: str) -> str | None:
    """A Danish reason to refuse the file, or None. The bytes are checked by the worker's ffmpeg."""
    if not name.strip():
        return "Filen mangler et navn."
    if not content_type_for(name):
        return f"Filtypen er ikke understøttet (tilladt: {EXTENSION_HELP})."
    declared = content_type.lower()
    if declared not in UNKNOWN_CONTENT_TYPES and declared not in VIDEO_CONTENT_TYPES:
        return f"Filen ser ikke ud til at være en video (tilladt: {EXTENSION_HELP})."
    if size <= 0:
        return "Filen er tom."
    if size > max_upload_bytes():
        return f"Videoen er for stor (over {settings.GAHKFLIX_MAX_UPLOAD_GB:g} GB)."
    return None


def valid_fingerprint(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass
class UploadTicket:
    """What begin decided, signed into the token commit gets back."""

    resident: int
    video_uuid: str
    file_uuid: str
    key: str
    name: str
    size: int
    content_type: str
    fingerprint: str
    last_modified: str  # the browser's File.lastModified, milliseconds since the epoch
    video: int | None  # set for a new version of an existing video

    def sign(self) -> str:
        return signing.dumps(asdict(self), salt=TOKEN_SALT, compress=True)

    @classmethod
    def load(cls, token: str) -> "UploadTicket | None":
        try:
            data: dict[str, Any] = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
        except signing.BadSignature:
            return None
        try:
            return cls(**data)
        except TypeError:
            return None
