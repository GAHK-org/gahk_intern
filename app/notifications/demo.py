"""Demo notifications for `manage.py seed_demo`.

For the documented logins, so the bell's badge, its dropdown and the notifications page all have
something to show on a fresh checkout, in the states worth seeing:

  * unseen at every importance, Kritisk included (the coloured edge and the level word);
  * a grouped row naming two people, and one counting several ("3 personer reagerede");
  * seen ones, which the list shows and the badge does not count;
  * one long expired, shown nowhere, so `purge_expired_notifications` has something to purge.

Rows are written directly rather than through services.notify: that would apply preferences and
try to push, and the point here is a fixed picture. The links go to seeded events, opslag and Den
Hurtige posts, so clicking one lands somewhere real. beboer@ also gets one muted room, so the
settings page shows a row that differs from the default.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.urls import reverse

from residents.models import Resident

from . import categories
from .categories import Importance
from .models import Notification, NotificationPreference

LOGINS = ("admin@gahk.dk", "formand@gahk.dk", "ak@gahk.dk", "oel@gahk.dk", "beboer@gahk.dk")


@dataclass(frozen=True)
class _Row:
    category: str
    importance: Importance
    title: str
    body: str
    url: str
    age: timedelta
    seen: bool = False
    expired: bool = False
    group_key: str = ""
    actors: tuple[Resident, ...] = ()


def _links(now: datetime) -> tuple[str, str, str, str, str, str]:
    """(event url, event title, other event title, notice url, chat url, chat preview), falling back
    to the feature's front page when that feature seeded nothing."""
    from den_hurtige import channels
    from den_hurtige.models import QuickPost
    from events.models import Event
    from opslagstavle.models import Notice

    events = list(
        Event.objects.filter(cancelled_at__isnull=True, starts_at__gt=now).order_by("starts_at")[:2]
    )
    event_url = reverse("events:detail", args=[events[0].pk]) if events else reverse("events:index")
    event_title = events[0].title if events else "Fællesspisning"
    other_title = events[1].title if len(events) > 1 else "Brætspilsaften"
    notice = Notice.objects.order_by("-created_at").first()
    notice_url = reverse("opslagstavle:detail", args=[notice.pk]) if notice else reverse("opslagstavle:board")
    post = QuickPost.objects.order_by("-created_at").first()
    if post is None:
        return event_url, event_title, other_title, notice_url, reverse("den_hurtige:feed"), ""
    channel = channels.lookup(post.channel) or channels.DEFAULT
    return (
        event_url,
        event_title,
        other_title,
        notice_url,
        f"{channel.url}?traad={post.pk}",
        post.content[:60],
    )


def seed(residents: list[Resident], now: datetime, rng: random.Random) -> int:
    logins = [r for r in residents if r.email in LOGINS]
    others = [r for r in residents if r.email not in LOGINS]
    if not logins or len(others) < 3:
        return 0
    event_url, event_title, other_title, notice_url, chat_url, chat_preview = _links(now)
    rows: list[Notification] = []
    for resident in logins:
        a, b, c = rng.sample(others, 3)
        specs = [
            _Row(
                categories.SYSTEM,
                Importance.KRITISK,
                "Vandet lukkes torsdag kl. 9-12",
                "VVS'eren skifter stigrør i opgang B. Fyld en kande inden.",
                reverse("dashboard"),
                timedelta(minutes=25),
            ),
            _Row(
                categories.EVENTS_CHANGES,
                Importance.HOEJ,
                f"Ændret: {event_title}",
                "Starter nu en halv time senere.",
                event_url,
                timedelta(hours=2),
            ),
            _Row(
                categories.POSTS_REPLIES,
                Importance.NORMAL,
                f"{a.first_name} og {b.first_name} svarede",
                "God idé, jeg tager kage med!",
                notice_url,
                timedelta(hours=5),
                group_key="posts.replies:demo",
                actors=(a, b),
            ),
            _Row(
                categories.CHAT_LIKES,
                Importance.LAV,
                "3 personer reagerede på din besked",
                f"👍 {chat_preview}".strip(),
                chat_url,
                timedelta(days=1),
                group_key="chat.likes:demo",
                actors=(a, b, c),
            ),
            _Row(
                categories.EVENTS_NEW,
                Importance.NORMAL,
                other_title,
                "Alle er velkomne.",
                event_url,
                timedelta(days=2),
                seen=True,
            ),
            _Row(
                categories.GENERAL,
                Importance.NORMAL,
                "Husmøde i næste uge",
                "Dagsordenen ligger på opslagstavlen.",
                notice_url,
                timedelta(days=3),
                seen=True,
            ),
            _Row(
                categories.REPAIRS,
                Importance.NORMAL,
                "Ny reparation meldt ind",
                "Dryppende vandhane i køkken 2.",
                reverse("reparationer:board"),
                timedelta(days=50),
                seen=True,
                expired=True,
            ),
        ]
        for spec in specs:
            at = now - spec.age
            rows.append(
                Notification(
                    recipient=resident,
                    category=spec.category,
                    importance=spec.importance,
                    title=spec.title,
                    body=spec.body,
                    url=spec.url,
                    group_key=spec.group_key,
                    actors=[{"id": r.pk, "name": r.full_name} for r in spec.actors],
                    created_at=at,
                    updated_at=at,
                    # Long expired: past purge_expired's month too, so the purge finds it.
                    expires_at=now - timedelta(days=40) if spec.expired else now + timedelta(days=7),
                    seen_at=at + timedelta(minutes=10) if spec.seen else None,
                )
            )
    Notification.objects.bulk_create(rows)

    beboer = next((r for r in logins if r.email == "beboer@gahk.dk"), None)
    if beboer is not None:
        from den_hurtige import channels

        NotificationPreference.objects.update_or_create(
            resident=beboer,
            category=categories.chat_room_key(channels.CHANNELS[-1].slug),
            defaults={"in_app": True, "push": False, "vibrate": False},
        )
    return len(rows)


