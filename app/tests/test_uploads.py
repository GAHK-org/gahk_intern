"""The one image-upload policy (core.uploads), shared by the CMS, Den Hurtige, værelsestjek and
opslagstavlen.

Consolidating three drifted copies is only worth it if the policy itself is pinned down, so most of
these are pure unit tests over `check_image_upload` — no DB, no client.

`attached_image` is the exception and is tested here too, because it is not just the check: it is
the warn-and-drop REACTION that three features had each written for themselves (Den Hurtige's
messages and replies, and the comment forms on opslagstavlen and begivenheder). Each of those has
an end-to-end test of its own, but none of them pins the contract the other two now depend on, so
it gets tested where it lives. The remaining per-feature reactions (refuse the form / answer 400)
stay with their features.
"""

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from core.images import MAX_DIMENSION
from core.uploads import attached_image, check_image_upload, validate_image_upload

from .media_samples import heic_bytes

JPEG = b"\xff\xd8\xff" + b"x" * 64


def upload(name: str, content_type: str, body: bytes = JPEG) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, body, content_type=content_type)


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("foto.jpg", "image/jpeg"),
        ("foto.jpeg", "image/jpeg"),
        ("plakat.png", "image/png"),
        ("logo.gif", "image/gif"),
        ("moderne.webp", "image/webp"),
        ("SKRIGENDE.JPG", "image/jpeg"),  # case must not matter
    ],
)
def test_a_real_raster_image_is_accepted(name: str, content_type: str) -> None:
    assert check_image_upload(upload(name, content_type), max_mb=5) is None


def test_an_svg_is_refused() -> None:
    """The whole reason this module exists. An SVG is a document that can carry <script>, and served
    from our own origin at /media/ a direct navigation would execute it as us — past nh3, which only
    ever sees page HTML."""
    assert check_image_upload(upload("logo.svg", "image/svg+xml"), max_mb=5) is not None


def test_a_disguised_extension_is_refused() -> None:
    """content_type is a hint the client controls; the extension is what the file is *served* as, so
    both have to agree before we will host it."""
    assert check_image_upload(upload("evil.svg", "image/png"), max_mb=5) is not None


def test_a_disguised_content_type_is_refused() -> None:
    assert check_image_upload(upload("foto.jpg", "image/svg+xml"), max_mb=5) is not None


@pytest.mark.parametrize("content_type", ["application/pdf", "text/html", "", "application/x-php"])
def test_a_non_image_is_refused(content_type: str) -> None:
    assert check_image_upload(upload("payload.jpg", content_type), max_mb=5) is not None


def test_the_size_cap_is_the_callers_to_choose() -> None:
    """Each feature passes its own settings value (CMS_IMAGE_MAX_MB, QUICK_POST_MAX_MB,
    ROOM_PHOTO_MAX_MB, NOTICE_IMAGE_MAX_MB), so the cap is an argument and not a global."""
    big = upload("stor.jpg", "image/jpeg", b"\xff\xd8\xff" + b"x" * (2 * 1024 * 1024))

    assert check_image_upload(big, max_mb=5) is None
    assert check_image_upload(big, max_mb=1) is not None


# --- the animation ceiling ------------------------------------------------------------------------
#
# A GIF is the one permitted format that reaches us at the size the resident picked. Every other
# image is redrawn through a canvas on its way out of the browser, and a canvas cannot keep more
# than one frame — so frontend/src/imageupload.ts passes animations through untouched, and the cap
# they are held to has to be written for un-shrunk files. These pin that it is, and that it is the
# only thing the animation ceiling moves.

GIF_HEADER = b"GIF89a"


def gif(megabytes: float = 0) -> SimpleUploadedFile:
    return upload("reaktion.gif", "image/gif", GIF_HEADER + b"x" * int(megabytes * 1024 * 1024))


def test_a_gif_is_measured_against_the_animation_ceiling(settings: object) -> None:
    """The bug this fixes: a 6 MB reaction GIF refused by a cap written for photographs that the
    browser has already shrunk to a few hundred KB — which a GIF never is."""
    settings.ANIMATED_IMAGE_MAX_MB = 15  # type: ignore[attr-defined]

    assert check_image_upload(gif(6), max_mb=5) is None


