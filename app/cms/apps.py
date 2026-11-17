from django.apps import AppConfig


class CmsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cms"
    verbose_name = "Hjemmeside"

    def ready(self) -> None:
        from core.media import register_public_copy

        from .snapshot import published_image_url

        # The library was anonymous at /media/cms/ while this app rendered gahk.dk, and gahk.dk
        # forwards /media/ here: an old link to a published picture goes to gahk.dk's copy.
        register_public_copy("cms/", published_image_url)
