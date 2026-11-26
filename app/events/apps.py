from django.apps import AppConfig


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "events"
    verbose_name = "Begivenheder"
    # Storage names this app owns; only its code writes under them (spec/architecture.md).
    storage_prefixes = ("begivenheder/",)

    def ready(self) -> None:
        from . import live

        live.register()
