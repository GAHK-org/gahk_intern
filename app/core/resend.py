"""One send, one message: what stops a message, reply, post or comment from being saved twice.

The same send can reach the server more than once:

  * a second press while the first is still on its way (a phone photo takes seconds to upload, and
    the button looks as if nothing happened). The browser drops the first page load, not the first
    request, which the server has usually already received;
  * the browser sending a request again after its connection dropped;
  * a page brought back by the Back button with the sent text still in the box (#365), sent again.

The browser stops what it can (frontend/src/send-once.ts). This is the part that sees every path: a
send identical to one the same person made in the same place moments ago is answered as that one
was, and saved once.

ONLY SENDS WITH TEXT. Two photos sent on their own a minute apart are two photos, and the text is
the one thing that can be compared without opening the pictures.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from django.db import models, transaction
from django.utils import timezone

from residents.models import Resident

# Long enough for a slow upload sent twice and for a retry; short enough that writing the same thing
# again later is a new message.
RESEND_WINDOW = timedelta(minutes=2)


@contextmanager
def one_send_at_a_time(author: Resident) -> Iterator[None]:
    """A transaction in which this person's other sends wait for this one.

    Without it two copies arriving together both look for the other, both find nothing (neither is
    saved yet), and both save. Locking the author's own row makes the second wait and then find the
    first. Only the same person's sends wait, and only for the moment it takes to save one.
    """
    with transaction.atomic():
        Resident.objects.select_for_update().filter(pk=author.pk).values_list("pk").first()
        yield


def already_sent[M: models.Model](queryset: models.QuerySet[M], text_field: str, text: str) -> M | None:
    """The same send saved moments ago, if there is one. `queryset` is narrowed to the same author and
    place; `text_field` is where the sent `text` is kept. Call inside one_send_at_a_time."""
    if not text:
        return None
    recent = queryset.filter(created_at__gte=timezone.now() - RESEND_WINDOW, **{text_field: text})
    return recent.order_by("-created_at").first()
