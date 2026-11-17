"""Turning a raw message into something safe to show: headers, a text or HTML body, attachments.

An HTML mail is the most hostile input this site renders — written by anyone on the internet, aimed
at whoever opens a group inbox. Three independent layers, so no single mistake is enough:

  1. nh3 strips scripts, event handlers, forms, iframes and unsafe URL schemes (clean_html).
  2. The result is shown in an <iframe sandbox> via `srcdoc` (templates/postkasser/message.html):
     no scripts, an opaque origin, so even a sanitizer miss cannot reach the session or the page.
  3. The srcdoc carries its own Content-Security-Policy that forbids every network load except
     images, and those only once the reader clicks "Vis billeder". A remote image in a mail is a
     tracking pixel as often as not — it tells the sender who opened it and when.
"""

import email
import email.policy
import html as htmllib
import re
from dataclasses import dataclass, field
from email.errors import HeaderParseError
from email.message import EmailMessage, Message, MIMEPart
from email.utils import getaddresses, parsedate_to_datetime

import nh3
from django.utils.html import escape

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "center", "code", "col", "colgroup", "dd", "div",
    "dl", "dt", "em", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p",
    "pre", "s", "small", "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "u", "ul",
}  # fmt: skip
ALLOWED_ATTRS = {
    "*": {"style", "align", "dir", "title", "width", "height", "bgcolor", "color"},
    "a": {"href"},
    "img": {"src", "alt"},
    "td": {"colspan", "rowspan", "valign"},
    "th": {"colspan", "rowspan", "valign"},
    "table": {"border", "cellpadding", "cellspacing"},
    "font": {"face", "size"},
}


_SAFE_HREF = ("http://", "https://", "mailto:")
_REMOTE_IMG = re.compile(r"""<img\b[^>]*?\bsrc="https?:""", re.IGNORECASE)


def _attribute_filter(tag: str, attr: str, value: str) -> str | None:
    """Only absolute links survive. `cid:` images point into the message's own parts, which the
    sandboxed frame cannot fetch (they are listed as attachments instead); relative and
    protocol-relative URLs would resolve against whatever the frame's base is, so they go too.
    `data:` is allowed for image sources only."""
    if (tag, attr) not in {("a", "href"), ("img", "src")}:
        return value
    lowered = value.strip().lower()
    if lowered.startswith(_SAFE_HREF):
        return value
    if tag == "img" and lowered.startswith("data:"):
        return value
    return None


_DROP_CONTENT = {"title", "head", "script", "style"}


def clean_html(html: str) -> str:
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        clean_content_tags=_DROP_CONTENT,
        attributes=ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto", "data"},
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
        attribute_filter=_attribute_filter,
        strip_comments=True,
    )


_BLOCK_END = re.compile(r"</(?:p|div|tr|li|h[1-6])\s*>|<br\s*/?>", re.IGNORECASE)


def html_to_text(html: str) -> str:
    """A flattened HTML body for quoting in a reply: block ends become line breaks."""
    text = nh3.clean(_BLOCK_END.sub("\n", html), tags=set(), clean_content_tags=_DROP_CONTENT)
    return re.sub(r"\n{3,}", "\n\n", htmllib.unescape(text)).strip()


def frame_document(body_html: str, *, allow_images: bool) -> str:
    """The full document for the iframe's srcdoc. `img-src` is the only network load ever allowed,
    and only on request; style-src 'unsafe-inline' is fine because nothing here can run script."""
    img_src = "data: https: http:" if allow_images else "data:"
    csp = f"default-src 'none'; style-src 'unsafe-inline'; img-src {img_src}"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta http-equiv='Content-Security-Policy' content=\"{csp}\">"
        "<base target='_blank'>"
        "<style>body{font-family:system-ui,sans-serif;font-size:15px;line-height:1.5;margin:0;"
        "padding:12px;color:#1a1a1a;background:#fff;word-wrap:break-word}img{max-width:100%;height:auto}"
        "</style></head><body>"
        f"{body_html}</body></html>"
    )


@dataclass
class Attachment:
    index: int
    filename: str
    content_type: str
    size: int


@dataclass
class RenderedMessage:
    subject: str
    from_header: str
    to: str
    cc: str
    reply_to: str
    date: str
    message_id: str
    references: str
    text: str  # plain-text body, or the HTML flattened, for quoting in a reply
    html: str | None  # sanitized HTML body, or None for a plain-text mail
    has_remote_images: bool
    attachments: list[Attachment] = field(default_factory=list)


