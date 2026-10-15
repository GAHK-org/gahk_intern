"""Visit/admission statistics + the front-page visit counter.

Decisions: the visit counter applies to the **front page only**; visitor IPs are stored as an
**HMAC-SHA256** hash (keyed by VISIT_COUNTER_HMAC_KEY) rather than raw, and counts are kept
indefinitely. `gahk_counterdato` (per-date aggregate) migrates to DailyVisitCount for chart history.
"""

from django.db import models
from django.utils import timezone


class DailyVisitCount(models.Model):  # gahk_counterdato
    date = models.DateField(unique=True)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"{self.date}: {self.count}"


class VisitTally(models.Model):  # gahk_counter, but IP is HMAC-hashed (no raw IP stored)
    ip_hash = models.CharField(max_length=64, unique=True)  # hex HMAC-SHA256 of the visitor IP
    count = models.PositiveIntegerField(default=0)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.ip_hash[:8]}… ×{self.count}"
