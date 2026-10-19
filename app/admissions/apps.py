from django.apps import AppConfig
from django.core.checks import register


class AdmissionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "admissions"

    def ready(self) -> None:
        from .checks import check_turnstile_keys

        register(check_turnstile_keys)
