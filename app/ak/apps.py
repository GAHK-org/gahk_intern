from django.apps import AppConfig


class AkConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ak"

    def ready(self) -> None:
        from . import handlers  # noqa: F401 — registers the domain-event subscriptions
