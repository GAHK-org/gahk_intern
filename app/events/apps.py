from django.apps import AppConfig


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "events"
    verbose_name = "Begivenheder"

    def ready(self) -> None:
        from . import live

        live.register()

        from . import handlers  # noqa: F401 — registers this app's hooks into other domains
