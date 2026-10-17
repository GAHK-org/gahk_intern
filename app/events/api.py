"""Begivenheder's public interface — the only module other domains may import from `events`
(enforced by Tach; see docs/domain-boundaries.md).

Two directions, and they are built differently on purpose:

  * OTHER DOMAINS ASKING ABOUT EVENTS get DTOs (`EventSummary`), never an `Event`. Visibility is the
    one thing this app guards hardest (see access.py: a private event must be absent, not
    forbidden), and a model instance handed to another domain carries every field and relation
    with it.

  * EVENTS SHOWING OTHER DOMAINS' CONTENT ABOUT AN EVENT — "Omtalt på opslagstavlen" on the detail
    page — is inverted: a domain registers a `MentionProvider` here, from its own AppConfig.ready(),
    and the detail page renders whatever sections come back. `events` does not import the board;
    the board depends on `events`, and only on this module. That is what removed the
    events <-> opslagstavle import cycle.
"""

import datetime
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from django.http import HttpRequest
from django.urls import reverse

from core.clock import current_datetime

from . import access
from .models import Event, Visibility

__all__ = [
    "EventSummary",
    "Mention",
    "MentionProvider",
    "MentionSection",
    "event_summaries",
    "linkable_events",
    "mentions_of",
    "register_mention_provider",
    "request_allowed",
]


@dataclass(frozen=True)
class EventSummary:
    id: int
    title: str
    starts_at: datetime.datetime
    is_open: bool  # visible to every resident ("åben"), as opposed to invite-only
    is_cancelled: bool
    url: str

    @property
    def choice_label(self) -> str:
        """How the event reads in a picker — what Event.__str__ gave the old ModelChoiceField."""
        return f"{self.title} ({self.starts_at:%d.%m.%Y})"


def _summary(event: Event) -> EventSummary:
    return EventSummary(
        id=event.pk,
        title=event.title,
        starts_at=event.starts_at,
        is_open=event.visibility == Visibility.AABENT,
        is_cancelled=event.cancelled_at is not None,
        url=reverse("events:detail", args=[event.pk]),
    )


def request_allowed(request: HttpRequest) -> bool:
    """Whether this reader may open begivenheder at all (the rollout gate). Another domain asks before
    linking here, because a link that 403s is worse than no link."""
    return access.request_allowed(request)


def linkable_events() -> list[EventSummary]:
    """Events another domain may point at from content the WHOLE HOUSE reads: upcoming, not
    cancelled, and open to every resident.

    The visibility filter is the load-bearing one — a chip naming a private party on a board sixty
    people read announces exactly what "kun inviterede" exists to hide. A caller using this as the
    choices of a form field also gets its validation: an id that is not in here cannot be posted.
    """
    qs = Event.objects.upcoming(current_datetime()).filter(
        visibility=Visibility.AABENT, cancelled_at__isnull=True
    )
    return [_summary(e) for e in qs]


def event_summaries(ids: Iterable[int | None]) -> dict[int, EventSummary]:
    """Summaries for the given ids, in one query. Missing ids (deleted events) are simply absent.

    NOT filtered on visibility: a caller showing one checks `is_open` itself, because whether an
    invite-only event may be shown depends on where — and on a board the whole house reads, it may
    not be (see opslagstavle's notice card).
    """
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return {e.pk: _summary(e) for e in Event.objects.filter(pk__in=wanted)}


# --- Extension point: other domains' content about an event ---------------------------------------


@dataclass(frozen=True)
class Mention:
    url: str
    excerpt_markdown: str  # rendered and truncated by the template, never sliced as source
    author_name: str
    created_at: datetime.datetime


@dataclass(frozen=True)
class MentionSection:
    heading: str
    mentions: list[Mention]


# Gets the reader (to apply THEIR gates — the provider's own feature may be closed to them) and the
# event id. Returns None for "nothing to show this reader".
MentionProvider = Callable[[HttpRequest, int], MentionSection | None]

_providers: list[MentionProvider] = []


def register_mention_provider(provider: MentionProvider) -> None:
    """Show `provider`'s section on every event's detail page. Call it from AppConfig.ready()."""
    if provider not in _providers:
        _providers.append(provider)


def mentions_of(request: HttpRequest, event_id: int) -> list[MentionSection]:
    """Every registered provider's non-empty section for this event and reader."""
    sections = (provider(request, event_id) for provider in _providers)
    return [s for s in sections if s is not None and s.mentions]
