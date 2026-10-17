"""What opslagstavlen plugs into other domains. Imported once, from OpslagstavleConfig.ready().

Today that is one extension point: the posts about an event, shown on the event's own page. It is
registered here rather than queried from `events` so that the dependency points one way only —
opslagstavle -> events.api — and begivenheder never imports the board (see events.api).
"""

from django.http import HttpRequest
from django.urls import reverse

from events.api import Mention, MentionSection, register_mention_provider

from .access import request_allowed
from .models import Notice

MAX_MENTIONS = 5


def notices_about_event(request: HttpRequest, event_id: int) -> MentionSection | None:
    """The newest posts linked to this event, for a reader who can actually open the board.

    Gated on the READER: opslagstavlen has its own rollout gate, and pointing somebody at a board
    that will 403 them is worse than not mentioning it.
    """
    if not request_allowed(request):
        return None
    notices = Notice.objects.filter(event_id=event_id).select_related("author").order_by("-created_at")
    return MentionSection(
        heading="Omtalt på opslagstavlen",
        mentions=[
            Mention(
                url=reverse("opslagstavle:detail", args=[n.pk]),
                excerpt_markdown=n.body,
                author_name=n.author.full_name,
                created_at=n.created_at,
            )
            for n in notices[:MAX_MENTIONS]
        ],
    )


register_mention_provider(notices_about_event)
