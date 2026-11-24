from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules


class AuditConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "audit"
    verbose_name = "Revisionsspor"

    def ready(self) -> None:
        # Every app names what it audits in its own auditing.py (registrations, exemptions, action
        # names); importing them connects the capture signals.
        autodiscover_modules("auditing")

        from . import jobs  # noqa: F401  (the worker-job context)