def parse(raw: bytes) -> EmailMessage:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)  # policy.default always builds EmailMessage  # noqa: S101
    return msg


def _content(part: MIMEPart) -> str:
    try:
        content = part.get_content()
    except (LookupError, ValueError):  # unknown charset / broken transfer encoding
        payload = part.get_payload(decode=True)
        content = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
    return content if isinstance(content, str) else ""


def _header(msg: EmailMessage, name: str) -> str:
    """A header as one line of text. policy.default parses lazily and raises on hostile input (a
    CR/LF smuggled in through an encoded word), so fall back to the raw value; either way CR/LF are
    stripped, because the result may be put in another mail's headers."""
    try:
        value = str(msg.get(name, "") or "")
    except (ValueError, HeaderParseError, TypeError, AttributeError, IndexError, KeyError):
        value = next((str(v) for k, v in msg.raw_items() if k.lower() == name.lower()), "")
    return value.replace("\r", "").replace("\n", "")


def part_bytes(part: EmailMessage) -> bytes:
    """What a download of this attachment contains. A forwarded mail (message/rfc822) has no
    transfer-decoded payload of its own; its content is the nested message."""
    if part.is_multipart():  # message/rfc822: the payload is the nested message itself
        inner = part.get_payload(0)
        return inner.as_bytes() if isinstance(inner, Message) else b""
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def attachments_of(msg: EmailMessage) -> list[tuple[Attachment, EmailMessage]]:
    found = []
    for index, part in enumerate(msg.iter_attachments()):
        found.append(
            (
                Attachment(
                    index=index,
                    filename=part.get_filename() or f"vedhæftning-{index + 1}",
                    content_type=part.get_content_type(),
                    size=len(part_bytes(part)),
                ),
                part,
            )
        )
    return found


def render(raw: bytes) -> RenderedMessage:
    msg = parse(raw)
    html_part = msg.get_body(preferencelist=("html",))
    text_part = msg.get_body(preferencelist=("plain",))
    html = clean_html(_content(html_part)) if html_part is not None else None
    if text_part is not None:
        text = _content(text_part)
    elif html is not None:
        text = html_to_text(html)
    else:
        text = ""
    return RenderedMessage(
        subject=_header(msg, "Subject"),
        from_header=_header(msg, "From"),
        to=_header(msg, "To"),
        cc=_header(msg, "Cc"),
        reply_to=_header(msg, "Reply-To"),
        date=_header(msg, "Date"),
        message_id=_header(msg, "Message-ID"),
        references=_header(msg, "References"),
        text=text,
        html=html,
        has_remote_images=bool(html and _REMOTE_IMG.search(html)),
        attachments=[a for a, _part in attachments_of(msg)],
    )


def text_as_html(text: str) -> str:
    """A plain-text body inside the same frame as HTML ones, so both render the same way."""
    return f"<pre style='white-space:pre-wrap;font:inherit;margin:0'>{escape(text)}</pre>"


def header_fields(raw_headers: bytes) -> dict[str, object]:
    """The cached columns of a MailMessage, from a header-only fetch (services.sync)."""
    msg = parse(raw_headers)
    from_pairs = getaddresses([_header(msg, "From")])
    from_name, from_address = from_pairs[0] if from_pairs else ("", "")
    date_header = _header(msg, "Date")
    try:
        date = parsedate_to_datetime(date_header) if date_header else None
    except (TypeError, ValueError):
        date = None
    if date is not None and date.tzinfo is None:
        date = None  # a zoneless Date header is a guess either way; sort it last rather than lie
    content_type = msg.get_content_type()
    return {
        "message_id": _header(msg, "Message-ID")[:998],
        # Kept to thread the mail (services.sync); CR/LF are stripped by _header like the rest.
        "in_reply_to": _header(msg, "In-Reply-To"),
        "references": _header(msg, "References"),
        "subject": _header(msg, "Subject")[:998],
        "from_name": from_name[:255],
        "from_address": from_address[:320],
        "to": _header(msg, "To"),
        "cc": _header(msg, "Cc"),
        "date": date,
        # From the top-level type only — BODYSTRUCTURE would be exact, but this is a list-view hint
        # and multipart/mixed is what nearly every client sends when, and only when, a file is attached.
        "has_attachments": content_type == "multipart/mixed",
    }
