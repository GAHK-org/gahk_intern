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

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError

ALLOWED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
ALLOWED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
EXTENSION_HELP = "JPEG, PNG, GIF eller WebP"

GIF_CONTENT_TYPE = "image/gif"
GIF_EXTENSION = ".gif"


def _size_ceiling(content_type: str, name: str, max_mb: int) -> int:
    """The ceiling in MB for this particular file: the caller's, or the animation one for a GIF.

    THE ONE PIECE OF POLICY THIS MODULE DECIDES RATHER THAN TAKES, and the distinction is what makes
    it defensible: `max_mb` is per-FEATURE configuration, which is the caller's to choose and stays
    that way, while this is per-FORMAT, which no caller is in a position to know better than here.

    A GIF is the only permitted format that reaches us at the size the resident picked.
    frontend/src/imageupload.ts redraws every other image through a canvas on its way out of the
    browser, but a canvas cannot compress an animation — it decodes frame one and discards the rest
    — so animations are passed through untouched. Holding them to a cap written for
    already-downscaled photographs refused exactly the reaction GIFs the feature is for.

    settings.ANIMATED_IMAGE_MAX_MB REPLACES the caller's number rather than being max()'d with it,
    so the two knobs stay readable in isolation: a *_MAX_MB says how big a photograph may be, this
    says how big an animation may be, and neither quietly moves when the other is changed.

    Decided on the content type and the extension, the same two things the checks above trust, and
    for the same reason: both have already had to agree before we get here. Animated WebP and APNG
    are NOT covered — telling them from their still forms needs the file's bytes, not its metadata,
    and this function deliberately never reads the body. They keep the caller's cap, which is the
    conservative direction to be wrong in.
    """
    if content_type == GIF_CONTENT_TYPE or name.endswith(GIF_EXTENSION):
        return settings.ANIMATED_IMAGE_MAX_MB
    return max_mb


def check_image_upload(upload: Any, max_mb: int) -> str | None:  # noqa: ANN401 — an UploadedFile
    """Return a Danish error message, or None when `upload` is an image we are willing to serve.

    Both the content type and the filename extension are checked. The content type is a hint the
    client controls, and the extension is what the file is ultimately served as — so an `image/png`
    header on a `.svg` name has to fail, and it does.

    `max_mb` is the ceiling for a still image; a GIF is measured against the animation ceiling
    instead, for the reason `_size_ceiling` gives.
    """
    content_type = (getattr(upload, "content_type", "") or "").lower()
    name = (getattr(upload, "name", "") or "").lower()

    if content_type not in ALLOWED_CONTENT_TYPES:
        return f"Filen er ikke et billede (tilladt: {EXTENSION_HELP})."
    if not any(name.endswith(ext) for ext in ALLOWED_EXTENSIONS):
        return f"Filendelsen passer ikke til et billede (tilladt: {EXTENSION_HELP})."
    # The applied ceiling, not the argument: the number in the message is the one the resident has
    # to get under, and for a GIF those two are not the same.
    ceiling = _size_ceiling(content_type, name, max_mb)
    size = getattr(upload, "size", 0) or 0
    if size > ceiling * 1024 * 1024:
        return f"Billedet er for stort (over {ceiling} MB)."
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
    NOTICE_IMAGE_MAX_MB, EVENT_IMAGE_MAX_MB) rather than policy this module gets to decide. The one
    exception is an animation, which is measured against ANIMATED_IMAGE_MAX_MB instead — a question
    about the format rather than about the feature, so `_size_ceiling` answers it here.

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
