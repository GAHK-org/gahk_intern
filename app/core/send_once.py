"""One send, one message: a form's send is saved once, however many times it reaches the server.

The same send can arrive twice: a second press while the first is still on its way (Safari sends
both; a phone photo takes seconds to upload), or the browser sending a request again after its
connection dropped. The browser stops what it can (frontend/src/send-once.ts); this is the part that
sees every path.

Each form carries a token, made when the form is drawn (`{% send_token %}`, templatetags/send_once.py)
and renewed by the page once a send has gone through and the form stays (Den Hurtige's composer and
reply box). The first request with a token is saved together with it; a request with a token already
saved is the same send again, and is answered as the first was. Two sends of the same text are two
tokens, so writing the same thing twice on purpose works, photos included.

A form without a token (a page drawn before this existed) is saved as before.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils import timezone

from residents.permissions import current_resident

from .models import SentForm

FIELD = "send_token"
KEPT = timedelta(days=1)


@dataclass
class Send:
    repeated: bool = False
    # Where the first send went, when it said (a view sets it on a new send for the next to read).
    landed: str = ""


def token(request: HttpRequest) -> uuid.UUID | None:
    try:
        return uuid.UUID(request.POST.get(FIELD, ""))
    except ValueError:
        return None


@contextmanager
def send_once(request: HttpRequest) -> Iterator[Send]:
    """A transaction for saving one send. `send.repeated` says it was saved before: answer, save nothing.

    The token is saved first, in the same transaction as what the view saves. A copy arriving at the
    same moment waits on the token's unique index until the first commits, then finds it, so two
    copies together are still saved once. If the view fails, the token goes with it, and sending
    again works.
    """
    sent = token(request)
    send = Send()
    with transaction.atomic():
        if sent is None:
            yield send
            return
        resident = current_resident(request)
        try:
            with transaction.atomic():
                row = SentForm.objects.create(resident=resident, token=sent)
        except IntegrityError:
            first = SentForm.objects.get(resident=resident, token=sent)
            yield Send(repeated=True, landed=first.landed)
            return
        SentForm.objects.filter(resident=resident, created_at__lt=timezone.now() - KEPT).delete()
        yield send
        if send.landed:
            row.landed = send.landed
            row.save(update_fields=["landed"])
