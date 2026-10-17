"""Background jobs owned by residents: the admin-grant notification and profile thumbnails."""

import logging
from pathlib import PurePath

from celery import shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.urls import reverse

from core.images import square_jpeg

from .admin_access import approvers
from .models import PROFILE_THUMBNAIL_SIZE, AdminAccessGrant, Resident

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


@shared_task
def make_profile_thumbnail(resident_id: int, picture: str) -> bool:
    """Make the thumbnail of `picture`, if it is still the resident's picture and has none (#177).

    Queued by Resident.queue_profile_thumbnail. Anything else is a job that is no longer wanted: the
    resident is gone, has changed picture since (that picture has its own job), or a duplicate job
    got there first. The final update repeats the same conditions, so a job racing a change of
    picture or another job cannot store a thumbnail of the wrong picture; the loser deletes its file.

    Reads the picture back from storage rather than from the upload, which is long gone by now. A
    picture Pillow cannot read, or that is missing from the bucket, gets no thumbnail, and its avatar
    keeps drawing the original. Returns whether a thumbnail was stored.
    """
    pending = Resident.objects.filter(pk=resident_id, profile_picture=picture, profile_thumbnail="")
    resident = pending.first()
    if resident is None:
        return False
    try:
        with resident.profile_picture.storage.open(picture, "rb") as source:
            data = square_jpeg(source, PROFILE_THUMBNAIL_SIZE)
    except OSError:
        logger.warning("profile thumbnail: cannot read %s", picture, exc_info=True)
        return False
    if data is None:
        logger.info("profile thumbnail: %s is not an image Pillow can read", picture)
        return False
    thumbnail = resident.profile_thumbnail
    thumbnail.save(f"{PurePath(picture).stem}.jpg", ContentFile(data), save=False)
    name = thumbnail.name or ""
    if pending.update(profile_thumbnail=name):
        return True
    thumbnail.storage.delete(name)
    return False
