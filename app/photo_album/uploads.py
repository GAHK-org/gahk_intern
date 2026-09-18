"""What the photo album is willing to store, checked on the server.

core/uploads.py owns the policy for the shape every other feature has — one OPTIONAL image beside
a text field — and deliberately allows raster images only. The album cannot reuse it: it also takes
video and the HEIC/HEIF a modern iPhone produces, and its reaction to a bad file has to differ. In
the other features the picture is a garnish, so `attached_image` warns and drops it rather than
losing what somebody typed. Here the file *is* the submission, so a refused one has to fail the
upload with a message; silently dropping it would look exactly like a successful upload.

**No SVG**, for precisely the reason core/uploads.py's docstring sets out: an SVG is a document,
not a picture, and one served from our own origin under /media/ executes its script as us when
navigated to directly. The album is the worst place to allow it — every resident may upload, and
`upload_media` stores an unrecognised file unchanged as all three variants, so the thumbnail in the
grid would be the payload itself.

Both the content type and the extension are checked, for the reason given there too: the content
type is a hint the client controls, and the extension is what the file is ultimately served as.
"""

from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError

IMAGE_CONTENT_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/heif"}
)
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"})
# Browsers disagree about HEIC's content type and some send nothing at all for it, so the extension
# is what actually decides those two — hence both sets list them.
VIDEO_CONTENT_TYPES = frozenset({"video/mp4", "video/quicktime", "video/webm", "video/x-m4v"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".m4v"})

EXTENSION_HELP = "JPEG, PNG, GIF, WebP, HEIC, MP4, MOV, WebM eller M4V"


def is_video(upload: Any) -> bool:  # noqa: ANN401 — an UploadedFile
    """Whether this upload should take the video path through `upload_media`."""
    content_type = (getattr(upload, "content_type", "") or "").lower()
    name = (getattr(upload, "name", "") or "").lower()
    return content_type in VIDEO_CONTENT_TYPES or any(name.endswith(e) for e in VIDEO_EXTENSIONS)


def check_media_upload(upload: Any) -> str | None:  # noqa: ANN401 — an UploadedFile
    """Return a Danish error message, or None when the album is willing to store `upload`."""
    content_type = (getattr(upload, "content_type", "") or "").lower()
    name = (getattr(upload, "name", "") or "").lower()
    extension = name[name.rfind(".") :] if "." in name else ""

    video = extension in VIDEO_EXTENSIONS or content_type in VIDEO_CONTENT_TYPES
    allowed_extensions = VIDEO_EXTENSIONS if video else IMAGE_EXTENSIONS
    allowed_types = VIDEO_CONTENT_TYPES if video else IMAGE_CONTENT_TYPES

    if extension not in allowed_extensions:
        return f"Filtypen er ikke understøttet (tilladt: {EXTENSION_HELP})."
    # An empty content type is accepted only when the extension already vouched for the file: some
    # browsers send none for HEIC and for .mov. A *wrong* one still fails, so an image/png header on
    # a .svg name — or on a .mp4 name — cannot get through.
    if content_type and content_type not in allowed_types:
        return f"Filens indhold passer ikke til filtypen (tilladt: {EXTENSION_HELP})."

    maximum = settings.PHOTO_ALBUM_VIDEO_MAX_MB if video else settings.PHOTO_ALBUM_IMAGE_MAX_MB
    size = getattr(upload, "size", 0) or 0
    if size > maximum * 1024 * 1024:
        kind = "Videoen" if video else "Billedet"
        return f"{kind} er for stor{'' if video else 't'} (over {maximum} MB)."
    return None


def validate_media_upload(upload: Any) -> None:  # noqa: ANN401 — an UploadedFile
    """check_media_upload as a ValidationError — for the upload form."""
    message = check_media_upload(upload)
    if message is not None:
        raise ValidationError(message)
