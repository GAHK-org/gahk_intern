"""The notifications app: who is told what, how, and for how long — and the pages that show it.

notifications.services is the one place every feature notifies through. Features pick the AUDIENCE
and the WORDING; everything after that is decided here from the resident's own settings, so it is
tested here once rather than in every feature's module:

  * in-app row, push, both or neither (NotificationPreference, stored only as exceptions);
  * vibrating or silent push (per category, globally, and by importance level);
  * KRITISK, which overrides all of the above;
  * push-only categories (a Den Hurtige room), which make rows only for residents a push reaches;
  * grouping ("Anders og Bente reagerede…"), one row rewritten rather than stacked;
  * the TTL, on the row and handed to the push service.

Push is observed the way the feature tests observe it: `core.push._dispatch` swapped for a recorder
and `core.push._run_in_background` collapsed to a direct call. So a recorded call is exactly one
fan-out — which matters, because "one payload per group, not per resident" is part of the contract.
The transport itself (what `_dispatch` does with a device) is test_push.py's.
"""

import datetime
import importlib
import uuid
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core import push
from core.models import PushSubscription, Room
from den_hurtige.channels import notification_category
from notifications import categories, services, tasks
from notifications.categories import Importance
from notifications.models import Notification, NotificationPreference, NotificationSettings
from residents.models import Residency, Resident, Role, RoleAssignment, active_period

pytestmark = pytest.mark.django_db

TARGET = "/intern/opslagstavle/"

Recorded = list[tuple[list[int], dict, int]]


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch, settings: object) -> Recorded:
    """Records (recipient user ids, payload, ttl) for every fan-out the code would send.

    VAPID keys set, because without them `send_many` is a no-op AND `services._with_devices` reports
    nobody reachable — push-only categories would then quietly produce no rows at all.
    """
    settings.VAPID_PUBLIC_KEY = "test-public-key"  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = "test-private-key"  # type: ignore[attr-defined]
    recorded: Recorded = []

    def fake_dispatch(subscriptions: object, payload: dict, ttl: int = 0) -> int:
        ids = sorted(subscriptions.values_list("user_id", flat=True))  # type: ignore[attr-defined]
        recorded.append((ids, payload, ttl))
        return len(ids)

    monkeypatch.setattr(push, "_dispatch", fake_dispatch)
    monkeypatch.setattr(push, "_run_in_background", lambda fn: fn())
    return recorded


def device(user: Resident, endpoint: str | None = None) -> PushSubscription:
    """Register a fake push device. One per device, for everything — no topic flags any more."""
    return PushSubscription.objects.create(
        user=user,
        endpoint=endpoint or f"https://push.example/{user.pk}/{uuid.uuid4().hex}",
        auth="a" * 22,
        p256dh="p" * 87,
        user_agent="pytest",
    )


def prefer(resident: Resident, category: str, **flags: bool) -> NotificationPreference:
    """One exception to the all-on default, as the settings page stores it."""
    return NotificationPreference.objects.create(resident=resident, category=category, **flags)


def make_notification(recipient: Resident, **fields: object) -> Notification:
    """A row as services.notify would write it, with whatever the test is about overridden."""
    now = timezone.now()
    defaults: dict[str, object] = {
        "category": categories.GENERAL,
        "importance": Importance.NORMAL,
        "title": "Hej",
        "url": TARGET,
        "created_at": now,
        "updated_at": now,
        "expires_at": now + services.DEFAULT_TTL,
    }
    defaults.update(fields)
    return Notification.objects.create(recipient=recipient, **defaults)


@pytest.fixture
def two(make_resident: Callable[..., Resident]) -> tuple[Resident, Resident]:
    """Two residents, each with a device, everything at the defaults."""
    a = make_resident(email="a@gahk.dk", first_name="Anders", last_name="And")
    b = make_resident(email="b@gahk.dk", first_name="Bente", last_name="Bo")
    device(a)
    device(b)
    return a, b


# --- notify: the defaults -----------------------------------------------------------------------


def test_notify_writes_a_row_per_resident_and_pushes_once(
    pushes: Recorded, two: tuple[Resident, Resident]
) -> None:
    """Default settings: everyone gets the in-app row AND the push, and everyone vibrates — so the
    whole fan-out is ONE payload, not one per resident. The open-link carries the shared batch, which
    is what lets that one payload mark each tapping resident's own row seen."""
    a, b = two

    reached = services.notify(
        Resident.objects.filter(pk__in=[a.pk, b.pk]),
        category=categories.POSTS_NEW,
        title="Nyt opslag",
        body="Kaffe i stuen",
        url=TARGET,
    )

    assert reached == 2
    rows = list(Notification.objects.order_by("recipient_id"))
    assert [r.recipient_id for r in rows] == sorted([a.pk, b.pk])
    assert {r.batch for r in rows} == {rows[0].batch}
    for row in rows:
        assert row.category == categories.POSTS_NEW
        assert row.title == "Nyt opslag"
        assert row.body == "Kaffe i stuen"
        assert row.url == TARGET
        assert row.in_app is True
        assert row.pushed is True
        assert row.importance == Importance.NORMAL
        assert row.seen_at is None

    assert len(pushes) == 1
    ids, payload, _ttl = pushes[0]
    assert ids == sorted([a.pk, b.pk])
    assert payload["head"] == "Nyt opslag"
    assert payload["body"] == "Kaffe i stuen"
    assert payload["url"] == TARGET
    assert payload["open"] == f"/intern/notifikationer/aabn/{rows[0].batch}/"
    assert payload["tag"] == ""
    assert payload["vibrate"] is True
    assert payload["importance"] == int(Importance.NORMAL)


def test_a_resident_without_a_device_gets_the_row_but_no_push(
    pushes: Recorded, make_resident: Callable[..., Resident]
) -> None:
    """In-app needs no device. `pushed` is statistics: it must say what actually happened."""
    lonely = make_resident(email="c@gahk.dk")

    assert services.notify([lonely.pk], category=categories.GENERAL, title="Hej", url=TARGET) == 1

    row = Notification.objects.get()
    assert row.in_app is True
    assert row.pushed is False
    assert pushes == []


def test_the_resident_who_caused_it_is_excluded(pushes: Recorded, two: tuple[Resident, Resident]) -> None:
    """Nobody is notified about their own post."""
    a, b = two

    reached = services.notify(
        Resident.objects.all(), category=categories.POSTS_NEW, title="t", url=TARGET, exclude=a.pk
    )

    assert reached == 1
    assert list(Notification.objects.values_list("recipient_id", flat=True)) == [b.pk]
    assert [ids for ids, _p, _t in pushes] == [[b.pk]]


