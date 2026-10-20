from django.apps import AppConfig


class OpslagstavleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "opslagstavle"
    verbose_name = "Opslagstavlen"

    def ready(self) -> None:
        from core.media import register_cacheable

        from . import live
        from .models import comment_image_is_live, notice_image_is_live

        live.register()

        # Photos on recent and pinned opslag come from the server's disk, older ones from the bucket.
        register_cacheable("opslag/", notice_image_is_live)
        register_cacheable("opslag/kommentarer/", comment_image_is_live)
