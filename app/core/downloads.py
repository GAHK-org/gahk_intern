"""The Content-Disposition header for a file download, which more than one domain hands out."""

from urllib.parse import quote


def content_disposition(filename: str) -> str:
    """`attachment` with the resident's filename, spelled both ways.

    Both spellings, because neither alone is enough: the quoted `filename=` is all an old client
    understands and cannot carry a non-ASCII byte, and `filename*=` is the RFC 5987 form that can.
    Danish names have spaces and aeoeaa in them, so this is the normal case here, not the edge one.

    In core because two domains need it: arkiv and the photo album both set it on presigned GETs,
    where the object key is a bare hash and the browser would otherwise save `9f86d081...` with no
    extension, and on zip responses, which have no presigned URL to put it on.
    """
    return f"attachment; filename=\"{_ascii_fallback(filename)}\"; filename*=UTF-8''{_rfc5987(filename)}"


def _ascii_fallback(filename: str) -> str:
    """A filename old clients can parse: ASCII, no quotes, never empty."""
    cleaned = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    return cleaned or "download"


def _rfc5987(filename: str) -> str:
    """Percent-encoded UTF-8 for the `filename*` parameter, which is what modern browsers read."""
    return quote(filename, safe="")
