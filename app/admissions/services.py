"""Applications from gahk.dk's outbox, and the applicant's auto-reply.

gahk.dk's optagelse forms store a submission in the webhost's own database (its outbox) until
cms.tasks.sync_gahk_web pulls it here. That data is UNTRUSTED input — the webhost is less trusted
than intern, and its database is reachable over the network — so every item is checked as if it
had been posted to intern: its shape here, its fields with the same forms the old intern views used
(admissions.forms). Only then is a row stored.

What happens to an item decides whether gahk.dk may delete it (the ack):

  * stored, or stored before (same `external_id`) → acknowledged;
  * fails the form rules, or is malformed → acknowledged and dropped, with the reason in the log:
    the same data will fail the same way on every pull, so keeping it would only repeat the error;
  * a kind intern does not know (a newer gahk.dk?) → NOT acknowledged, so it waits on the webhost
    until an intern that knows it pulls it;
  * an id that is not a UUID → cannot be acknowledged at all (gahk.dk only takes UUIDs); logged.

The log never carries the applicant's personal data, only the item's id and the failing field names.
"""

import functools
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .forms import FremlejeForm, RundvisningForm
from .models import Application

logger = logging.getLogger(__name__)

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FORMS: dict[str, type[RundvisningForm] | type[FremlejeForm]] = {
    Application.Type.TOUR: RundvisningForm,
    Application.Type.SUBLET: FremlejeForm,
}
# No field of a real application comes near this; anything longer is not one.
MAX_FIELD_LENGTH = 10_000
# A submission time this far ahead of intern's clock is the webhost's clock being wrong; it is
# stored as "now" rather than sorting at the top of the list for weeks.
CLOCK_SKEW = timedelta(hours=1)


def send_auto_reply(app: Application) -> None:
    """Send the applicant a "we got it" auto-reply. Best-effort; a mail failure must not lose the
    stored application. The committee is no longer emailed per application (F-011) — they review
    the list view. Not sent with fail_silently: SMTP errors — e.g. one.com refusing a
    DEFAULT_FROM_EMAIL the SMTP account is not an alias of — must reach the log instead of vanishing.
    """
    try:
        send_mail(
            "GAHK – vi har modtaget din henvendelse",
            f"Kære {app.full_name}\n\nTak for din henvendelse. Vi vender tilbage.\n\nMvh. Indstillingen",
            settings.DEFAULT_FROM_EMAIL,
            [app.email],
        )
    except Exception:
        logger.exception("Failed sending the auto-reply for application %s", app.pk)


@dataclass
class IngestResult:
    created: list[Application] = field(default_factory=list)
    ack_ids: list[str] = field(default_factory=list)  # what gahk.dk may delete
    rejected: int = 0  # dropped as invalid (and acknowledged)


class _Drop(ValueError):
    """The item can never become valid: acknowledge it and log why."""


class _Keep(ValueError):
    """The item may become valid later (an unknown kind): leave it on gahk.dk."""


def _submitted_at(value: object) -> datetime:
    moment = parse_datetime(value) if isinstance(value, str) else None
    if moment is None or timezone.is_naive(moment):
        raise _Drop("submitted_at is not an ISO timestamp with a UTC offset")
    now = timezone.now()
    return now if moment > now + CLOCK_SKEW else moment


def _form_data(form_class: type[RundvisningForm] | type[FremlejeForm], fields: object) -> dict[str, str]:
    if not isinstance(fields, dict):
        raise _Drop("fields is not an object")
    data: dict[str, str] = {}
    for name in form_class.base_fields:
        value = fields.get(name, "")
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise _Drop(f"{name} is not text")
        if len(value) > MAX_FIELD_LENGTH:
            raise _Drop(f"{name} is too long")
        data[name] = value
    return data


def _ingest_one(item: dict[str, Any], external_id: str) -> Application | None:
    """Store one outbox item; None when it was stored before. Raises _Drop / _Keep."""
    kind = item.get("kind")
    form_class = FORMS.get(kind) if isinstance(kind, str) else None
    if form_class is None:
        raise _Keep(f"unknown kind {kind!r}")
    submitted_at = _submitted_at(item.get("submitted_at"))
    form = form_class(data=_form_data(form_class, item.get("fields")))
    if not form.is_valid():
        raise _Drop(f"invalid fields: {', '.join(sorted(form.errors))}")
    if Application.objects.filter(external_id=external_id).exists():
        return None
    application = form.save(commit=False)
    application.type = kind
    application.submitted_at = submitted_at
    application.external_id = uuid.UUID(external_id)
    try:
        with transaction.atomic():
            application.save()
    except IntegrityError:
        # The same item, stored by a sync running alongside this one.
        return None
    return application


def ingest_outbox_applications(items: list[Any]) -> IngestResult:
    """Store the applications in gahk.dk's outbox (GET /api/outbox "applications"). Idempotent.

    Returns what was created and which ids to acknowledge. Each new applicant's auto-reply is queued
    once the row has been committed.
    """
    from .tasks import send_application_auto_reply

    result = IngestResult()
    for item in items:
        raw_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(item, dict) or not isinstance(raw_id, str) or not UUID_RE.match(raw_id):
            logger.error("gahk.dk outbox: an application without a valid id was skipped (%r)", raw_id)
            continue
        try:
            application = _ingest_one(item, raw_id)
        except _Keep as exc:
            logger.warning("gahk.dk outbox: application %s left on gahk.dk: %s", raw_id, exc)
            continue
        except _Drop as exc:
            logger.error("gahk.dk outbox: application %s dropped: %s", raw_id, exc)
            result.rejected += 1
            result.ack_ids.append(raw_id)
            continue
        result.ack_ids.append(raw_id)
        if application is not None:
            result.created.append(application)
            transaction.on_commit(functools.partial(send_application_auto_reply.delay, application.pk))
    if result.created:
        logger.info("gahk.dk outbox: %s new application(s) stored", len(result.created))
    return result
