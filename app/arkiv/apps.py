from django.apps import AppConfig


class ArkivConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "arkiv"
    verbose_name = "Arkiv"
    # Storage names this app owns; only its code writes under them (spec/architecture.md).
    storage_prefixes = ("arkiv/", "arkiv-thumb/", "arkiv-preview/", "arkiv-zip/")
