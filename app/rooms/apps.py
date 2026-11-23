from django.apps import AppConfig


class RoomsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "rooms"
    # Storage names this app owns; only its code writes under them (spec/architecture.md).
    storage_prefixes = ("roomimages/",)