def test_recipients_may_be_ids_and_duplicates_are_told_once(
    pushes: Recorded, two: tuple[Resident, Resident]
) -> None:
    """Features build audiences by union (organiser + co-organisers + invitees); a resident in two
    of them must not get two rows and two banners."""
    a, b = two

    assert services.notify([a.pk, b.pk, a.pk], category=categories.GENERAL, title="t", url=TARGET) == 2
    assert Notification.objects.count() == 2


def test_an_unknown_category_fails_loudly(pushes: Recorded, two: tuple[Resident, Resident]) -> None:
    """A typo at a call site must not notify under a category no settings toggle controls."""
    with pytest.raises(KeyError):
        services.notify([two[0].pk], category="posts.nwe", title="t", url=TARGET)
    assert not Notification.objects.exists()


def test_importance_defaults_to_the_category_and_can_be_raised(
    pushes: Recorded, two: tuple[Resident, Resident]
) -> None:
    a, _b = two

    services.notify([a.pk], category=categories.CHAT_LIKES, title="like", url=TARGET)
    services.notify(
        [a.pk], category=categories.GENERAL, title="vigtigt", url=TARGET, importance=Importance.HOEJ
    )

    assert Notification.objects.get(title="like").importance == Importance.LAV
    assert Notification.objects.get(title="vigtigt").importance == Importance.HOEJ
    assert [p["importance"] for _i, p, _t in pushes] == [1, 3]


# --- notify: preferences ------------------------------------------------------------------------


def test_in_app_off_still_pushes_and_hides_the_row(pushes: Recorded, two: tuple[Resident, Resident]) -> None:
    """The row still exists (it is what tapping the push marks seen, and what the statistics count),
    but it is not in the dropdown, the page or the badge."""
    a, b = two
    prefer(a, categories.POSTS_NEW, in_app=False)

    services.notify([a.pk, b.pk], category=categories.POSTS_NEW, title="t", url=TARGET)

    row = Notification.objects.get(recipient=a)
    assert row.in_app is False
    assert row.pushed is True
    assert not Notification.objects.filter(recipient=a).visible().exists()
    assert services.unseen_count(a.pk) == 0
    assert pushes[0][0] == sorted([a.pk, b.pk])


def test_push_off_keeps_the_row_and_skips_the_device(
    pushes: Recorded, two: tuple[Resident, Resident]
) -> None:
    a, b = two
    prefer(a, categories.POSTS_NEW, push=False)

    services.notify([a.pk, b.pk], category=categories.POSTS_NEW, title="t", url=TARGET)

    row = Notification.objects.get(recipient=a)
    assert row.in_app is True
    assert row.pushed is False
    assert [ids for ids, _p, _t in pushes] == [[b.pk]]


def test_both_off_means_nothing_at_all(pushes: Recorded, two: tuple[Resident, Resident]) -> None:
    """A resident who turned a category fully off is not "reached", and has no row to see."""
    a, b = two
    prefer(a, categories.POSTS_NEW, in_app=False, push=False)

    reached = services.notify([a.pk, b.pk], category=categories.POSTS_NEW, title="t", url=TARGET)

    assert reached == 1
    assert not Notification.objects.filter(recipient=a).exists()


def test_a_preference_only_applies_to_its_own_category(
    pushes: Recorded, two: tuple[Resident, Resident]
) -> None:
    a, _b = two
    prefer(a, categories.POSTS_NEW, in_app=False, push=False)

    services.notify([a.pk], category=categories.POSTS_REPLIES, title="t", url=TARGET)

    row = Notification.objects.get(recipient=a)
    assert (row.in_app, row.pushed) == (True, True)


def test_category_vibrate_off_puts_the_resident_in_the_silent_group(
    pushes: Recorded, two: tuple[Resident, Resident]
) -> None:
    """Vibrating and silent residents need different payloads — two fan-outs, never one per
    resident."""
    a, b = two
    prefer(a, categories.POSTS_NEW, vibrate=False)

    services.notify([a.pk, b.pk], category=categories.POSTS_NEW, title="t", url=TARGET)

    by_vibrate = {payload["vibrate"]: ids for ids, payload, _ttl in pushes}
    assert len(pushes) == 2
    assert by_vibrate == {True: [b.pk], False: [a.pk]}
    # Same batch in both: the silent push still opens (and marks seen) the resident's row.
    assert pushes[0][1]["open"] == pushes[1][1]["open"]


def test_global_vibrate_off_silences_every_category(pushes: Recorded, two: tuple[Resident, Resident]) -> None:
    a, b = two
    NotificationSettings.objects.create(resident=a, vibrate=False)

    services.notify([a.pk, b.pk], category=categories.EVENTS_CHANGES, title="t", url=TARGET)

    assert {payload["vibrate"]: ids for ids, payload, _ttl in pushes} == {True: [b.pk], False: [a.pk]}


def test_vibrate_min_importance_silences_only_what_is_below_it(
    pushes: Recorded, two: tuple[Resident, Resident]
) -> None:
    """ "Don't vibrate for likes": a NORMAL push arrives silently, a HØJ one still vibrates."""
    a, _b = two
    NotificationSettings.objects.create(resident=a, vibrate_min_importance=Importance.HOEJ)

    services.notify([a.pk], category=categories.POSTS_NEW, title="normal", url=TARGET)
    services.notify([a.pk], category=categories.EVENTS_CHANGES, title="hoej", url=TARGET)

    assert [(p["head"], p["vibrate"]) for _ids, p, _ttl in pushes] == [("normal", False), ("hoej", True)]


def test_kritisk_overrides_every_preference(pushes: Recorded, two: tuple[Resident, Resident]) -> None:
    """The level's whole purpose: a resident who turned the category off — in-app, push and
    vibration, and vibration globally — still gets the row and a vibrating push."""
    a, _b = two
    prefer(a, categories.GENERAL, in_app=False, push=False, vibrate=False)
    NotificationSettings.objects.create(resident=a, vibrate=False, vibrate_min_importance=Importance.KRITISK)

    reached = services.notify(
        [a.pk], category=categories.GENERAL, title="Vandet lukkes", url=TARGET, importance=Importance.KRITISK
    )

    assert reached == 1
    row = Notification.objects.get(recipient=a)
    assert (row.in_app, row.pushed, row.importance) == (True, True, Importance.KRITISK)
    assert [(ids, p["vibrate"], p["importance"]) for ids, p, _t in pushes] == [([a.pk], True, 4)]


