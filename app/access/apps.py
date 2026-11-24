from django.apps import AppConfig, apps
from django.db.models.signals import post_migrate


class AccessConfig(AppConfig):
    """Access levels: which parts of the intranet exist for whom (access.policy), and whose level is
    what (access.levels). No models of its own: the level is stored on the account."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "access"

    def ready(self) -> None:
        from . import signals

        # After every migrate, so a deploy works every level out again: the first one fills the new
        # column, and any later one picks up a change to the rule itself without waiting for the night.
        # Sent once per app with models; residents' is the one that holds the fields.
        post_migrate.connect(
            signals.levels_after_migrate, sender=apps.get_app_config("residents"), dispatch_uid="access"
        )
