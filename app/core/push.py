"""Web Push transport, shared by every feature that notifies residents.

Talks to pywebpush directly rather than through django-webpush. That package is a thin Django
wrapper whose useful surface we had already bypassed (its URLs, template tags, JS and service worker
all conflicted with this PWA), and the little that was left got in the way: its send helper swallows
410 internally and re-raises everything else, so a single dead endpoint aborted the loop and dropped
every remaining recipient — while a 410 looked like a success to the caller. Owning the ~30 lines
here means one error path instead of two, and one table instead of its three.

Delivery runs off the request thread: each subscription is a separate HTTPS round-trip to FCM/APNs
(~0.3-1 s), and Daphne runs with `-t 60`, so a dorm-wide fan-out inline would kill the
worker. `_run_in_background` fires after the transaction commits, so the thread never races the post
it is announcing.

This module is *transport*. Who gets notified about what — and the wording — stays with each
feature (den_hurtige/services.py, opslagstavle/services.py), because that is policy and it differs:
a chat message goes to the whole dorm, a comment on a noticeboard post goes to its author.
"""

import json
import logging
import threading
from collections.abc import Callable

from django.conf import settings
from django.db import connection, transaction
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.templatetags.static import static
from pywebpush import WebPushException, webpush

from residents.permissions import current_resident

from .forms import PushSubscriptionForm
from .models import TOPIC_FIELDS, PushSubscription

logger = logging.getLogger(__name__)

# Time-to-live. Both current topics are worthless long after the fact — a "Den Hurtige" message
# within the hour, a noticeboard post within the day — so tell the push service to drop it rather
# than deliver it to a phone that comes back online next week.
TTL_SECONDS = 3600

BODY_PREVIEW_CHARS = 120

# Endpoints a browser has permanently discarded. 410 Gone is the documented signal; FCM also answers
# 404 for an endpoint it no longer knows. Anything else may be transient, so the row is kept.
DEAD_ENDPOINT_STATUSES = (404, 410)


def is_configured() -> bool:
    """True when both VAPID keys are set. The subscribe UI reports 'not set up' otherwise, which is
    the normal dev state — the features themselves work fine without push."""
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def vapid_public_key() -> str:
    return str(settings.VAPID_PUBLIC_KEY)


def preview(text: str) -> str:
    """Collapse whitespace and clip to a lock-screen-sized excerpt."""
    text = " ".join(text.split())
    if len(text) <= BODY_PREVIEW_CHARS:
        return text
    return text[: BODY_PREVIEW_CHARS - 1].rstrip() + "…"


def _payload(head: str, body: str, url: str) -> dict[str, str]:
    """The JSON the service worker (app/templates/sw.js) reads: head/body/icon/url.

    `head` is the sender or the subject, not the feature. Every platform already labels the
    notification with the app it came from — iOS renders the manifest name under the title — so a
    title like "Ny besked på Den Hurtige" said it twice and pushed the part that matters (who, and
    what they wrote) down into the body.

    `url` is a parameter rather than a constant because it is what makes a tap land on the right
    thing: the feed for a chat message, the individual post for a noticeboard notification.
    """
    return {"head": head, "body": body, "icon": static("icons/icon-192x192.png"), "url": url}


def subscribers(topic: str, exclude_user_id: int | None = None) -> QuerySet[PushSubscription]:
    """Every device opted in to `topic`, optionally minus one user's (normally the author — they
    just pressed the button, so a push back to their own phone is pure noise).

    `topic` is required, not defaulted: a default would make "notify everyone" the behaviour you get
    by forgetting the argument, which is the wrong way round for consent.
    """
    qs = PushSubscription.objects.filter(**{TOPIC_FIELDS[topic]: True})
    if exclude_user_id is not None:
        qs = qs.exclude(user_id=exclude_user_id)
    return qs


def _send(subscription: PushSubscription, body: str) -> None:
    """One encrypted push. Raises WebPushException on any non-2xx from the push service."""
    webpush(
        subscription_info=subscription.as_subscription_info(),
        data=body,
        ttl=TTL_SECONDS,
        vapid_private_key=settings.VAPID_PRIVATE_KEY,
        # Built fresh every call on purpose: pywebpush writes `aud` and `exp` into this dict, so a
        # shared/module-level one would carry the first recipient's audience to everyone after it.
        vapid_claims={"sub": f"mailto:{settings.VAPID_ADMIN_EMAIL}"},
    )


def _dispatch(subscriptions: QuerySet[PushSubscription], payload: dict[str, str]) -> int:
    """Send `payload` to every subscription. Returns the number actually delivered.

    One bad endpoint must never cost the rest of the dorm its notification, so every device is
    isolated in its own try. Endpoints the browser has thrown away are deleted here rather than left
    to accumulate — nothing else ever cleans this table up.
    """
    body = json.dumps(payload)
    sent = attempted = 0
    for subscription in subscriptions:
        attempted += 1
        try:
            _send(subscription, body)
        except WebPushException as exc:
            # pywebpush leaves exc.response as None for connection-level failures, so read the
            # status defensively — a direct exc.response.status_code raises AttributeError there.
            status = getattr(exc.response, "status_code", None)
            if status in DEAD_ENDPOINT_STATUSES:
                subscription.delete()
            else:
                logger.warning("push failed (%s) for subscription %s", status, subscription.pk)
        except Exception:
            logger.exception("push crashed for subscription %s", subscription.pk)
        else:
            sent += 1
    # Always logged, including the zero cases: "no subscribers" and "every send failed" are
    # indistinguishable from the outside, and both look exactly like the feature being broken.
    logger.info("push delivered to %s/%s device(s)", sent, attempted)
    return sent


