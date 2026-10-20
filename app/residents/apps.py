from django.apps import AppConfig


class ResidentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "residents"

    def ready(self) -> None:
        from . import auth, signals  # noqa: F401  (failed-login log; RoleAssignment → is_staff sync)