# --- notify: push-only categories ---------------------------------------------------------------


def test_a_room_category_makes_rows_only_for_device_owners_with_push_on(
    pushes: Recorded, make_resident: Callable[..., Resident]
) -> None:
    """A Den Hurtige room is push only: the chat itself is the in-app copy. So the only residents
    with a row are those a push actually reaches — no device, or the room muted, means no row."""
    reached_one = make_resident(email="a@gahk.dk")
    no_device = make_resident(email="b@gahk.dk")
    muted = make_resident(email="c@gahk.dk")
    device(reached_one)
    device(muted)
    prefer(muted, notification_category("tv-rezz"), push=False)

    reached = services.notify(
        [reached_one.pk, no_device.pk, muted.pk],
        category=notification_category("tv-rezz"),
        title="Anders",
        body="Fodbold?",
        url="/intern/den-hurtige/tv-rezz/",
    )

    assert reached == 1
    row = Notification.objects.get()
    assert row.recipient_id == reached_one.pk
    assert (row.in_app, row.pushed) == (False, True)
    assert [ids for ids, _p, _t in pushes] == [[reached_one.pk]]


def test_push_only_rows_stay_out_of_the_in_app_lists(
    pushes: Recorded, client: Client, two: tuple[Resident, Resident]
) -> None:
    """Not in the badge, the dropdown or the page — a feed of every chat message would bury the
    notifications that are actually about you."""
    a, _b = two
    services.notify([a.pk], category=notification_category("generelt"), title="Rum-besked", url="/x/")
    client.force_login(a)

    assert services.unseen_count(a.pk) == 0
    assert "Rum-besked" not in client.get(reverse("notifications:index")).content.decode()
    assert "Rum-besked" not in client.get(reverse("notifications:dropdown")).content.decode()


def test_without_vapid_keys_push_only_categories_produce_nothing(
    monkeypatch: pytest.MonkeyPatch, settings: object, make_resident: Callable[..., Resident]
) -> None:
    """The dev state: no push. In-app categories still write rows (marked not pushed); a push-only
    category writes none, rather than rows nobody could ever see."""
    settings.VAPID_PUBLIC_KEY = ""  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = ""  # type: ignore[attr-defined]
    monkeypatch.setattr(push, "_dispatch", lambda *a, **k: pytest.fail("no push without keys"))
    a = make_resident(email="a@gahk.dk")
    device(a)

    assert services.notify([a.pk], category=notification_category("generelt"), title="t", url="/x/") == 0
    assert services.notify([a.pk], category=categories.GENERAL, title="t", url="/x/") == 1
    assert Notification.objects.get().pushed is False


# --- TTL ----------------------------------------------------------------------------------------


def test_the_default_ttl_is_seven_days(pushes: Recorded, two: tuple[Resident, Resident]) -> None:
    """On the row (it leaves the lists then) AND on the push (the service drops it then)."""
    a, _b = two
    before = timezone.now()

    services.notify([a.pk], category=categories.GENERAL, title="t", url=TARGET)

    row = Notification.objects.get()
    assert (
        before + datetime.timedelta(days=7) <= row.expires_at <= timezone.now() + datetime.timedelta(days=7)
    )
    ttl = pushes[0][2]
    assert 7 * 24 * 3600 - 60 <= ttl <= 7 * 24 * 3600


def test_a_chat_message_lives_exactly_as_long_as_the_message(
    pushes: Recorded, two: tuple[Resident, Resident]
) -> None:
    """A time-limited Den Hurtige message: its notification must not outlive it, on the device or in
    the app."""
    a, _b = two
    expires = timezone.now() + datetime.timedelta(hours=2)

    services.notify(
        [a.pk], category=notification_category("generelt"), title="t", url="/x/", expires_at=expires
    )

    assert Notification.objects.get().expires_at == expires
    assert 2 * 3600 - 60 <= pushes[0][2] <= 2 * 3600


def test_an_already_expired_notification_is_not_sent(
    pushes: Recorded, two: tuple[Resident, Resident]
) -> None:
    a, _b = two

    reached = services.notify(
        [a.pk],
        category=categories.GENERAL,
        title="t",
        url=TARGET,
        expires_at=timezone.now() - datetime.timedelta(seconds=1),
    )

    assert reached == 0
    assert not Notification.objects.exists()
    assert pushes == []


def test_an_expired_row_leaves_the_lists_at_once(make_resident: Callable[..., Resident]) -> None:
    """Expiry is a comparison, not a sweep — no waiting for the nightly purge."""
    a = make_resident(email="a@gahk.dk")
    make_notification(a, title="gammel", expires_at=timezone.now() - datetime.timedelta(seconds=1))
    make_notification(a, title="ny")

    assert list(Notification.objects.filter(recipient=a).visible().values_list("title", flat=True)) == ["ny"]
    assert services.unseen_count(a.pk) == 1


# --- notify_grouped -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "local_now",
    [
        datetime.datetime(2026, 10, 20, 12, 0),  # the week before winter time (25 October 2026)
        datetime.datetime(2027, 3, 24, 12, 0),  # the week before summer time (28 March 2027)
    ],
)
def test_the_ttl_is_seven_days_across_a_change_of_clock(
    pushes: Recorded,
    two: tuple[Resident, Resident],
    monkeypatch: pytest.MonkeyPatch,
    local_now: datetime.datetime,
) -> None:
    """Seven days of elapsed time, not seven days on the wall clock, which are an hour off across
    a change to or from summer time. Pinned to such weeks, so it does not depend on today's date."""
    a, b = two
    now = timezone.make_aware(local_now)
    monkeypatch.setattr(services, "current_datetime", lambda: now)

    services.notify([a.pk], category=categories.GENERAL, title="t", url=TARGET)
    services.notify_grouped(
        b.pk, category=categories.POSTS_LIKES, group_key="g", actor=a, verb="reagerede", url=TARGET
    )

    for row in Notification.objects.all():
        assert row.expires_at - now == datetime.timedelta(days=7)


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["Anders"], "Anders reagerede"),
        (["Bente", "Anders"], "Bente og Anders reagerede"),
        (["Carl", "Bente", "Anders"], "3 personer reagerede"),
    ],
)
def test_grouped_title(names: list[str], expected: str) -> None:
    actors = [{"id": i, "name": name} for i, name in enumerate(names)]

    assert services.grouped_title(actors, "reagerede") == expected


