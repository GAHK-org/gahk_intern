"""Who is told what, and how — the one place every feature notifies through.

Features decide the AUDIENCE (who could care: a channel's readers, an event's invitees, the person
whose post was answered) and the WORDING. This module decides everything after that, from the
resident's own settings:

  * whether they get an in-app row, a push, both or neither (NotificationPreference, per category);
  * whether the push vibrates (NotificationSettings plus the category's vibrate toggle);
  * how long it lives (the TTL, also handed to the push service so a stale push is dropped there).

KRITISK overrides every setting: the in-app row (where the category has one) and the push both go
out, vibrating. That is what the level is for, and the only reason an administrator would pick it.

ONE PUSH PAYLOAD PER FAN-OUT, NOT PER RECIPIENT. Every row of one `notify` call shares a `batch`
UUID, and the payload's open-link carries it: /intern/notifikationer/aabn/<batch>/ marks the
*requesting* resident's row seen and redirects to its url. So a dorm-wide message is still one
payload (or two: one vibrating, one silent), not a hundred.

Delivery runs after commit on a background thread (core.push.send_many); rows are written inline,
so the dropdown is right the moment the request returns.
"""

import datetime
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q, QuerySet
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from core import push
from core.clock import current_datetime
from core.models import PushSubscription
from residents.api import Resident, resident_ids_with_any_role, roster_resident_ids

from . import categories, live
from .categories import Importance
from .models import Notification, NotificationPreference, NotificationSettings

# The TTL of anything that does not bring its own. A Den Hurtige message brings its expiry.
DEFAULT_TTL = datetime.timedelta(days=7)


def _default_expiry(now: datetime.datetime) -> datetime.datetime:
    """`now` plus DEFAULT_TTL as elapsed time: exactly seven times 24 hours.

    `now` is local time (core.clock.current_datetime), and Python adds a timedelta to an aware
    datetime on the wall clock. Across a change to or from summer time "+7 days" would then be an
    hour more or less than seven days — longer than the TTL handed to the push service. In UTC there
    is no such change.
    """
    return now.astimezone(datetime.UTC) + DEFAULT_TTL


# How many actors a grouped row remembers. Only the first two are ever named; the rest are a count,
# so keeping every liker of a popular post would grow the row for nothing.
MAX_ACTORS = 50

Recipients = QuerySet[Resident] | Iterable[int]


def is_site_path(url: str) -> bool:
    """A path on this site and nothing else. `url_has_allowed_host_and_scheme` with no hosts allowed
    is Django's own guard: it also refuses the forms browsers quietly turn into another host,
    such as a slash followed by a backslash, which a plain `startswith("/")` lets through."""
    return url.startswith("/") and url_has_allowed_host_and_scheme(url, allowed_hosts=None)


def everyone() -> QuerySet[Resident]:
    """The whole kollegium, as an audience: anyone living here this month, anyone holding a role
    this month (administrators need not live here), superusers — and anyone with a push device.

    The last one is what the audience used to BE (everyone who had pressed a subscribe button), so
    nobody who was being notified before stops being notified because the residency list is a month
    behind. Features narrow this further with their rollout gate (access.allowed_residents).
    """
    devices = PushSubscription.objects.values("user_id")
    return Resident.objects.filter(is_active=True).filter(
        Q(pk__in=roster_resident_ids())
        | Q(pk__in=resident_ids_with_any_role())
        | Q(pk__in=devices)
        | Q(is_superuser=True)
    )


@dataclass(frozen=True)
class Decision:
    """How one resident gets one notification."""

    in_app: bool
    push: bool
    vibrate: bool


