# ruff: noqa: S310

import json
import logging
from urllib.request import Request, urlopen

from celery import shared_task
from django.conf import settings

from .models import DocumentEditingSession, DocumentSessionState
from .services import sign_jwt

logger = logging.getLogger(__name__)


@shared_task
def force_save_active_documents() -> int:
    """Ask ONLYOFFICE for status-6 snapshots without closing collaboration sessions."""
    url = f"{settings.ONLYOFFICE_INTERNAL_URL.rstrip('/')}/coauthoring/CommandService.ashx"
    triggered = 0
    for session in DocumentEditingSession.objects.filter(state=DocumentSessionState.ACTIVE).iterator():
        payload = {"c": "forcesave", "key": session.document_key, "userdata": f"periodic:{session.pk}"}
        request = Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {sign_jwt(payload)}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                result = json.loads(response.read())
            if result.get("error") == 0:
                triggered += 1
            else:
                logger.warning("ONLYOFFICE rejected force-save for session %s: %s", session.pk, result)
        except Exception:
            logger.exception("ONLYOFFICE force-save request failed for session %s", session.pk)
    return triggered