def test_grouping_rewrites_one_row(pushes: Recorded, make_resident: Callable[..., Resident]) -> None:
    """The spec's example: A liked it, then B — the first notification is REPLACED by "B og A…",
    and a third makes it a count. One row throughout, newest actor first, unseen again each time."""
    author = make_resident(email="author@gahk.dk")
    device(author)
    anders = make_resident(email="a@gahk.dk", first_name="Anders", last_name="And")
    bente = make_resident(email="b@gahk.dk", first_name="Bente", last_name="Bo")
    carl = make_resident(email="c@gahk.dk", first_name="Carl", last_name="Cykel")

    def like(actor: Resident) -> Notification | None:
        return services.notify_grouped(
            author.pk,
            category=categories.CHAT_LIKES,
            group_key="chat.likes:7",
            actor=actor,
            verb="reagerede på din besked",
            url="/intern/den-hurtige/?traad=7",
        )

    first = like(anders)
    assert first is not None
    assert first.title == "Anders And reagerede på din besked"
    services.mark_seen(Notification.objects.filter(pk=first.pk))

    second = like(bente)
    assert second is not None and second.pk == first.pk
    second.refresh_from_db()
    assert second.title == "Bente Bo og Anders And reagerede på din besked"
    assert second.seen_at is None, "a new actor makes it news again"
    assert [a["id"] for a in second.actors] == [bente.pk, anders.pk]

    like(carl)
    row = Notification.objects.get()  # still exactly one row
    assert row.title == "3 personer reagerede på din besked"
    assert row.group_key == "chat.likes:7"
    assert row.category == categories.CHAT_LIKES
    assert row.importance == Importance.LAV

    # Every push carries the group key as its tag, so the device replaces the banner too.
    assert [p["tag"] for _i, p, _t in pushes] == ["chat.likes:7"] * 3
    assert [p["head"] for _i, p, _t in pushes][-1] == "3 personer reagerede på din besked"
    assert pushes[-1][1]["open"] == f"/intern/notifikationer/aabn/{row.batch}/"


def test_the_same_actor_again_moves_to_the_front_without_counting_twice(
    pushes: Recorded, make_resident: Callable[..., Resident]
) -> None:
    author = make_resident(email="author@gahk.dk")
    anders = make_resident(email="a@gahk.dk", first_name="Anders", last_name="And")
    bente = make_resident(email="b@gahk.dk", first_name="Bente", last_name="Bo")
    for actor in (anders, bente, anders):
        services.notify_grouped(
            author.pk,
            category=categories.POSTS_REPLIES,
            group_key="posts.replies:1",
            actor=actor,
            verb="svarede",
            url=TARGET,
        )

    row = Notification.objects.get()
    assert row.title == "Anders And og Bente Bo svarede"


def test_acting_on_your_own_post_notifies_nobody(
    pushes: Recorded, make_resident: Callable[..., Resident]
) -> None:
    author = make_resident(email="author@gahk.dk")
    device(author)

    result = services.notify_grouped(
        author.pk,
        category=categories.CHAT_LIKES,
        group_key="chat.likes:1",
        actor=author,
        verb="reagerede på din besked",
        url=TARGET,
    )

    assert result is None
    assert not Notification.objects.exists()
    assert pushes == []


def test_separate_group_keys_are_separate_rows(
    pushes: Recorded, make_resident: Callable[..., Resident]
) -> None:
    author = make_resident(email="author@gahk.dk")
    anders = make_resident(email="a@gahk.dk")
    for key in ("chat.likes:1", "chat.likes:2"):
        services.notify_grouped(
            author.pk, category=categories.CHAT_LIKES, group_key=key, actor=anders, verb="v", url=TARGET
        )

    assert Notification.objects.count() == 2


def test_an_expired_group_is_revived_with_a_fresh_actor_list(
    pushes: Recorded, make_resident: Callable[..., Resident]
) -> None:
    """Last week's likers are not news. The row is reused (the unique constraint holds over expired
    rows too) but starts over."""
    author = make_resident(email="author@gahk.dk")
    anders = make_resident(email="a@gahk.dk", first_name="Anders", last_name="And")
    bente = make_resident(email="b@gahk.dk", first_name="Bente", last_name="Bo")
    kwargs = {"category": categories.CHAT_LIKES, "group_key": "chat.likes:1", "verb": "v", "url": TARGET}
    services.notify_grouped(author.pk, actor=anders, **kwargs)  # type: ignore[arg-type]
    Notification.objects.update(expires_at=timezone.now() - datetime.timedelta(minutes=1))

    services.notify_grouped(author.pk, actor=bente, **kwargs)  # type: ignore[arg-type]

    row = Notification.objects.get()
    assert row.title == "Bente Bo v"
    assert row.expires_at > timezone.now()


def test_grouped_respects_preferences(pushes: Recorded, make_resident: Callable[..., Resident]) -> None:
    """Grouping goes through the same decisions as notify: likes fully off means no row."""
    author = make_resident(email="author@gahk.dk")
    device(author)
    prefer(author, categories.CHAT_LIKES, in_app=False, push=False)

    result = services.notify_grouped(
        author.pk,
        category=categories.CHAT_LIKES,
        group_key="chat.likes:1",
        actor=make_resident(email="a@gahk.dk"),
        verb="v",
        url=TARGET,
    )

    assert result is None
    assert not Notification.objects.exists()
    assert pushes == []


# --- seen / unseen ------------------------------------------------------------------------------


def test_mark_seen_stamps_only_what_was_unseen(make_resident: Callable[..., Resident]) -> None:
    """Re-marking must not move the "seen at" of something seen yesterday."""
    a = make_resident(email="a@gahk.dk")
    earlier = timezone.now() - datetime.timedelta(days=1)
    old = make_notification(a, seen_at=earlier)
    make_notification(a)

    assert services.mark_seen(Notification.objects.filter(recipient=a)) == 1

    old.refresh_from_db()
    assert old.seen_at == earlier
    assert not Notification.objects.unseen().exists()


def test_unseen_count_is_the_residents_own_visible_unseen_rows(
    make_resident: Callable[..., Resident],
) -> None:
    a = make_resident(email="a@gahk.dk")
    b = make_resident(email="b@gahk.dk")
    make_notification(a)
    make_notification(a)
    make_notification(a, seen_at=timezone.now())
    make_notification(a, in_app=False)  # push only
    make_notification(a, expires_at=timezone.now() - datetime.timedelta(seconds=1))
    make_notification(b)

    assert services.unseen_count(a.pk) == 2


# --- the category registry ----------------------------------------------------------------------


