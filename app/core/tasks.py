from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

from residents.models import Resident, active_period


@shared_task
def send_admin_dummy_notification() -> int:
    """Send the temporary twice-daily worker notification to current administrators."""
    year, month = active_period()
    recipients = set(
        Resident.objects.filter(
            is_active=True,
            role_assignments__role="administrator",
            role_assignments__year=year,
            role_assignments__month=month,
        ).values_list("email", flat=True)
    )
    recipients.update(
        Resident.objects.filter(is_active=True, is_superuser=True).values_list("email", flat=True)
    )
    if not recipients:
        return 0
    return send_mail(
        "Celery-testnotifikation",
        "Dette er den planlagte testnotifikation fra Celery-worker'en.",
        settings.DEFAULT_FROM_EMAIL,
        sorted(recipients),
    )