def _decisions(ids: list[int], category: categories.Category, importance: Importance) -> dict[int, Decision]:
    """Each recipient's Decision, from at most two queries however many recipients there are.
    Missing rows are the defaults — everything on — see models' module docstring."""
    prefs = {
        p.resident_id: p
        for p in NotificationPreference.objects.filter(resident_id__in=ids, category=category.key)
    }
    settings = {s.resident_id: s for s in NotificationSettings.objects.filter(resident_id__in=ids)}
    out: dict[int, Decision] = {}
    for rid in ids:
        if importance >= Importance.KRITISK:
            out[rid] = Decision(in_app=category.in_app, push=True, vibrate=True)
            continue
        pref = prefs.get(rid)
        setting = settings.get(rid)
        vibrate = (
            (pref is None or pref.vibrate)
            and (setting is None or setting.vibrate)
            and (setting is None or importance >= setting.vibrate_min_importance)
        )
        out[rid] = Decision(
            in_app=category.in_app and (pref is None or pref.in_app or not category.in_app_optional),
            push=pref is None or pref.push,
            vibrate=bool(vibrate),
        )
    return out


def _ids(recipients: Recipients) -> list[int]:
    if isinstance(recipients, QuerySet):
        return list(recipients.values_list("pk", flat=True).distinct())
    return list(dict.fromkeys(recipients))


def _with_devices(ids: Iterable[int]) -> set[int]:
    """The recipients a push could reach at all. Empty when push is not configured (dev), which
    makes push-only categories quietly produce nothing there rather than unreachable rows."""
    if not push.is_configured():
        return set()
    return set(PushSubscription.objects.filter(user_id__in=list(ids)).values_list("user_id", flat=True))


def open_url(batch: uuid.UUID) -> str:
    return reverse("notifications:open_batch", args=[batch])


def _deliveries(
    vibrating: list[int],
    silent: list[int],
    *,
    title: str,
    body: str,
    url: str,
    batch: uuid.UUID,
    tag: str,
    importance: Importance,
    ttl: int,
) -> list[push.Delivery]:
    out: list[push.Delivery] = []
    for ids, vibrate in ((vibrating, True), (silent, False)):
        if not ids:
            continue
        payload = push._payload(
            title, body, url, open_url=open_url(batch), tag=tag, vibrate=vibrate, importance=int(importance)
        )
        out.append((PushSubscription.objects.filter(user_id__in=ids), payload, ttl))
    return out


def notify(
    recipients: Recipients,
    *,
    category: str,
    title: str,
    url: str,
    body: str = "",
    importance: Importance | None = None,
    expires_at: datetime.datetime | None = None,
    exclude: int | None = None,
    background: bool = True,
) -> int:
    """Tell `recipients` about one thing. Returns how many residents were reached either way.

    `exclude` is the resident who caused it — nobody is notified about their own post. `background`
    is core.push.send_many's: a management command must pass False.
    """
    cat = categories.get(category)
    level = Importance(importance or cat.importance)
    now = current_datetime()
    expires_at = expires_at or _default_expiry(now)
    if expires_at <= now:
        return 0
    ids = [rid for rid in _ids(recipients) if rid != exclude]
    if not ids:
        return 0

    decisions = _decisions(ids, cat, level)
    reachable = _with_devices(rid for rid, d in decisions.items() if d.push)
    batch = uuid.uuid4()
    rows: list[Notification] = []
    vibrating: list[int] = []
    silent: list[int] = []
    for rid in ids:
        decision = decisions[rid]
        pushed = decision.push and rid in reachable
        if not (decision.in_app or pushed):
            continue
        rows.append(
            Notification(
                recipient_id=rid,
                category=cat.key,
                importance=level,
                title=title[:200],
                body=body[:300],
                url=url,
                batch=batch,
                in_app=decision.in_app,
                pushed=pushed,
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
            )
        )
        if pushed:
            (vibrating if decision.vibrate else silent).append(rid)
    Notification.objects.bulk_create(rows)
    live.inbox_changed(row.recipient_id for row in rows if row.in_app)

    push.send_many(
        _deliveries(
            vibrating,
            silent,
            title=title,
            body=body,
            url=url,
            batch=batch,
            tag="",
            importance=level,
            ttl=push.ttl_until(expires_at),
        ),
        background=background,
    )
    return len(rows)


