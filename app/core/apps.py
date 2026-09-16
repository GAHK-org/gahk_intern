from django.apps import AppConfig
from django.core.checks import register


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self) -> None:
        # VAPID validation lives here because the keys are shared by every feature that pushes; the
        # check IDs are core.E001-E006 (they were den_hurtige.E00x before push moved to core).
        from .checks import (
            check_media_storage_prefix,
            check_media_storage_url,
            check_media_url,
            check_vapid_public_key,
        )

        register(check_vapid_public_key)
        # MEDIA_URL is a prefix of content stored in the database (opslag Markdown bodies,
        # cms.Page.background_image), so changing it unlinks every historic image silently. These
        # two refuse to start rather than let that happen — see core/storage.py.
        register(check_media_url)
        register(check_media_storage_url)
        register(check_media_storage_prefix)
