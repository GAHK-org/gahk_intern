"""Front-page visit counter (F-002/F-011/F-013).

Counts visits to the front page **only**. The visitor IP is stored as an HMAC-SHA256 hash (keyed by
VISIT_COUNTER_HMAC_KEY) — never raw — and counts are kept indefinitely. A 30-minute dedup window per
hashed IP mirrors the legacy `counter()` behaviour. Failures never break the page.
"""

import datetime
import hashlib
import hmac
import logging
from collections.abc import Callable

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from core.net import client_ip

from .models import DailyVisitCount, VisitTally

logger = logging.getLogger(__name__)
DEDUP_WINDOW = datetime.timedelta(minutes=30)


class FrontPageVisitCounterMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if request.method == "GET" and request.path == "/" and getattr(response, "status_code", 0) == 200:
            try:
                self._count(request)
            except Exception:
                # a counter hiccup must never break the front page
                logger.warning("front-page visit counter failed", exc_info=True)
        return response

    def _count(self, request: HttpRequest) -> None:
        # The last X-Forwarded-For hop, not the first: the first is whatever the client claimed, so
        # one visitor could rotate it and count as many (core.net).
        ip = client_ip(request)
        if not ip:
            return
        ip_hash = hmac.new(settings.VISIT_COUNTER_HMAC_KEY.encode(), ip.encode(), hashlib.sha256).hexdigest()
        now = timezone.now()
        with transaction.atomic():
            tally, created = VisitTally.objects.select_for_update().get_or_create(
                ip_hash=ip_hash,
                defaults={"count": 1, "first_seen": now, "last_seen": now},
            )
            counted = created
            if not created and now - tally.last_seen >= DEDUP_WINDOW:
                VisitTally.objects.filter(pk=tally.pk).update(count=F("count") + 1, last_seen=now)
                counted = True
            if counted:
                today = timezone.localdate()
                DailyVisitCount.objects.get_or_create(date=today, defaults={"count": 0})
                DailyVisitCount.objects.filter(date=today).update(count=F("count") + 1)
