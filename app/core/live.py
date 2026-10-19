"""Live updates over WebSockets: "something on this page changed, come and look".

THE SOCKET CARRIES SIGNALS, NEVER CONTENT. An event is a topic, a kind and a primary key —
{"topic": "opslagstavle", "kind": "changed", "pk": 41} — and the browser answers it by re-fetching through the ordinary HTTP views it already
uses. That one decision is what keeps this module small:

  * Rendering stays per reader. A message carries "did *I* react?", a delete button only its author
    or a moderator sees, an administrator previewing as a beboer loses both. Pushing rendered HTML
    would mean rendering it once per subscriber here, or getting it wrong for all but one of them.
  * Access stays in one place. The view a signal leads to runs every check it always ran, so a
    signal that reached the wrong socket hands over a number and nothing else.
  * A lost signal costs latency, not correctness. A page re-fetches everything it shows when its
    socket reconnects (frontend/src/live.ts), and Den Hurtige also keeps a slow poll for what has no
    signal at all (a message archiving), so nothing here needs delivery guarantees, replay or
    ordering. Opslagstavlen has no poll: a signal it missed leaves it as it was before it was live.

TOPICS are strings like "den-hurtige.tv-rezz" or "opslagstavle". The part before the first dot names
a feature, which registers who may subscribe (`register`, called from the feature's AppConfig.ready);
the rest is the feature's own to interpret. A topic nobody registered refuses every subscriber.

A page has ONE socket and may listen to several topics on it — every page has the bell's
"notifikationer.<pk>", and Den Hurtige adds its channels — which is why each signal names its topic
and every listener in the browser filters on it.

THE CHANNEL LAYER IS IN-MEMORY (CHANNEL_LAYERS in config/settings.py), which is correct for exactly
as long as `web` is one uvicorn process — see DEPLOY.md §4. A signal published by another process (a
Celery worker, `manage.py shell`, a second uvicorn worker) reaches nobody; see DEPLOY.md §4.
"""

import logging
import re
from collections.abc import Callable
from typing import Literal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.http import HttpRequest

logger = logging.getLogger(__name__)

# Who may subscribe to a topic: called with an HttpRequest-shaped view of the socket (user and
# session, which is all residents.permissions reads) and the part of the topic after the feature
# prefix. Runs in a worker thread, so it may query the database.
Authorizer = Callable[[HttpRequest, str], bool]

_AUTHORIZERS: dict[str, Authorizer] = {}

# Channels refuses group names outside this alphabet and over 100 characters, and a topic is part
# of a URL as well. Checked on both ends so a bad topic fails where it was written.
_TOPIC = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*$")
_MAX_TOPIC = 90  # leaves room for the group prefix under Channels' 100

# The message type the consumer handles: "live.event" dispatches to LiveConsumer.live_event.
EVENT_TYPE = "live.event"


def register(feature: str, authorize: Authorizer) -> None:
    """Declare who may subscribe to `feature` and every `feature.<rest>` topic."""
    _AUTHORIZERS[feature] = authorize


def valid_topic(topic: str) -> bool:
    return len(topic) <= _MAX_TOPIC and bool(_TOPIC.match(topic))


def group_name(topic: str) -> str:
    return f"live.{topic}"


def may_subscribe(request: HttpRequest, topic: str) -> bool:
    """Whether this socket may listen to `topic`. Anonymous, unknown and malformed all say no."""
    if not valid_topic(topic) or not request.user.is_authenticated:
        return False
    feature, _, rest = topic.partition(".")
    authorize = _AUTHORIZERS.get(feature)
    return authorize is not None and authorize(request, rest)


Kind = Literal["created", "changed", "deleted"]


def publish(topic: str, kind: Kind, pk: int | None, **extra: str | int) -> None:
    """Tell everyone watching `topic` that the object `pk` was created, changed or deleted.

    Sent AFTER COMMIT, because the browser's first move on hearing it is to read the change back —
    and a signal that outran its own transaction would fetch the old state and then sit there
    looking current. Outside a transaction (no ATOMIC_REQUESTS here) on_commit runs immediately.

    NEVER RAISES. The write that called this has already happened; a channel layer that is down
    must not turn it into a 500 the author retries, posting twice. The signal is lost, and the
    backstop poll catches up.
    """
    if not valid_topic(topic):
        raise ValueError(f"Not a live topic: {topic!r}")
    data: dict[str, object] = {"topic": topic, "kind": kind, "pk": pk, **extra}
    transaction.on_commit(lambda: _send(topic, data))


def _send(topic: str, data: dict[str, object]) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(group_name(topic), {"type": EVENT_TYPE, "data": data})
    except Exception:
        logger.exception("Could not publish a live update to %s", topic)