def grouped_title(actors: list[dict[str, object]], verb: str) -> str:
    """ "Anders svarede", "Anders og Bente svarede", "3 personer svarede". Newest actor first."""
    names = [str(a["name"]) for a in actors]
    if len(names) == 1:
        return f"{names[0]} {verb}"
    if len(names) == 2:
        return f"{names[0]} og {names[1]} {verb}"
    return f"{len(names)} personer {verb}"


def notify_grouped(
    recipient_id: int,
    *,
    category: str,
    group_key: str,
    actor: Resident,
    verb: str,
    url: str,
    body: str = "",
    importance: Importance | None = None,
    expires_at: datetime.datetime | None = None,
    background: bool = True,
) -> Notification | None:
    """Tell ONE resident, folding into their existing row for `group_key` if there is one.

    "Anders reagerede på din besked", then "Anders og Bente reagerede på din besked", then
    "3 personer reagerede på din besked" — one row, rewritten, unseen again and moved to the top.
    The push carries `group_key` as its tag, so on the device the new banner replaces the old one
    instead of stacking under it.

    A row whose TTL has run out is revived rather than duplicated (the unique constraint holds over
    expired rows too) — and starts its actor list over: last week's likers are not news.
    """
    if actor.pk == recipient_id:
        return None
    cat = categories.get(category)
    level = Importance(importance or cat.importance)
    now = current_datetime()
    expires_at = expires_at or _default_expiry(now)
    if expires_at <= now:
        return None

    decision = _decisions([recipient_id], cat, level)[recipient_id]
    pushed = decision.push and recipient_id in _with_devices([recipient_id])
    if not (decision.in_app or pushed):
        return None

    me = {"id": actor.pk, "name": actor.full_name}
    with transaction.atomic():
        row, created = Notification.objects.select_for_update().get_or_create(
            recipient_id=recipient_id,
            group_key=group_key,
            defaults={
                "category": cat.key,
                "importance": level,
                "title": "",
                "url": url,
                "expires_at": expires_at,
                "created_at": now,
                "actors": [],
            },
        )
        revived = not created and row.expires_at <= now
        if revived:
            row.created_at = now
        previous = [] if created or revived else list(row.actors)
        actors = [me, *(a for a in previous if a.get("id") != actor.pk)][:MAX_ACTORS]
        row.actors = actors
        row.category = cat.key
        row.importance = level
        row.title = grouped_title(actors, verb)[:200]
        row.body = body[:300]
        row.url = url
        if pushed:
            # The banner on the device carries the batch; keep it valid until a new push replaces it.
            row.batch = uuid.uuid4()
        row.in_app = decision.in_app
        row.pushed = row.pushed or pushed
        row.updated_at = now
        row.expires_at = expires_at
        row.seen_at = None
        row.save()
    if decision.in_app:
        live.inbox_changed([recipient_id])

    if pushed:
        push.send_many(
            _deliveries(
                [recipient_id] if decision.vibrate else [],
                [] if decision.vibrate else [recipient_id],
                title=row.title,
                body=row.body,
                url=url,
                batch=row.batch,
                tag=group_key,
                importance=level,
                ttl=push.ttl_until(row.expires_at),
            ),
            background=background,
        )
    return row


def mark_seen(rows: QuerySet[Notification]) -> int:
    unseen = rows.filter(seen_at__isnull=True)
    # Whose badges this clears, read before the UPDATE makes them indistinguishable from the rest.
    recipients = list(unseen.values_list("recipient_id", flat=True).distinct())
    marked = unseen.update(seen_at=current_datetime())
    if marked:
        live.inbox_changed(recipients)
    return marked


def unseen_count(resident_id: int) -> int:
    """The bell's badge. Only what the dropdown can show — a push-only row is not unseen in-app."""
    return Notification.objects.filter(recipient_id=resident_id).visible().unseen().count()


def purge_expired(older_than: datetime.timedelta = datetime.timedelta(days=30)) -> int:
    """Delete rows whose TTL ran out more than `older_than` ago. They stop showing at expiry; they
    are kept that long after only so the admin statistics have a month to count."""
    deleted, _ = Notification.objects.filter(expires_at__lt=current_datetime() - older_than).delete()
    return deleted