def test_the_animation_ceiling_replaces_the_callers_rather_than_raising_it(settings: object) -> None:
    """Not max() of the two. A *_MAX_MB says how big a PHOTOGRAPH may be, so a feature with a
    generous photo cap must not thereby acquire an even more generous GIF cap — the two knobs are
    meant to be readable on their own."""
    settings.ANIMATED_IMAGE_MAX_MB = 0  # type: ignore[attr-defined] — any GIF counts as too big

    assert check_image_upload(gif(), max_mb=50) is not None


def test_a_still_image_is_untouched_by_the_animation_ceiling(settings: object) -> None:
    """Per-format, so raising what an animation may weigh must not quietly raise what a JPEG may."""
    settings.ANIMATED_IMAGE_MAX_MB = 100  # type: ignore[attr-defined]
    big = upload("stor.jpg", "image/jpeg", b"\xff\xd8\xff" + b"x" * (2 * 1024 * 1024))

    assert check_image_upload(big, max_mb=1) is not None


def test_the_refusal_names_the_ceiling_that_actually_applied(settings: object) -> None:
    """A resident told "over 5 MB" while the real limit is another number cannot act on it."""
    settings.ANIMATED_IMAGE_MAX_MB = 0  # type: ignore[attr-defined]

    assert "0 MB" in (check_image_upload(gif(), max_mb=5) or "")


def test_the_messages_are_danish() -> None:
    """User-facing text is Danish; these strings are shown verbatim by every caller."""
    assert "billede" in (check_image_upload(upload("x.pdf", "application/pdf"), max_mb=5) or "")
    assert "stort" in (check_image_upload(upload("x.jpg", "image/jpeg"), max_mb=0) or "")


def test_the_validating_wrapper_raises_for_forms_and_json_endpoints() -> None:
    validate_image_upload(upload("ok.png", "image/png"), max_mb=5)  # must not raise

    with pytest.raises(ValidationError):
        validate_image_upload(upload("logo.svg", "image/svg+xml"), max_mb=5)


# --- attached_image: the shared warn-and-drop reaction --------------------------------------------


class _FakeRequest:
    """Just enough request for `attached_image`: the FILES mapping it reads, and somewhere for the
    warning to go.

    Hand-rolled rather than RequestFactory + the message middleware, because the middleware is what
    would have to be assembled to make `messages.warning` land somewhere inspectable — and that
    assembly is the part that would be testing Django. What matters here is "was a warning raised at
    all, and was the file returned or dropped", which this answers without a database.
    """

    def __init__(self, files: dict) -> None:
        self.FILES = files
        self._messages: list = []


def _patched(monkeypatch: object, request: _FakeRequest) -> None:
    from core import uploads

    monkeypatch.setattr(  # type: ignore[attr-defined]
        uploads.messages, "warning", lambda _req, message: request._messages.append(message)
    )


def test_nothing_attached_is_none_and_says_nothing(monkeypatch: object) -> None:
    """No photo is the ordinary case, not a problem — it must not warn."""
    request = _FakeRequest({})
    _patched(monkeypatch, request)

    assert attached_image(request, max_mb=5) is None
    assert request._messages == []


def test_an_acceptable_image_comes_back_unchanged(monkeypatch: object) -> None:
    request = _FakeRequest({"image": upload("foto.jpg", "image/jpeg")})
    _patched(monkeypatch, request)

    assert attached_image(request, max_mb=5) is request.FILES["image"]
    assert request._messages == []


def test_a_refused_image_is_dropped_with_a_warning_rather_than_raising(monkeypatch: object) -> None:
    """THE CONTRACT ALL THREE CALLERS DEPEND ON. A ValidationError here would lose the message or
    comment typed beside the photo, which is the outcome every caller was written to avoid."""
    request = _FakeRequest({"image": upload("evil.svg", "image/svg+xml")})
    _patched(monkeypatch, request)

    assert attached_image(request, max_mb=5) is None
    assert len(request._messages) == 1
    assert "Billedet blev ikke gemt" in request._messages[0]


