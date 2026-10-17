import re

from django import template
from django.conf import settings
from django.templatetags.static import static
from django.utils.safestring import SafeString, mark_safe

from cms.sanitize import clean_inline_html, inline_text

register = template.Library()


@register.filter
def legacy_img(path: str) -> str:
    """Map a stored legacy image path (/public/image/...) to its copied static asset URL."""
    if not path:
        return ""
    rel = path[len("/public/") :] if path.startswith("/public/") else path.lstrip("/")
    return static("legacy/" + rel)


@register.filter
def body_media(html: str) -> str:
    """Rewrite legacy asset URLs inside CMS body HTML to the copied static location. Handles both the
    relative forms (`/public/…`, `public/…`) and the absolute legacy-host form
    (`http(s)://[www.]gahk.dk/public/…`) — rewriting the absolute one also removes mixed-content
    warnings on the HTTPS site (it was still pointing at `http://…`)."""
    if not html:
        return html
    prefix = settings.STATIC_URL.rstrip("/") + "/legacy/"
    attr = r'((?:src|href)\s*=\s*["\'])'
    html = re.sub(attr + r"https?://(?:www\.)?gahk\.dk/public/", r"\1" + prefix, html, flags=re.IGNORECASE)
    return re.sub(attr + r"/?public/", r"\1" + prefix, html, flags=re.IGNORECASE)


@register.filter
def heading_html(html: str) -> SafeString:
    """Render a CMS heading (page header, news title) with its inline formatting (<br>, <em>, &amp;)
    but nothing executable. Sanitised here, at render time, rather than trusting `|safe`: the admin
    forms clean on save too, but rows migrated from the legacy site never went through those forms."""
    return mark_safe(clean_inline_html(html) or "")  # noqa: S308  # nosec — clean_inline_html output only


@register.filter
def heading_text(html: str) -> str:
    """A CMS heading as plain text, for <title> and attribute values; autoescape still applies."""
    return inline_text(html)
