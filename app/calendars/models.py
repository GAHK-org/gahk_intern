"""A resident's personal calendar subscription: one .ics address, and what goes into it.

Its own app rather than part of begivenheder, because the calendar is the RESIDENT's, not the
events': it carries birthdays today, and kitchen and cleaning shifts once those exist. Each source
stays in its own app and this one only assembles them (see feed.py).

The model came from events.CalendarFeedToken (moved in calendars 0001/0002, same rows, same
tokens), so every address handed out before the move keeps working.
"""

import secrets
from typing import Any

from django.conf import settings
from django.db import models
from django.db.models import F

from audit import trail
from core.clock import current_datetime


def new_token() -> str:
    """A fresh calendar-feed secret.

    A NAMED module function rather than a lambda or an inline default because migrations serialise
    the callable by import path. `secrets`, never `random`: this is a bearer credential, and bandit
    is right to flag the other one (B311).
    """
    return secrets.token_urlsafe(32)


class EventChoice(models.TextChoices):
    ALL = "all", "Alle begivenheder du kan se"
    ACCEPTED = "accepted", "Kun dem du har sagt ja til"


class CalendarSubscription(models.Model):
    """One resident's secret .ics address, their choices for it, and whether it is being fetched.

    Its own model rather than columns on Resident, deliberately. Resident is the auth principal —
    loaded by every request, touched by every app, dumped by the alumneliste export. A secret living
    there is one that a stray .values(), a serializer or an admin changelist can leak from code that
    has no idea it is handling a credential. Here, the only code that can render it is code that
    already knows what it is. Second reason: revocation should be a row you delete, not a column you
    remember to null.

    The token IS the credential — a calendar client cannot log in, so the feed view carries no auth
    decorator. Everything that follows from that (path segment not query string, 404 on unknown,
    no-store, rotation) lives in views.py beside the endpoint.
    """

    resident = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_subscription"
    )
    token = models.CharField(max_length=64, unique=True, editable=False, default=new_token)
    created_at = models.DateTimeField(auto_now_add=True)
    rotated_at = models.DateTimeField(null=True, blank=True)

    # Everything on by default. Kitchen and cleaning shifts get their own switches once they exist.
    events = models.CharField(max_length=16, choices=EventChoice.choices, default=EventChoice.ALL)
    include_birthdays = models.BooleanField(default=True)

    # Use of THIS address: both reset when it is rotated, since a new address has not been fetched.
    # A calendar app fails silently, so "when did it last fetch" is the only sign a subscription
    # works, and the settings page warns when it has gone quiet (views.STALE_AFTER).
    last_used_at = models.DateTimeField(null=True, blank=True)
    fetch_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Kalenderabonnement"
        verbose_name_plural = "Kalenderabonnementer"

    def __str__(self) -> str:
        return f"Kalenderabonnement for {self.resident}"

    @classmethod
    def for_resident(cls, resident: Any) -> "CalendarSubscription":  # noqa: ANN401
        """This resident's subscription, minting one on first use.

        Lazily, never backfilled by a migration: a RunPython minting sixty secrets would create
        sixty things to leak for people who may never open the feature, and — worse — would bake a
        non-deterministic data step into the migration history.
        """
        # An event only when one is made: opening the page again writes nothing.
        with trail.event("calendars.subscription.created", only_if_changed=True) as event:
            subscription, created = cls.objects.get_or_create(resident=resident)
            if created:
                event.involve(subscription, "subject")
        return subscription

    def rotate(self) -> str:
        """Mint a new secret and invalidate the old one. Returns the new token."""
        self.token = new_token()
        self.rotated_at = current_datetime()
        self.last_used_at = None
        self.fetch_count = 0
        self.save(update_fields=["token", "rotated_at", "last_used_at", "fetch_count"])
        return self.token

    def record_fetch(self) -> None:
        """Count one fetch. One UPDATE per request: a few residents' apps polling hourly is nothing."""
        now = current_datetime()
        type(self).objects.filter(pk=self.pk).update(last_used_at=now, fetch_count=F("fetch_count") + 1)
