from django.apps import AppConfig


class GahkflixConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gahkflix"
    verbose_name = "GAHKFlix"
    # Storage names this app owns; only its code writes under them (spec/architecture.md).
    storage_prefixes = ("gahkflix/",)
