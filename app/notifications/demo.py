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

Those links are other domains' content, and this app is platform: it may not import the features
whose rows it points at. So seed_demo, which wires every domain together anyway, looks them up and
hands them in as a `DemoContent`.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from residents.api import Resident

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


@dataclass(frozen=True)
class DemoContent:
    """What the demo rows point at in the features seeded before them."""

    event_url: str
    event_title: str
    other_event_title: str
    notice_url: str
    chat_url: str
    chat_preview: str
    # The category of the Den Hurtige room beboer@ has muted; None mutes nothing.
    muted_room: str | None = None


def seed(residents: list[Resident], now: datetime, rng: random.Random, content: DemoContent) -> int:
    logins = [r for r in residents if r.email in LOGINS]
    others = [r for r in residents if r.email not in LOGINS]
    if not logins or len(others) < 3:
        return 0
    rows: list[Notification] = []
    for resident in logins:
        a, b, c = rng.sample(others, 3)
        specs = [
            _Row(
                categories.SYSTEM,
                Importance.KRITISK,
                "Vandet lukkes torsdag kl. 9-12",
                "VVS'eren skifter stigrør i opgang B. Fyld en kande inden.",
                "/intern/",
                timedelta(minutes=25),
            ),
            _Row(
                categories.EVENTS_CHANGES,
                Importance.HOEJ,
                f"Ændret: {content.event_title}",
                "Starter nu en halv time senere.",
                content.event_url,
                timedelta(hours=2),
            ),
            _Row(
                categories.POSTS_REPLIES,
                Importance.NORMAL,
                f"{a.first_name} og {b.first_name} svarede",
                "God idé, jeg tager kage med!",
                content.notice_url,
                timedelta(hours=5),
                group_key="posts.replies:demo",
                actors=(a, b),
            ),
            _Row(
                categories.CHAT_LIKES,
                Importance.LAV,
                "3 personer reagerede på din besked",
                f"👍 {content.chat_preview}".strip(),
                content.chat_url,
                timedelta(days=1),
                group_key="chat.likes:demo",
                actors=(a, b, c),
            ),
            _Row(
                categories.EVENTS_NEW,
                Importance.NORMAL,
                content.other_event_title,
                "Alle er velkomne.",
                content.event_url,
                timedelta(days=2),
                seen=True,
            ),
            _Row(
                categories.GENERAL,
                Importance.NORMAL,
                "Husmøde i næste uge",
                "Dagsordenen ligger på opslagstavlen.",
                content.notice_url,
                timedelta(days=3),
                seen=True,
            ),
            _Row(
                categories.REPAIRS,
                Importance.NORMAL,
                "Ny reparation meldt ind",
                "Dryppende vandhane i køkken 2.",
                "/intern/reparationer/",
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
    if beboer is not None and content.muted_room is not None:
        NotificationPreference.objects.update_or_create(
            resident=beboer,
            category=content.muted_room,
            defaults={"in_app": True, "push": False, "vibrate": False},
        )
    return len(rows)