def test_the_warning_repeats_why_the_file_was_refused(monkeypatch: object) -> None:
    """Not just "it did not count": the reason comes from check_image_upload, so a resident can fix
    it. Two different reasons, so this cannot pass by hard-coding one."""
    svg = _FakeRequest({"image": upload("logo.svg", "image/svg+xml")})
    _patched(monkeypatch, svg)
    attached_image(svg, max_mb=5)

    big = _FakeRequest({"image": upload("stor.jpg", "image/jpeg", body=b"x" * (2 * 1024 * 1024))})
    _patched(monkeypatch, big)
    attached_image(big, max_mb=1)

    assert "billede" in svg._messages[0]
    assert "stort" in big._messages[0]


def test_the_ceiling_is_the_callers_to_choose(monkeypatch: object) -> None:
    """`max_mb` is per-feature configuration (QUICK_POST / NOTICE_IMAGE / EVENT_IMAGE), not
    something this module decides — the same file passes or fails on the caller's number."""
    body = b"\xff\xd8\xff" + b"x" * (2 * 1024 * 1024)

    ok = _FakeRequest({"image": upload("foto.jpg", "image/jpeg", body=body)})
    _patched(monkeypatch, ok)
    assert attached_image(ok, max_mb=5) is not None

    refused = _FakeRequest({"image": upload("foto.jpg", "image/jpeg", body=body)})
    _patched(monkeypatch, refused)
    assert attached_image(refused, max_mb=1) is None


# --- HEIC: accepted, and converted before anything is stored ----------------------------------------
#
# An iPhone saves HEIC by default and only Safari can decode it. frontend/src/imageupload.ts cannot
# redraw it in any other browser, so it reaches the server at full camera size, and storing it would
# leave every non-Safari reader with a broken image. These pin that it is turned into a JPEG first.


def heic(
    name: str = "IMG_4821.HEIC", content_type: str = "image/heic", size: tuple[int, int] = (64, 48)
) -> SimpleUploadedFile:
    return upload(name, content_type, heic_bytes(size))


@pytest.mark.parametrize("content_type", ["image/heic", "image/heif", "", "application/octet-stream"])
def test_a_heic_is_accepted_even_without_a_type(content_type: str) -> None:
    """Chrome on Windows without Microsoft's HEIF extension sends no type at all for a .heic."""
    assert check_image_upload(heic(content_type=content_type), max_mb=5) is None


def test_an_untyped_file_is_still_refused_without_a_heic_extension() -> None:
    """The untyped exemption is for HEIC only; it must not reopen the door for anything else."""
    assert check_image_upload(upload("logo.svg", ""), max_mb=5) is not None
    assert check_image_upload(upload("foto.jpg", "application/octet-stream"), max_mb=5) is not None


def test_a_heic_is_stored_as_a_downscaled_jpeg() -> None:
    stored = validate_image_upload(heic(size=(3200, 2400)), max_mb=5)

    assert stored.name == "IMG_4821.jpg"
    assert stored.content_type == "image/jpeg"
    with Image.open(stored) as image:
        assert image.format == "JPEG"
        assert max(image.size) == MAX_DIMENSION


def test_a_file_that_only_claims_to_be_heic_is_refused() -> None:
    """The extension is not taken on trust: the file is decoded, and a fake fails there."""
    with pytest.raises(ValidationError):
        validate_image_upload(upload("evil.heic", "image/heic", b"<svg onload=alert(1)>"), max_mb=5)


def test_a_heic_has_its_own_ceiling(settings: object) -> None:
    """It arrives un-shrunk, like a GIF, so the caller's cap (written for downscaled files) would
    refuse a perfectly ordinary iPhone photo."""
    settings.HEIF_IMAGE_MAX_MB = 25  # type: ignore[attr-defined]
    big = upload("IMG.heic", "image/heic", b"x" * (2 * 1024 * 1024))
    assert check_image_upload(big, max_mb=1) is None


def test_an_attached_heic_comes_back_as_a_jpeg(monkeypatch: object) -> None:
    request = _FakeRequest({"image": heic()})
    _patched(monkeypatch, request)

    stored = attached_image(request, max_mb=5)
    assert stored is not None
    assert stored.name == "IMG_4821.jpg"
    assert request._messages == []
