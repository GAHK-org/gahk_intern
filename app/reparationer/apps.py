from django.apps import AppConfig


class ReparationerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "reparationer"
    verbose_name = "Reparationer"
    # Storage names this app owns; only its code writes under them (spec/architecture.md).
    storage_prefixes = ("reparationer/",)

    def ready(self) -> None:
        from . import live

        live.register()