def seed_kitchen(kitchen: Resident, residents: list[Resident], now: datetime) -> int:
    """The kitchen account's bell: one row in each category it can be sent (KITCHEN_CATEGORIES), linked
    to the Køkkenet posts and opslag seeded for it, plus the same Kritisk broadcast every login gets.
    Its settings page lists only these categories, so this is also what that page is about."""
    from den_hurtige import channels
    from den_hurtige.models import QuickPost
    from opslagstavle.models import Notice

    if len(residents) < 5:
        return 0
    kitchen_posts = QuickPost.objects.filter(channel=channels.KITCHEN_CHANNEL)
    own_post = kitchen_posts.filter(author=kitchen, comments__isnull=False).first()
    their_post = kitchen_posts.exclude(author=kitchen).first()
    notice = Notice.objects.filter(author=kitchen, comments__isnull=False).first()
    channel_url = channels.BY_SLUG[channels.KITCHEN_CHANNEL].url
    own_url = f"{channel_url}?traad={own_post.pk}" if own_post else channel_url
    their_url = f"{channel_url}?traad={their_post.pk}" if their_post else channel_url
    notice_url = reverse("opslagstavle:detail", args=[notice.pk]) if notice else reverse("opslagstavle:board")
    a, b, c = residents[2], residents[3], residents[4]
    specs = [
        _Row(
            categories.SYSTEM,
            Importance.KRITISK,
            "Vandet lukkes torsdag kl. 9-12",
            "VVS'eren skifter stigrør i opgang B. Fyld en kande inden.",
            channel_url,
            timedelta(minutes=25),
        ),
        _Row(
            categories.chat_room_key(channels.KITCHEN_CHANNEL),
            Importance.NORMAL,
            residents[1].full_name,
            their_post.content[:60] if their_post else "",
            their_url,
            timedelta(minutes=30),
        ),
        _Row(
            categories.CHAT_REPLIES,
            Importance.NORMAL,
            f"{a.first_name} svarede på din besked",
            "Kan man stadig nå at bestille den vegetariske?",
            own_url,
            timedelta(hours=1, minutes=55),
            group_key="chat.replies:demo-kitchen",
            actors=(a,),
        ),
        _Row(
            categories.CHAT_LIKES,
            Importance.LAV,
            "3 personer reagerede på din besked",
            f"👍 {own_post.content[:60]}" if own_post else "👍",
            own_url,
            timedelta(hours=1),
            group_key="chat.likes:demo-kitchen",
            actors=(b, c, residents[5] if len(residents) > 5 else a),
        ),
        _Row(
            categories.POSTS_REPLIES,
            Importance.NORMAL,
            f"{residents[1].first_name} og {a.first_name} kommenterede",
            "Kan vi låne køkkenet til en julefrokost?",
            notice_url,
            timedelta(hours=19),
            group_key="posts.replies:demo-kitchen",
            actors=(residents[1], a),
            seen=True,
        ),
        _Row(
            categories.GENERAL,
            Importance.NORMAL,
            "Husmøde i næste uge",
            "Dagsordenen ligger på opslagstavlen.",
            channel_url,
            timedelta(days=3),
            seen=True,
        ),
    ]
    rows = [
        Notification(
            recipient=kitchen,
            category=spec.category,
            importance=spec.importance,
            title=spec.title,
            body=spec.body,
            url=spec.url,
            group_key=spec.group_key,
            actors=[{"id": r.pk, "name": r.full_name} for r in spec.actors],
            created_at=now - spec.age,
            updated_at=now - spec.age,
            expires_at=now + timedelta(days=7),
            seen_at=now - spec.age + timedelta(minutes=10) if spec.seen else None,
        )
        for spec in specs
    ]
    Notification.objects.bulk_create(rows)
    return len(rows)