def test_a_registered_category_needs_a_free_key_and_a_group_the_settings_page_draws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(categories, "_registered", list(categories._registered))
    monkeypatch.setattr(categories, "_by_key", dict(categories._by_key))

    with pytest.raises(ValueError, match="already registered"):
        categories.register([categories.Category(categories.GENERAL, "Endnu en", "general")])
    with pytest.raises(ValueError, match="unknown group"):
        categories.register([categories.Category("x.y", "X", "nowhere")])


def test_registering_the_same_category_twice_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """AppConfig.ready() can run more than once."""
    monkeypatch.setattr(categories, "_registered", list(categories._registered))
    monkeypatch.setattr(categories, "_by_key", dict(categories._by_key))
    room = categories.get(notification_category("tv-rezz"))

    categories.register([room])

    assert categories.all_categories().count(room) == 1


def test_registered_categories_list_first_in_their_group() -> None:
    """Den Hurtige's rooms above "Svar på dine beskeder", as on the settings page before they were
    registered rather than built here."""
    [chat] = [leaves for key, _name, leaves in categories.grouped() if key == "chat"]

    assert [c.key for c in chat[-2:]] == [categories.CHAT_REPLIES, categories.CHAT_LIKES]
    assert all(c.key.startswith("chat.room.") for c in chat[:-2])


# --- views: the list, the dropdown, opening -----------------------------------------------------


def test_the_pages_require_login(client: Client) -> None:
    for name in ("index", "dropdown", "preferences"):
        response = client.get(reverse(f"notifications:{name}"))
        assert response.status_code == 302, name
        assert "/intern/admin/login" in response["Location"]


