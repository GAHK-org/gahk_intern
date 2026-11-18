from celery import shared_task
from django.core.management import call_command


@shared_task
def sync_mailboxes() -> None:
    """Refresh every active mailbox's header cache from one.com (beat: every 2 minutes)."""
    call_command("sync_mailboxes")


@shared_task
def apply_time_rules() -> None:
    """Move the sager that have gone quiet, by each mailbox's time rules (beat: hourly)."""
    call_command("apply_mail_time_rules")


@shared_task
def purge_drafts() -> None:
    """Delete the kladder nobody has touched for 30 days (beat: daily)."""
    call_command("purge_mail_drafts")
