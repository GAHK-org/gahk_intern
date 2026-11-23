"""The access levels' nightly run (CELERY_BEAT_SCHEDULE "refresh-access-levels")."""

from celery import shared_task

from . import levels


@shared_task
def refresh_access_levels() -> str:
    """Just after midnight: time alone moves levels. A list made ahead takes effect on the 1st, the
    moved-out limits pass with the months, and an override ends after its last day. Idempotent."""
    forgotten = levels.forget_expired_overrides()
    changed = levels.refresh_access_levels()
    return f"{len(changed)} level(s) changed, {forgotten} expired override(s) cleared"