def test_index_lists_own_active_rows_most_important_first_then_newest(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """A cancelled event outranks a newer like — the ordering importance levels exist for."""
    a = make_resident(email="a@gahk.dk")
    now = timezone.now()
    make_notification(a, title="gammel", updated_at=now - datetime.timedelta(hours=2))
    make_notification(a, title="ny", updated_at=now)
    make_notification(
        a, title="aflyst", importance=Importance.HOEJ, updated_at=now - datetime.timedelta(days=1)
    )
    make_notification(a, title="udløbet", expires_at=now - datetime.timedelta(seconds=1))
    make_notification(make_resident(email="b@gahk.dk"), title="andres")
    client.force_login(a)

    response = client.get(reverse("notifications:index"))

    assert response.status_code == 200
    assert [r.title for r in response.context["rows"]] == ["aflyst", "ny", "gammel"]
    assert response.context["view"] == "kronologisk"
    assert response.context["unseen"] == 3
    html = response.content.decode()
    assert "andres" not in html
    assert "udløbet" not in html
    assert reverse("notifications:open", args=[response.context["rows"][0].pk]) in html


def test_index_by_category_groups_in_display_order(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    a = make_resident(email="a@gahk.dk")
    make_notification(a, title="event", category=categories.EVENTS_NEW)
    make_notification(a, title="post", category=categories.POSTS_NEW)
    make_notification(a, title="reply", category=categories.POSTS_REPLIES)
    make_notification(a, title="retired", category="something.retired")
    client.force_login(a)

    response = client.get(reverse("notifications:index"), {"visning": "kategori"})

    assert response.context["view"] == "kategori"
    groups = [(name, sorted(r.title for r in rows)) for name, rows in response.context["groups"]]
    # A category retired since the row was written counts as general rather than erroring.
    # Generelt (and system) come last: the feature groups are what people scan for.
    assert groups == [
        ("Opslagstavlen", ["post", "reply"]),
        ("Begivenheder", ["event"]),
        ("Generelt", ["retired"]),
    ]


def test_an_unknown_view_falls_back_to_chronological(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    client.force_login(make_resident(email="a@gahk.dk"))

    assert client.get(reverse("notifications:index"), {"visning": "x"}).context["view"] == "kronologisk"


def test_the_dropdown_shows_the_ten_newest(client: Client, make_resident: Callable[..., Resident]) -> None:
    a = make_resident(email="a@gahk.dk")
    now = timezone.now()
    for i in range(12):
        make_notification(a, title=f"n{i}", updated_at=now - datetime.timedelta(minutes=i))
    client.force_login(a)

    response = client.get(reverse("notifications:dropdown"))

    assert response.status_code == 200
    assert [r.title for r in response.context["rows"]] == [f"n{i}" for i in range(10)]


def test_open_marks_seen_and_redirects_to_the_target(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    a = make_resident(email="a@gahk.dk")
    row = make_notification(a, url="/intern/begivenheder/3/")
    client.force_login(a)

    response = client.get(reverse("notifications:open", args=[row.pk]))

    assert response.status_code == 302
    assert response["Location"] == "/intern/begivenheder/3/"
    row.refresh_from_db()
    assert row.seen_at is not None


def test_open_refuses_someone_elses_notification(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    row = make_notification(make_resident(email="a@gahk.dk"))
    client.force_login(make_resident(email="b@gahk.dk"))

    assert client.get(reverse("notifications:open", args=[row.pk])).status_code == 404
    row.refresh_from_db()
    assert row.seen_at is None


@pytest.mark.parametrize("url", ["//evil.example/", "https://evil.example/", "/\\evil.example/"])
def test_open_never_becomes_an_open_redirect(
    client: Client, make_resident: Callable[..., Resident], url: str
) -> None:
    a = make_resident(email="a@gahk.dk")
    row = make_notification(a, url=url)
    client.force_login(a)

    response = client.get(reverse("notifications:open", args=[row.pk]))

    assert response["Location"] == "/intern/notifikationer/"


def test_open_batch_marks_the_tapping_residents_own_row(
    pushes: Recorded, client: Client, two: tuple[Resident, Resident]
) -> None:
    """The push payload is shared by the whole fan-out; tapping it marks only YOUR row seen."""
    a, b = two
    services.notify([a.pk, b.pk], category=categories.POSTS_NEW, title="t", url="/intern/opslagstavle/9/")
    open_url = pushes[0][1]["open"]
    client.force_login(a)

    response = client.get(open_url)

    assert response.status_code == 302
    assert response["Location"] == "/intern/opslagstavle/9/"
    assert Notification.objects.get(recipient=a).seen_at is not None
    assert Notification.objects.get(recipient=b).seen_at is None


def test_open_batch_also_marks_a_push_only_row(
    pushes: Recorded, client: Client, two: tuple[Resident, Resident]
) -> None:
    """A room message has no in-app row to click — the push is the only way it is ever seen."""
    a, _b = two
    services.notify([a.pk], category=notification_category("generelt"), title="t", url="/intern/den-hurtige/")
    client.force_login(a)

    response = client.get(pushes[0][1]["open"])

    assert response["Location"] == "/intern/den-hurtige/"
    assert Notification.objects.get().seen_at is not None


def test_open_batch_without_a_row_lands_on_the_list(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Purged, or a push meant for whoever owned this browser before: no error, just the list."""
    client.force_login(make_resident(email="a@gahk.dk"))

    response = client.get(reverse("notifications:open_batch", args=[uuid.uuid4()]))

    assert response.status_code == 302
    assert response["Location"] == reverse("notifications:index")


def test_mark_all_seen_marks_only_own_visible_rows(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    a = make_resident(email="a@gahk.dk")
    b = make_resident(email="b@gahk.dk")
    make_notification(a)
    make_notification(a)
    others = make_notification(b)
    client.force_login(a)

    assert client.get(reverse("notifications:mark_all_seen")).status_code == 405
    response = client.post(reverse("notifications:mark_all_seen"))

    assert response.status_code == 302
    assert services.unseen_count(a.pk) == 0
    others.refresh_from_db()
    assert others.seen_at is None


def test_the_bell_badge_counts_unseen(client: Client, make_resident: Callable[..., Resident]) -> None:
    a = make_resident(email="a@gahk.dk")
    make_notification(a)
    make_notification(a)
    client.force_login(a)

    html = client.get(reverse("notifications:index")).content.decode()

    assert '<span class="notif-badge">2</span>' in html


# --- views: preferences -------------------------------------------------------------------------


def _all_on() -> dict[str, str]:
    """The POST of the settings form with every box ticked — the defaults."""
    data = {"vibrate": "on", "vibrate_min_importance": str(int(Importance.LAV))}
    for category in categories.all_categories():
        if category.in_app and category.in_app_optional:
            data[f"{category.key}__in_app"] = "on"
        data[f"{category.key}__push"] = "on"
        data[f"{category.key}__vibrate"] = "on"
    return data


def test_preferences_page_renders_every_category_and_room_anchor(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Each chat room's indicator links to `#rum-<slug>`, so the anchor must be there."""
    client.force_login(make_resident(email="a@gahk.dk"))

    response = client.get(reverse("notifications:preferences"))

    assert response.status_code == 200
    html = response.content.decode()
    for category in categories.all_categories():
        assert f'name="{category.key}__push"' in html
        optional = category.in_app and category.in_app_optional
        assert (f'name="{category.key}__in_app"' in html) is optional
    assert 'id="rum-tv-rezz"' in html


def test_system_and_general_come_last_on_the_settings_page(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    client.force_login(make_resident(email="a@gahk.dk"))

    groups = [g["key"] for g in client.get(reverse("notifications:preferences")).context["groups"]]

    assert groups[-2:] == ["system", "general"]


@pytest.mark.parametrize("key", [categories.SYSTEM, categories.GENERAL])
def test_system_and_general_cannot_be_turned_off_in_app(
    client: Client, make_resident: Callable[..., Resident], key: str
) -> None:
    """The box is drawn ticked and disabled; a hand-made POST without it is stored as on anyway."""
    a = make_resident(email="a@gahk.dk")
    client.force_login(a)
    data = {k: v for k, v in _all_on().items() if k != f"{key}__push"}
    data.pop(f"{key}__in_app", None)

    client.post(reverse("notifications:preferences"), data)

    pref = NotificationPreference.objects.get(resident=a, category=key)
    assert pref.in_app is True
    assert pref.push is False


@pytest.mark.parametrize("key", [categories.SYSTEM, categories.GENERAL])
def test_an_old_in_app_off_row_for_system_or_general_is_ignored(
    pushes: Recorded, two: tuple[Resident, Resident], key: str
) -> None:
    """A preference row saved before in-app was locked must not hide the house's own messages."""
    a, _b = two
    prefer(a, key, in_app=False)

    services.notify([a.pk], category=key, title="t", url=TARGET)

    assert Notification.objects.get(recipient=a).in_app is True
    assert services.unseen_count(a.pk) == 1


def test_saving_the_defaults_stores_nothing(client: Client, make_resident: Callable[..., Resident]) -> None:
    """Preferences are exceptions: a category at all-on has no row."""
    a = make_resident(email="a@gahk.dk")
    client.force_login(a)

    response = client.post(reverse("notifications:preferences"), _all_on())

    assert response.status_code == 302
    assert not NotificationPreference.objects.exists()
    s = NotificationSettings.objects.get(resident=a)
    assert (s.vibrate, s.vibrate_min_importance) == (True, Importance.LAV)


def test_preferences_round_trip(client: Client, make_resident: Callable[..., Resident]) -> None:
    """Unticking stores one row for that category only; the page shows it unticked; ticking it
    again deletes the row rather than storing an all-on one."""
    a = make_resident(email="a@gahk.dk")
    client.force_login(a)
    room = notification_category("mhga")
    data = _all_on()
    del data[f"{categories.POSTS_NEW}__push"]
    del data[f"{categories.CHAT_LIKES}__in_app"]
    del data[f"{room}__push"]
    del data["vibrate"]
    data["vibrate_min_importance"] = str(int(Importance.HOEJ))

    client.post(reverse("notifications:preferences"), data)

    stored = {
        p.category: (p.in_app, p.push, p.vibrate) for p in NotificationPreference.objects.filter(resident=a)
    }
    assert stored == {
        categories.POSTS_NEW: (True, False, True),
        categories.CHAT_LIKES: (False, True, True),
        # A push-only room has no in-app toggle; it is stored as on, not as off.
        room: (True, False, True),
    }
    s = NotificationSettings.objects.get(resident=a)
    assert (s.vibrate, s.vibrate_min_importance) == (False, Importance.HOEJ)

    page = client.get(reverse("notifications:preferences"))
    rows = {r["category"].key: r for g in page.context["groups"] for r in g["rows"]}
    assert rows[categories.POSTS_NEW]["push"] is False
    assert rows[categories.CHAT_LIKES]["in_app"] is False
    assert rows[categories.POSTS_REPLIES]["push"] is True
    assert page.context["settings"].vibrate is False

    client.post(reverse("notifications:preferences"), _all_on())

    assert not NotificationPreference.objects.filter(resident=a).exists()


def test_a_bogus_importance_falls_back_to_vibrating_for_everything(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    a = make_resident(email="a@gahk.dk")
    client.force_login(a)

    client.post(reverse("notifications:preferences"), {**_all_on(), "vibrate_min_importance": "99"})

    assert NotificationSettings.objects.get(resident=a).vibrate_min_importance == Importance.LAV


def test_preferences_saved_on_the_page_steer_delivery(
    pushes: Recorded, client: Client, two: tuple[Resident, Resident]
) -> None:
    """End to end: muting a room on the settings page is what stops that room's pushes."""
    a, b = two
    data = _all_on()
    del data[f"{notification_category('tv-rezz')}__push"]
    client.force_login(a)
    client.post(reverse("notifications:preferences"), data)

    services.notify([a.pk, b.pk], category=notification_category("tv-rezz"), title="t", url="/x/")

    assert [ids for ids, _p, _t in pushes] == [[b.pk]]


# --- siteadmin ----------------------------------------------------------------------------------


@pytest.fixture
def admin(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(
        email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,), first_name="Ada", last_name="Min"
    )


def test_the_admin_page_is_for_administrators_only(
    client: Client, make_resident: Callable[..., Resident], admin: Resident
) -> None:
    url = reverse("siteadmin:notifications")

    assert client.get(url).status_code == 302  # anonymous → login
    client.force_login(make_resident(email="beboer@gahk.dk", roles=(Role.INSPEKTION,)))
    assert client.get(url).status_code == 403
    client.force_login(admin)
    assert client.get(url).status_code == 200


def test_the_admin_page_lists_devices_and_statistics(
    client: Client, make_resident: Callable[..., Resident], admin: Resident
) -> None:
    owner = make_resident(email="a@gahk.dk", first_name="Anders", last_name="And")
    device(owner)
    device(owner)
    make_notification(owner, category=categories.POSTS_NEW, pushed=True, seen_at=timezone.now())
    make_notification(owner, category=categories.POSTS_NEW)
    client.force_login(admin)

    response = client.get(reverse("siteadmin:notifications"))

    assert response.context["devices"] == 2
    assert response.context["device_users"] == 1
    week = response.context["windows"][0]
    assert (week["days"], week["total"], week["seen"], week["pushed"]) == (7, 2, 1, 1)
    assert week["by_category"][0]["name"] == "Nye opslag"
    assert "Anders And" in response.content.decode()


def test_an_administrator_can_cancel_a_device(
    client: Client, make_resident: Callable[..., Resident], admin: Resident
) -> None:
    target = device(make_resident(email="a@gahk.dk"))
    url = reverse("siteadmin:notification_device_cancel", args=[target.pk])
    client.force_login(admin)

    assert client.get(url).status_code == 405
    response = client.post(url)

    assert response.status_code == 302
    assert response["Location"] == reverse("siteadmin:notifications")
    assert not PushSubscription.objects.filter(pk=target.pk).exists()


def test_a_resident_cannot_cancel_a_device(client: Client, make_resident: Callable[..., Resident]) -> None:
    owner = make_resident(email="a@gahk.dk")
    target = device(owner)
    client.force_login(make_resident(email="b@gahk.dk"))

    response = client.post(reverse("siteadmin:notification_device_cancel", args=[target.pk]))

    assert response.status_code == 403
    assert PushSubscription.objects.filter(pk=target.pk).exists()


def test_broadcast_reaches_everyone(
    pushes: Recorded, client: Client, make_resident: Callable[..., Resident], admin: Resident
) -> None:
    """The broadcast's audience is services.everyone(); KRITISK goes through even to a resident who
    turned general messages off."""
    resident = make_resident(email="a@gahk.dk")
    device(resident)
    prefer(resident, categories.GENERAL, in_app=False, push=False)
    client.force_login(admin)

    response = client.post(
        reverse("siteadmin:notifications"),
        {
            "category": categories.GENERAL,
            "importance": str(int(Importance.KRITISK)),
            "title": "Vandet lukkes kl. 10",
            "body": "I hele huset.",
            "url": "/intern/",
        },
    )

    assert response.status_code == 302
    rows = Notification.objects.filter(title="Vandet lukkes kl. 10")
    assert set(rows.values_list("recipient_id", flat=True)) == {admin.pk, resident.pk}
    assert set(rows.values_list("importance", flat=True)) == {Importance.KRITISK}
    assert pushes[0][0] == [resident.pk]


@pytest.mark.parametrize("url", ["https://evil.example/", "//evil.example/", "/\\evil.example/"])
def test_broadcast_refuses_an_offsite_link(
    client: Client, admin: Resident, make_resident: Callable[..., Resident], url: str
) -> None:
    client.force_login(admin)

    response = client.post(
        reverse("siteadmin:notifications"),
        {"category": categories.GENERAL, "importance": "2", "title": "t", "url": url},
    )

    assert response.status_code == 200
    assert response.context["form"].errors["url"]
    assert not Notification.objects.exists()


# --- purge --------------------------------------------------------------------------------------


def test_purge_deletes_only_rows_long_expired(make_resident: Callable[..., Resident]) -> None:
    """Rows stop showing at expiry, but are kept 30 days longer for the admin statistics."""
    a = make_resident(email="a@gahk.dk")
    now = timezone.now()
    make_notification(a, title="lang tid", expires_at=now - datetime.timedelta(days=31))
    make_notification(a, title="for nylig", expires_at=now - datetime.timedelta(days=1))
    make_notification(a, title="aktiv")

    assert services.purge_expired() == 1
    assert set(Notification.objects.values_list("title", flat=True)) == {"for nylig", "aktiv"}


def test_the_purge_task_runs_the_purge(make_resident: Callable[..., Resident]) -> None:
    a = make_resident(email="a@gahk.dk")
    make_notification(a, expires_at=timezone.now() - datetime.timedelta(days=60))

    assert tasks.purge_expired_notifications() == 1
    assert not Notification.objects.exists()


# --- everyone() ---------------------------------------------------------------------------------


def test_everyone_is_residents_role_holders_device_owners_and_superusers(
    make_resident: Callable[..., Resident],
) -> None:
    """Anyone living here this period, anyone holding a role this period (administrators need not
    live here), superusers — and anyone with a push device, which is who the audience used to be.
    Inactive accounts never; and nobody with none of the above."""
    year, month = active_period()
    room = Room.objects.create(legacy_index=90, number=90, floor="stuen", side="mod gaden")
    living = make_resident(email="living@gahk.dk")
    Residency.objects.create(resident=living, room=room, year=year, month=month)
    role = make_resident(email="role@gahk.dk")
    RoleAssignment.objects.create(resident=role, role=Role.AK, year=year, month=month)
    owner = make_resident(email="device@gahk.dk")
    device(owner)
    superuser = make_resident(email="super@gahk.dk", is_superuser=True)

    moved_out = make_resident(email="former@gahk.dk")
    prev_year, prev_month = (year, month - 1) if month > 1 else (year - 1, 12)
    Residency.objects.create(
        resident=moved_out,
        room=Room.objects.create(legacy_index=91, number=91, floor="stuen", side="mod gaden"),
        year=prev_year,
        month=prev_month,
    )
    RoleAssignment.objects.create(resident=moved_out, role=Role.AK, year=prev_year, month=prev_month)
    deactivated = make_resident(email="inactive@gahk.dk", is_active=False)
    RoleAssignment.objects.create(resident=deactivated, role=Role.AK, year=year, month=month)
    make_resident(email="nobody@gahk.dk")

    assert active_period() == (year, month)
    assert set(services.everyone().values_list("email", flat=True)) == {
        "living@gahk.dk",
        "role@gahk.dk",
        "device@gahk.dk",
        "super@gahk.dk",
    }
    # And it is a usable audience as-is: no duplicates for a resident matching several rules.
    RoleAssignment.objects.create(resident=living, role=Role.AK, year=year, month=month)
    device(living)
    assert services.everyone().filter(pk=living.pk).count() == 1
    assert {owner.pk, superuser.pk} <= set(services.everyone().values_list("pk", flat=True))


# --- migration: ChannelMute → NotificationPreference --------------------------------------------
#
# The ChannelMute table no longer exists (den_hurtige 0010 drops it), so migrating the test database
# back and forth would mean a schema round-trip on SQLite inside a test transaction — slow, and the
# kind of test that breaks on unrelated migrations. The data function is what carries the meaning,
# so it is called directly with a minimal stand-in for the historical app registry: a ChannelMute
# whose rows the test chooses, and the real NotificationPreference.


def _migration() -> object:
    return importlib.import_module("notifications.migrations.0002_channel_mutes_to_preferences")


def _apps(mutes: list[SimpleNamespace], created: list[object] | None = None) -> SimpleNamespace:
    class ChannelMute:
        def __init__(self, resident_id: int, channel: str) -> None:
            self.resident_id = resident_id
            self.channel = channel

        objects = SimpleNamespace(
            all=lambda: mutes,
            bulk_create=lambda objs, ignore_conflicts=False: (created if created is not None else []).extend(
                objs
            ),
        )

    models = {
        ("den_hurtige", "ChannelMute"): ChannelMute,
        ("notifications", "NotificationPreference"): NotificationPreference,
    }
    return SimpleNamespace(get_model=lambda app, name: models[(app, name)])


def test_each_channel_mute_becomes_a_room_preference_with_push_off(
    make_resident: Callable[..., Resident],
) -> None:
    """A mute meant "no push from this room". In-app and vibration stay at the default: a room is
    push only, and the mute said nothing about vibration."""
    a = make_resident(email="a@gahk.dk")
    b = make_resident(email="b@gahk.dk")
    mutes = [
        SimpleNamespace(resident_id=a.pk, channel="tv-rezz"),
        SimpleNamespace(resident_id=a.pk, channel="mhga"),
        SimpleNamespace(resident_id=b.pk, channel="tv-rezz"),
    ]

    _migration().forwards(_apps(mutes), None)  # type: ignore[attr-defined]

    stored = {
        (p.resident_id, p.category): (p.in_app, p.push, p.vibrate)
        for p in NotificationPreference.objects.all()
    }
    assert stored == {
        (a.pk, notification_category("tv-rezz")): (True, False, True),
        (a.pk, notification_category("mhga")): (True, False, True),
        (b.pk, notification_category("tv-rezz")): (True, False, True),
    }


def test_the_migrated_mute_still_silences_the_room(
    pushes: Recorded, make_resident: Callable[..., Resident]
) -> None:
    """The point of the migration: the room a resident muted before stays quiet after."""
    a = make_resident(email="a@gahk.dk")
    b = make_resident(email="b@gahk.dk")
    device(a)
    device(b)
    _migration().forwards(_apps([SimpleNamespace(resident_id=a.pk, channel="gahkroom")]), None)  # type: ignore[attr-defined]

    services.notify([a.pk, b.pk], category=notification_category("gahkroom"), title="t", url="/x/")

    assert [ids for ids, _p, _t in pushes] == [[b.pk]]
    assert not Notification.objects.filter(recipient=a).exists()


def test_the_mute_migration_tolerates_an_existing_preference(make_resident: Callable[..., Resident]) -> None:
    """Re-running (or a row already there) must not crash the deploy on the unique constraint."""
    a = make_resident(email="a@gahk.dk")
    mutes = [SimpleNamespace(resident_id=a.pk, channel="tv-rezz")]

    _migration().forwards(_apps(mutes), None)  # type: ignore[attr-defined]
    _migration().forwards(_apps(mutes), None)  # type: ignore[attr-defined]

    assert NotificationPreference.objects.count() == 1


def test_the_mute_migration_reverses_to_channel_mutes(make_resident: Callable[..., Resident]) -> None:
    """Backwards turns only room preferences with push off back into mutes."""
    a = make_resident(email="a@gahk.dk")
    prefer(a, notification_category("tv-rezz"), push=False)
    prefer(a, notification_category("mhga"), vibrate=False)  # push on: not a mute
    prefer(a, categories.POSTS_NEW, push=False)  # not a room
    created: list[object] = []

    _migration().backwards(_apps([], created), None)  # type: ignore[attr-defined]

    assert [(m.resident_id, m.channel) for m in created] == [(a.pk, "tv-rezz")]  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_notifications_live_in_the_topbar_not_the_sidebar(make_resident: Callable[..., Resident]) -> None:
    """The bell is the one place to read them; their settings are in the avatar menu beside it."""
    c = Client()
    c.force_login(make_resident(email="menu@gahk.dk"))

    response = c.get("/intern/")
    labels = [label for _section, items in response.context["nav_intern"] for _url, label, _icon in items]
    body = response.content.decode()

    assert "Notifikationer" not in labels
    assert 'class="notif-bell"' in body
    menu = body[body.index('id="user-menu-panel"') :]
    assert reverse("notifications:preferences") in menu
    assert 'action="/intern/admin/logout"' in menu
    assert "<noscript>" in body  # logging out never depends on the menu's JS


@pytest.mark.django_db
def test_the_notifications_page_reloads_when_restored_by_back(make_resident: Callable[..., Resident]) -> None:
    """Otherwise the browser's back-forward cache shows a notification just opened as still unread."""
    c = Client()
    c.force_login(make_resident(email="back@gahk.dk"))
    assert "event.persisted" in c.get(reverse("notifications:index")).content.decode()
