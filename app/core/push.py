"""Web Push transport, shared by every feature that notifies residents.

Talks to pywebpush directly rather than through django-webpush. That package is a thin Django
wrapper whose useful surface we had already bypassed (its URLs, template tags, JS and service worker
all conflicted with this PWA), and the little that was left got in the way: its send helper swallows
410 internally and re-raises everything else, so a single dead endpoint aborted the loop and dropped
every remaining recipient — while a 410 looked like a success to the caller. Owning the ~30 lines
here means one error path instead of two, and one table instead of its three.

Delivery runs off the request thread: each subscription is a separate HTTPS round-trip to FCM/APNs
(~0.3-1 s), so a dorm-wide fan-out inline would hold the poster's request for minutes, on a
thread of the one app process. `_run_in_background` fires after the transaction commits, so the thread never races the post
it is announcing.

This module is *transport*. Who gets told what, and whether it is a push at all, is
notifications.services — which reads each resident's settings and calls `send` here with the
devices that are left. Features do not call this module directly any more.
"""

import datetime
import json
import logging
import threading
from collections.abc import Callable

from django.conf import settings
from django.db import connection, transaction
from django.db.models import F, QuerySet
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.templatetags.static import static
from pywebpush import WebPushException, webpush

from residents.api import current_resident

from .clock import current_datetime
from .forms import PushSubscriptionForm
from .models import PushDeliveryDay, PushSubscription

logger = logging.getLogger(__name__)

# Time-to-live when the caller has no better answer. Notifications carry their own (the
# notification's TTL — a Den Hurtige message's is its expiry), so this is the fallback for the test
# command and anything else that sends without one.
TTL_SECONDS = 3600

# The push services' own ceiling: FCM rejects a TTL above four weeks.
MAX_TTL_SECONDS = 28 * 24 * 3600

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


def _payload(
    head: str,
    body: str,
    url: str,
    *,
    open_url: str = "",
    tag: str = "",
    vibrate: bool = True,
    importance: int = 2,
) -> dict[str, object]:
    """The JSON the service worker (app/templates/sw.js) reads.

    `head` is the sender or the subject, not the feature. Every platform already labels the
    notification with the app it came from — iOS renders the manifest name under the title — so a
    title like "Ny besked på Den Hurtige" said it twice and pushed the part that matters (who, and
    what they wrote) down into the body.

    `url` is where the thing lives; `open_url`, when set, is what a tap actually opens — the
    notifications app's open link, which marks the recipient's row seen and then redirects to `url`.
    Both travel: `url` is the fallback for a payload from before open links existed.

    `tag` makes a grouped notification REPLACE its predecessor on the device rather than stack under
    it ("Anders og Bente reagerede" instead of two separate banners). Empty = stands alone.

    `vibrate` is the resident's setting already resolved for this push (notifications.services).
    """
    return {
        "head": head,
        "body": body,
        "icon": static("icons/icon-192x192.png"),
        "url": url,
        "open": open_url,
        "tag": tag,
        "vibrate": vibrate,
        "importance": importance,
    }


def _send(subscription: PushSubscription, body: str, ttl: int = TTL_SECONDS) -> None:
    """One encrypted push. Raises WebPushException on any non-2xx from the push service."""
    webpush(
        subscription_info=subscription.as_subscription_info(),
        data=body,
        ttl=ttl,
        vapid_private_key=settings.VAPID_PRIVATE_KEY,
        # Built fresh every call on purpose: pywebpush writes `aud` and `exp` into this dict, so a
        # shared/module-level one would carry the first recipient's audience to everyone after it.
        vapid_claims={"sub": f"mailto:{settings.VAPID_ADMIN_EMAIL}"},
    )


def _count(delivered: int, failed: int, removed: int) -> None:
    """Add one fan-out's outcome to today's PushDeliveryDay. F() so two concurrent fan-outs add up
    rather than overwrite each other."""
    if not (delivered or failed or removed):
        return
    day, _created = PushDeliveryDay.objects.get_or_create(date=current_datetime().date())
    PushDeliveryDay.objects.filter(pk=day.pk).update(
        delivered=F("delivered") + delivered, failed=F("failed") + failed, removed=F("removed") + removed
    )


