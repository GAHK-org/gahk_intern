import hashlib
from typing import Any

from django import template
from django.urls import reverse

from gahkflix import access, services
from gahkflix.models import Video, VideoFrame, key_of

register = template.Library()


def _version(key: str) -> str:
    """The key, shortened: a picture's URL changes exactly when the picture does."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]


@register.simple_tag
def picture_url(video: Video | None, kind: str = "thumbnail") -> str:
    if video is None:
        return ""
    field = getattr(video, kind, None)
    if not field:
        return ""
    return f"{reverse('gahkflix:picture', args=[video.pk, kind])}?v={_version(key_of(field))}"


@register.simple_tag
def frame_url(frame: VideoFrame) -> str:
    return f"{reverse('gahkflix:frame', args=[frame.video_id, frame.pk])}?v={_version(key_of(frame.image))}"


@register.filter
def duration(seconds: float | None) -> str:
    """ "1 t 23 min", "4 min", "45 sek" — how long a film is, the way a card says it."""
    if not seconds:
        return ""
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes = round(rest / 60)
    if hours:
        return f"{hours} t {minutes} min" if minutes else f"{hours} t"
    if total >= 60:
        return f"{max(1, minutes)} min"
    return f"{total} sek"


@register.filter
def get_item(mapping: dict[Any, Any] | None, key: object) -> Any:  # noqa: ANN401
    return (mapping or {}).get(key)


@register.filter
def percent(fraction: float | None) -> int:
    return round((fraction or 0) * 100)


@register.inclusion_tag("gahkflix/_credits_strip.html", takes_context=True)
def gahkflix_credits(context: dict[str, Any], resident: object) -> dict[str, Any]:
    """The "Medvirker i" strip on a resident's profile, for readers who can open GAHKFlix."""
    request = context.get("request")
    if request is None or not access.request_allowed(request):
        return {"videos": []}
    credits = list(services.credits_of(resident).order_by(*services.sort_date_ordering())[:8])  # type: ignore[arg-type]
    return {"videos": credits, "resident": resident}