def _run_in_background(fn: Callable[[], object]) -> None:
    """Run `fn` in a daemon thread once the current transaction commits.

    on_commit keeps the thread from reading a post that a later rollback removes. The thread closes
    its own DB connection afterwards — without that, every push batch leaks a Postgres connection
    (conn_max_age=600 keeps them open).
    """

    def runner() -> None:
        try:
            fn()
        except Exception:
            logger.exception("background push batch failed")
        finally:
            connection.close()

    transaction.on_commit(lambda: threading.Thread(target=runner, daemon=True).start())


def send(
    subscriptions: QuerySet[PushSubscription],
    head: str,
    body: str,
    url: str,
    *,
    background: bool = True,
) -> None:
    """Queue a push to an already-chosen audience. The feature decides who; this delivers.

    `background=False` delivers INLINE, and a management command must pass it.

    The default path hands the fan-out to a daemon thread (see _run_in_background), which is right
    in a request and silently wrong in a command: `handle()` returns, the interpreter shuts down,
    and Python kills daemon threads where they stand. A dorm-wide fan-out is one HTTPS round-trip
    per device, so a scheduled job would deliver to however many devices it happened to reach before
    exiting — a different number every night, with no error anywhere to say so.

    The cost of inline is the one the threading exists to avoid: the caller blocks for ~0.3-1 s per
    device. That is unacceptable in a request under the 60 s timeout and completely fine in a
    cron job, which has nothing else to do.

    core/management/commands/send_test_push.py predates this argument and reaches for the private
    `_send` to sidestep the thread. This parameter is the same fix made available properly; that
    command can move onto it whenever someone is next in there.
    """
    if not is_configured():
        return
    payload = _payload(head, body, url)
    if background:
        _run_in_background(lambda: _dispatch(subscriptions, payload))
    else:
        _dispatch(subscriptions, payload)


def notify(
    topic: str,
    head: str,
    body: str,
    url: str,
    exclude_user_id: int | None = None,
    *,
    background: bool = True,
) -> None:
    """Queue a push to everyone opted in to `topic`. The common case, on top of `send`."""
    send(
        subscribers(topic, exclude_user_id=exclude_user_id),
        head,
        body,
        url,
        background=background,
    )


def handle_subscription_request(request: HttpRequest, topic: str) -> HttpResponse:
    """Store (or drop) this browser's opt-in to `topic`. Shared by every feature's subscribe view.

    Login-gated and CSRF-protected by the calling view, unlike django-webpush's
    /webpush/save_information, which was @csrf_exempt and took its audience from the request body —
    anyone could have registered an endpoint and received every dorm message. Here the subscription
    is always bound to the session's own resident.

    The per-topic semantics are the whole reason this is not a two-liner:

      * Subscribing writes exactly ONE consent column. Writing both would mean opting in to the
        noticeboard silently opted you in to (or out of) the chat.
      * Unsubscribing clears one column and deletes the row only when NO topic is left. A browser
        has one push endpoint for both features, so deleting the row on the first opt-out would
        silently kill the other feature's notifications on that device.
      * The 202 reports how many topics remain, because the browser must only call its own
        `subscription.unsubscribe()` once nothing is subscribed any more.
    """
    resident = current_resident(request)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=400)
    if not isinstance(data, dict):
        return HttpResponse(status=400)

    form = PushSubscriptionForm.from_payload(data)
    if not form.is_valid():
        return HttpResponse(status=400)
    fields = form.cleaned_data

    # The view chose the topic (it owns the page); the payload is only a cross-check, so a crafted
    # body cannot subscribe a device to a feature whose page the caller never passed the gate for.
    if fields["topic"] != topic:
        return HttpResponse(status=400)
    field = TOPIC_FIELDS[topic]

    if fields["status_type"] == "unsubscribe":
        # Scoped to this user: the endpoint identifies the device, and scoping to the session's
        # resident stops one person unsubscribing another's phone by replaying it.
        subscription = PushSubscription.objects.filter(user=resident, endpoint=fields["endpoint"]).first()
        if subscription is None:
            return JsonResponse({"remaining_topics": 0}, status=202)
        setattr(subscription, field, False)
        remaining = sum(bool(getattr(subscription, f)) for f in TOPIC_FIELDS.values())
        if remaining:
            subscription.save(update_fields=[field])
        else:
            subscription.delete()
        return JsonResponse({"remaining_topics": remaining}, status=202)

    # Upsert on the endpoint: re-subscribing, or a second resident logging in on a shared browser,
    # must move the existing row rather than leave a stale one pushing to the previous owner.
    # `defaults` deliberately carries only this topic's flag — see the docstring.
    PushSubscription.objects.update_or_create(
        endpoint=fields["endpoint"],
        defaults={
            "user": resident,
            "auth": fields["auth"],
            "p256dh": fields["p256dh"],
            "user_agent": fields["user_agent"],
            field: True,
        },
    )
    return HttpResponse(status=201)
