"""Background jobs owned by residents. Currently only the admin-grant notification."""

import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

from .admin_access import approvers
from .models import AdminAccessGrant

logger = logging.getLogger(__name__)


@shared_task
def notify_admin_access_request(grant_id: int) -> int:
    """Mail every approver that a new administrator is waiting to be accepted.

    Best-effort by design: the siteadmin front page lists pending grants too, so a bounced or
    unconfigured SMTP delays the decision rather than hiding it.
    """
    grant = AdminAccessGrant.objects.filter(pk=grant_id).select_related("resident").first()
    if grant is None or grant.status != AdminAccessGrant.Status.PENDING:
        return 0
    recipients = sorted(approvers().exclude(pk=grant.resident_id).values_list("email", flat=True))
    if not recipients:
        logger.warning("No approver can accept admin access for resident %s", grant.resident_id)
        return 0
    url = settings.SITE_URL.rstrip("/") + reverse("siteadmin:admin_access")
    return send_mail(
        "Ny administrator afventer godkendelse",
        f"{grant.resident.full_name} har faaet rollen administrator og afventer godkendelse "
        f"af et andet medlem af Netvaerksgruppen.\n\n"
        f"Godkend eller afvis her: {url}\n\n"
        f"Indtil da har {grant.resident.first_name} ingen adgang til Django admin.",
        settings.DEFAULT_FROM_EMAIL,
        recipients,
    )
