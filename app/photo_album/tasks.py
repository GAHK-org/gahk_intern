from celery import shared_task
from django.core.management import call_command

from .models import DerivativeState, Media
from .services import build_derivatives, pending_derivatives


@shared_task
def build_media_derivatives(media_id: int) -> bool:
    """Build derivatives for one committed upload, if it still needs them."""
    try:
        media = Media.objects.get(pk=media_id, derivative_state=DerivativeState.PENDING)
    except Media.DoesNotExist:
        return False
    return build_derivatives(media)


@shared_task
def process_pending_media(limit: int = 10) -> int:
    """Backstop for messages lost while the broker is unavailable."""
    for media in pending_derivatives()[:limit]:
        build_media_derivatives.delay(media.pk)
    return min(pending_derivatives().count(), limit)


@shared_task
def purge_expired_media() -> None:
    """Permanently remove photo-album media past its retention period."""
    call_command("purge_photo_album")
