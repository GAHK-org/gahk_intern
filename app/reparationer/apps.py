from django.apps import AppConfig


class ReparationerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "reparationer"
    verbose_name = "Reparationer"

    def ready(self) -> None:
        from . import live

        live.register()
