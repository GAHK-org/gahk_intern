from django.apps import AppConfig


class KoekkenConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "koekken"
    verbose_name = "Køkkenvagter"

    def ready(self) -> None:
        from . import signals  # noqa: F401  (connects the receivers)
