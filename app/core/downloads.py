"""The `Content-Disposition` a download is given, so the browser saves it under the resident's name.

A bucket key is not the name a resident gave the file, so every download states that name itself:
on the presigned GET (`ResponseContentDisposition`) or on the response.
"""

from urllib.parse import quote


def content_disposition(filename: str, *, inline: bool = False) -> str:
    """`attachment` with the resident's filename, spelled both ways - or `inline`, for what the
    browser is to show itself (a PDF, a video), still under that name if it is saved from there.

    Both spellings, because neither alone is enough: the quoted `filename=` is all an old client
    understands and cannot carry a non-ASCII byte, and `filename*=` is the RFC 5987 form that can.
    Danish names have spaces and æøå in them, so this is the normal case here, not the edge one.
    """
    encoded = quote(filename, safe="")
    return f"{'inline' if inline else 'attachment'}; filename=\"{_ascii_fallback(filename)}\"; filename*=UTF-8''{encoded}"


def _ascii_fallback(filename: str) -> str:
    """A filename old clients can parse: ASCII, no quotes, never empty."""
    cleaned = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    return cleaned or "download"
