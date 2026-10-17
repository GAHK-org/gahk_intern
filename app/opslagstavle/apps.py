from django.apps import AppConfig


class OpslagstavleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "opslagstavle"
    verbose_name = "Opslagstavlen"

    def ready(self) -> None:
        from . import handlers  # noqa: F401 — registers this app's hooks into other domains
