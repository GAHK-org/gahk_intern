"""IMAP's "modified UTF-7" (RFC 3501 section 5.1.3) — how a mail server writes a folder name that is
not plain ASCII. A Danish mailbox has "Ølkælder" and "Udgående"; on the wire those are `&ANg-lk&AOY-lder`
and `Udg&AOU-ende`.

The rules, which differ from ordinary UTF-7 on purpose:
  * printable ASCII (0x20-0x7e) stands for itself, except `&`, which is written `&-`;
  * every other run of characters is UTF-16BE, base64-encoded with `,` in place of `/` and no
    padding, between `&` and `-`.

Folder names are stored on MailFolder.name exactly as the server knows them (that is what has to be
sent back in SELECT), and decoded only for display.
"""

import base64
import binascii
import re

_SHIFTED = re.compile(r"&([^-]*)-")


def _printable(char: str) -> bool:
    return " " <= char <= "~"


def _flush(run: list[str]) -> str:
    raw = "".join(run).encode("utf-16-be")
    return "&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-"


def encode(text: str) -> str:
    out: list[str] = []
    run: list[str] = []
    for char in text:
        if _printable(char):
            if run:
                out.append(_flush(run))
                run = []
            out.append("&-" if char == "&" else char)
        else:
            run.append(char)
    if run:
        out.append(_flush(run))
    return "".join(out)


def _decode_run(match: re.Match[str]) -> str:
    body = match.group(1)
    if not body:
        return "&"
    try:
        padded = body.replace(",", "/") + "=" * (-len(body) % 4)
        return base64.b64decode(padded, validate=True).decode("utf-16-be")
    except (binascii.Error, UnicodeDecodeError):
        # A name the server mangled is shown as it came rather than raising in the middle of a sync.
        return match.group(0)


def decode(text: str) -> str:
    return _SHIFTED.sub(_decode_run, text)
