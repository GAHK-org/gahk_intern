from django.apps import AppConfig


class RegnskabConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "regnskab"
    # Storage names this app owns; only its code writes under them (spec/architecture.md).
    storage_prefixes = ("alumneregning/",)
