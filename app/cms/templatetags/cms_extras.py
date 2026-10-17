import re

from django import template
from django.conf import settings
from django.templatetags.static import static

register = template.Library()


@register.filter
def legacy_img(path: str) -> str:
    """Map a stored legacy image path (/public/image/...) to its copied static asset URL.

    Only legacy paths are rewritten. A background picked from the image library is stored as its
    /media/cms/… URL (cms.CmsImage), and an editor may paste an absolute http(s) URL; prefixing
    `legacy/` onto those pointed the hero at /static/legacy/media/…, which does not exist.
    """
    if not path:
        return ""
    if path.startswith((settings.MEDIA_URL, "http://", "https://", "//")):
        return path
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
