from celery import shared_task

from . import services


@shared_task
def complete_finished_shifts() -> int:
    """Book points for every shift that has ended."""
    return services.complete_finished_shifts()


@shared_task
def extend_calendar() -> int:
    """Keep the standard shift calendar filled 3 months ahead."""
    return services.ensure_calendar()


@shared_task
def apply_monthly_charge() -> None:
    """Subtract the monthly køkkenkryds from every current resident."""
    services.ensure_monthly_charge()


@shared_task
def notify_unmanned_daily() -> None:
    services.notify_unmanned_daily()


@shared_task
def notify_unmanned_weekly() -> None:
    services.notify_unmanned_weekly()
