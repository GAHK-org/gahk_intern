"""Shared rules for image uploads across every feature that accepts one.

One policy, three entry points. Callers keep their own *reaction* to a bad file — that is the part
that legitimately differs (the CMS admin refuses the form, the opslagstavle's upload endpoint
answers 400 to a fetch) — but what counts as an acceptable image is decided here, once.

The third entry point, `attached_image`, is the whole reaction as well as the check, because three
features had independently written the same one: an OPTIONAL photo beside a text field, where a bad
file must warn and be dropped rather than fail the submission. See its docstring.

Consolidated from three near-duplicates that had already drifted apart: cms/images.py (strict),
den_hurtige/views.py::_validated_image and rooms/views.py (both content-type-prefix only). The two
lenient copies accepted `image/svg+xml`, which is the exact hole the strict one was written to
close — see below.

**No SVG.** An SVG is a document, not a picture: it can carry <script>, and served from our own
origin at /media/ a direct navigation to it would execute that script as us — straight past nh3,
which only ever sees the page HTML. The CMS-editor roles are trusted, but the sanitizer exists
precisely so a compromised editor account cannot inject script, and allowing SVG would hand that
back. Værelsestjek is open to every resident, so there it was not even a compromised-account
question. Raster only; export vectors to PNG.
"""

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError

ALLOWED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
ALLOWED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
EXTENSION_HELP = "JPEG, PNG, GIF eller WebP"


def check_image_upload(upload: Any, max_mb: int) -> str | None:  # noqa: ANN401 — an UploadedFile
    """Return a Danish error message, or None when `upload` is an image we are willing to serve.

    Both the content type and the filename extension are checked. The content type is a hint the
    client controls, and the extension is what the file is ultimately served as — so an `image/png`
    header on a `.svg` name has to fail, and it does.
    """
    content_type = (getattr(upload, "content_type", "") or "").lower()
    name = (getattr(upload, "name", "") or "").lower()

    if content_type not in ALLOWED_CONTENT_TYPES:
        return f"Filen er ikke et billede (tilladt: {EXTENSION_HELP})."
    if not any(name.endswith(ext) for ext in ALLOWED_EXTENSIONS):
        return f"Filendelsen passer ikke til et billede (tilladt: {EXTENSION_HELP})."
    size = getattr(upload, "size", 0) or 0
    if size > max_mb * 1024 * 1024:
        return f"Billedet er for stort (over {max_mb} MB)."
    return None


def validate_image_upload(upload: Any, max_mb: int) -> None:  # noqa: ANN401 — an UploadedFile
    """check_image_upload as a ValidationError — for ModelForms and JSON endpoints."""
    message = check_image_upload(upload, max_mb)
    if message is not None:
        raise ValidationError(message)


def attached_image(request: Any, max_mb: int) -> Any | None:  # noqa: ANN401 — HttpRequest/UploadedFile
    """The request's attached image, or None — for "nothing attached" and "attached but refused".

    WARNS AND DROPS RATHER THAN FAILING, which is the reaction the three callers all wanted and is
    the reason this is one function rather than three: losing an urgent message, or a paragraph
    somebody typed under a photo, because the picture was wrong is the worse outcome. A refused file
    leaves a `messages.warning` behind, so the resident is told it did not count even when whatever
    they were writing saves.

    COLLAPSING "nothing" AND "refused" INTO ONE None is deliberate and load-bearing downstream. It
    keeps the callers simple, and it means a photo-only submission whose photo was refused falls
    into the caller's own "you wrote nothing" branch — which is the right landing place, since there
    is genuinely nothing to save. What that costs is that the empty-submission message alone would
    explain only half of it, which is why the warning has to arrive beside it; every caller relies
    on Django's message framework carrying both through the redirect, and each has a test for it.

    `max_mb` stays a parameter because the ceiling is per-feature configuration (QUICK_POST_MAX_MB,
    NOTICE_IMAGE_MAX_MB, EVENT_IMAGE_MAX_MB) rather than policy this module gets to decide.

    Extracted at the third caller, on the schedule core/rollout.py's docstring sets out. The first
    copy was Den Hurtige's `_validated_image` — a backstop behind imageupload.ts, which downscales
    in the browser — and the second and third were written by copying it, which is how copies stop
    agreeing.
    """
    upload = request.FILES.get("image")
    if not upload:
        return None
    error = check_image_upload(upload, max_mb)
    if error is not None:
        messages.warning(request, f"{error} Billedet blev ikke gemt.")
        return None
    return upload
