from django.apps import AppConfig
from django.core.checks import register


class CmsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cms"

    def ready(self) -> None:
        # A page's address is validated against a hand-maintained copy of the URLconf's fixed
        # prefixes (cms.paths.RESERVED_TOP_SEGMENTS); this fails the build when the two drift.
        from .checks import check_reserved_top_segments

        register(check_reserved_top_segments)
