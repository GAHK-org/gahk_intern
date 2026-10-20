from django.apps import AppConfig


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "events"
    verbose_name = "Begivenheder"

    def ready(self) -> None:
        from . import live

        live.register()
