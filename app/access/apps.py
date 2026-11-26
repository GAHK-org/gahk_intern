from django.apps import AppConfig
from django.db.models.signals import post_migrate


class AccessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "access"

    def ready(self) -> None:
        from . import signals  # noqa: F401

        post_migrate.connect(_after_migrate, sender=self)


def _after_migrate(sender: AppConfig, **kwargs: object) -> None:
    """Every deploy: the permissions exist, each role's group holds what the table gives it, and every
    account is in its roles' groups. Picks up a change to the table itself."""
    from django.contrib.auth.management import create_permissions

    from .roles import sync, sync_group_permissions

    create_permissions(sender, verbosity=0)
    sync_group_permissions()
    sync()
