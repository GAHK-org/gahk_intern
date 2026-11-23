from django.apps import AppConfig


class OelkaelderConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "oelkaelder"
    # Storage names this app owns; only its code writes under them (spec/architecture.md).
    storage_prefixes = ("oel/",)
