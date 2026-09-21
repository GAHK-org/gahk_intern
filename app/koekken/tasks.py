from celery import shared_task
from django.core.management import call_command


@shared_task
def generate_koekkenvagter() -> None:
    """Generate this month's Vagt rows (idempotent)."""
    call_command("generate_koekkenvagter")


@shared_task
def post_koekken_obligation() -> None:
    """Post this month's FORPLIGTELSE ledger entries (idempotent)."""
    call_command("post_koekken_obligation")
