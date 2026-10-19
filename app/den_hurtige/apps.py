from django.apps import AppConfig
from django.core.checks import register


class DenHurtigeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "den_hurtige"
    verbose_name = "Den Hurtige"

    def ready(self) -> None:
        # Only the channel registry is validated here. The VAPID key pair moved to core.apps with
        # the rest of the push stack — the keys are shared with opslagstavlen, so they are not this
        # feature's to check any more.
        from core.media import register_cacheable

        from . import live
        from .checks import check_channels
        from .models import comment_image_is_live, post_image_is_live

        register(check_channels)
        live.register()
        # Photos on live messages come from the server's disk, archived ones from the bucket.
        register_cacheable("quick_posts/", post_image_is_live)
        register_cacheable("quick_comments/", comment_image_is_live)