def _dispatch(
    subscriptions: QuerySet[PushSubscription], payload: dict[str, object], ttl: int = TTL_SECONDS
) -> int:
    """Send `payload` to every subscription. Returns the number actually delivered.

    One bad endpoint must never cost the rest of the dorm its notification, so every device is
    isolated in its own try. Endpoints the browser has thrown away are deleted here rather than left
    to accumulate — nothing else ever cleans this table up.
    """
    body = json.dumps(payload)
    sent = attempted = removed = 0
    for subscription in subscriptions:
        attempted += 1
        try:
            _send(subscription, body, ttl)
        except WebPushException as exc:
            # pywebpush leaves exc.response as None for connection-level failures, so read the
            # status defensively — a direct exc.response.status_code raises AttributeError there.
            status = getattr(exc.response, "status_code", None)
            if status in DEAD_ENDPOINT_STATUSES:
                subscription.delete()
                removed += 1
            else:
                logger.warning("push failed (%s) for subscription %s", status, subscription.pk)
                PushSubscription.objects.filter(pk=subscription.pk).update(
                    failure_count=F("failure_count") + 1
                )
        except Exception:
            logger.exception("push crashed for subscription %s", subscription.pk)
        else:
            sent += 1
            PushSubscription.objects.filter(pk=subscription.pk).update(
                last_success_at=current_datetime(), failure_count=0
            )
    # Always logged, including the zero cases: "no subscribers" and "every send failed" are
    # indistinguishable from the outside, and both look exactly like the feature being broken.
    logger.info("push delivered to %s/%s device(s)", sent, attempted)
    _count(sent, attempted - sent - removed, removed)
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


def ttl_until(expires_at: datetime.datetime) -> int:
    """Seconds from now until `expires_at`, clamped to what a push service accepts. 0 = already
    expired, which the caller should take as "do not push"."""
    remaining = int((expires_at - current_datetime()).total_seconds())
    return max(0, min(remaining, MAX_TTL_SECONDS))


Delivery = tuple[QuerySet[PushSubscription], dict[str, object], int]


def send_many(deliveries: list[Delivery], *, background: bool = True) -> None:
    """Queue several (devices, payload, ttl) fan-outs in ONE background thread.

    One notification can need more than one payload — residents who want vibration and residents who
    do not get different ones — and a thread per payload would be a thread per variant per post.

    `background=False` delivers INLINE, and a management command must pass it.

    The default path hands the fan-out to a daemon thread (see _run_in_background), which is right
    in a request and silently wrong in a command: `handle()` returns, the interpreter shuts down,
    and Python kills daemon threads where they stand. A dorm-wide fan-out is one HTTPS round-trip
    per device, so a scheduled job would deliver to however many devices it happened to reach before
    exiting — a different number every night, with no error anywhere to say so.

    The cost of inline is the one the threading exists to avoid: the caller blocks for ~0.3-1 s per
    device. That is unacceptable in a request under the 60 s timeout and completely fine in a
    cron job, which has nothing else to do.
    """
    if not is_configured() or not deliveries:
        return

    def run() -> None:
        for subscriptions, payload, ttl in deliveries:
            _dispatch(subscriptions, payload, ttl=ttl)

    if background:
        _run_in_background(run)
    else:
        run()


def handle_subscription_request(request: HttpRequest) -> HttpResponse:
    """Store, drop or look up this browser's push subscription. The body of the ONE subscribe view
    (notifications.views.subscription) — a device subscribes once, not once per feature.

    Login-gated and CSRF-protected by the calling view, unlike django-webpush's
    /webpush/save_information, which was @csrf_exempt and took its audience from the request body —
    anyone could have registered an endpoint and received every dorm message. Here the subscription
    is always bound to the session's own resident.

    Three status types:

      * `subscribe` upserts on the endpoint. Re-subscribing, or a second resident logging in on a
        shared browser and pressing the button, moves the existing row rather than leaving a stale
        one pushing to the previous owner.
      * `unsubscribe` deletes it — scoped to this resident, so one person cannot unsubscribe
        another's phone by replaying its endpoint.
      * `status` answers whether the endpoint is this resident's ("mine"), someone else's
        ("other") or unknown ("none"). The browser cannot tell: getSubscription() only says the
        browser holds *a* subscription, not that the server still has it — an administrator may
        have cancelled it, or the push service may have reported it gone.
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

    if fields["status_type"] == "status":
        owner = (
            PushSubscription.objects.filter(endpoint=fields["endpoint"])
            .values_list("user_id", flat=True)
            .first()
        )
        state = "none" if owner is None else "mine" if owner == resident.pk else "other"
        return JsonResponse({"state": state})

    if fields["status_type"] == "unsubscribe":
        PushSubscription.objects.filter(user=resident, endpoint=fields["endpoint"]).delete()
        return HttpResponse(status=202)

    PushSubscription.objects.update_or_create(
        endpoint=fields["endpoint"],
        defaults={
            "user": resident,
            "auth": fields["auth"],
            "p256dh": fields["p256dh"],
            "user_agent": fields["user_agent"],
            "failure_count": 0,
        },
    )
    return HttpResponse(status=201)
