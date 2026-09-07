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

from core.uploads import attached_image, check_image_upload, validate_image_upload

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
