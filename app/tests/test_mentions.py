"""@mentions (core/mentions.py): residents and embedsgrupper named in posts and comments across
opslagstavlen, Den Hurtige, Reparationer and begivenheder."""

import datetime
from collections.abc import Callable

import pytest
from django.test import Client
from django.utils import timezone

from core import push
from core.links import linkify
from core.markdown import plain_text, render_markdown
from core.mentions import mentioned_groups, mentioned_ids, to_plain
from core.models import Room, Workgroup
from den_hurtige import access as den_hurtige_access
from den_hurtige.models import QuickPost
from den_hurtige.views import VALID_DURATIONS
from events import access as events_access
from events.models import MIN_INVITEES, Event, EventInvite, Visibility
from notifications import categories
from notifications.models import Notification
from opslagstavle import access as opslagstavle_access
from opslagstavle.models import Category, Notice
from reparationer.models import RepairTask
from residents.models import Residency, Resident, active_period


@pytest.fixture(autouse=True)
def _open_and_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    for access in (opslagstavle_access, den_hurtige_access, events_access):
        monkeypatch.setattr(access, "ACCESS_ROLES", None)
    monkeypatch.setattr(push, "_run_in_background", lambda fn: fn())


@pytest.fixture
def live_here(make_resident: Callable[..., Resident]) -> Callable[..., Resident]:
    """A resident on the active alumneliste, optionally in an embedsgruppe."""
    rooms = iter(range(500, 600))

    def _make(email: str, first_name: str = "Test", workgroup: Workgroup | None = None) -> Resident:
        resident = make_resident(email=email, first_name=first_name)
        n = next(rooms)
        room = Room.objects.create(legacy_index=n, number=n, floor="stuen", side="mod gaden")
        year, month = active_period()
        Residency.objects.create(resident=resident, room=room, year=year, month=month, workgroup=workgroup)
        return resident

    return _make


def told() -> list[int]:
    return sorted(
        Notification.objects.filter(category=categories.MENTIONS).values_list("recipient_id", flat=True)
    )


def token(resident: Resident) -> str:
    return f"@[{resident.full_name}]({resident.pk})"


# --- parsing and rendering ------------------------------------------------------------------------


def test_tokens_parse_into_residents_and_groups() -> None:
    text = "Hej @[Anders](4) og @[Inspektionen](g7), @[ikke] (5) og @Bo"
    assert mentioned_ids(text) == {4}
    assert mentioned_groups(text) == {7}
    assert to_plain(text) == "Hej @Anders og @Inspektionen, @[ikke] (5) og @Bo"


def test_plain_text_renders_mentions_escaped() -> None:
    html = linkify("@[<script>x</script>](4) og @[Grp](g7) https://dr.dk")
    assert (
        '<a data-mention="resident" href="/intern/beboer/4/profil">@&lt;script&gt;x&lt;/script&gt;</a>'
        in html
    )
    assert '<span data-mention="group">@Grp</span>' in html
    assert ">dr.dk</a>" in html
    assert "<script>" not in html


def test_markdown_renders_mentions_and_keeps_other_classes_out() -> None:
    html = render_markdown("**Hej** @[Anders](4) og @[Grp](g7)")
    assert 'href="/intern/beboer/4/profil" data-mention="resident"' in html
    assert '<span data-mention="group">@Grp</span>' in html
    assert plain_text("Hej @[Anders](4)") == "Hej @Anders"
    # The author cannot smuggle a marker or a span of their own through.
    html = render_markdown('<span data-mention="group">x</span> [a](https://a.dk)')
    assert "<span" not in html
    assert '<a href="https://a.dk" rel' in html


# --- the picker's lookup --------------------------------------------------------------------------


def test_lookup_offers_current_residents_and_groups_only(
    client: Client, make_resident: Callable[..., Resident], live_here: Callable[..., Resident]
) -> None:
    group = Workgroup.objects.create(name="Testgruppen")
    anders = live_here("a@gahk.dk", "Anders", workgroup=group)
    make_resident(email="former@gahk.dk", first_name="Andreas")  # not on this month's list
    empty = Workgroup.objects.create(name="Tomgruppen")  # nobody in it this month

    client.force_login(anders)
    data = client.get("/intern/beboere/naevn", {"q": "and"}).json()
    assert [r["id"] for r in data["residents"]] == [anders.pk]

    data = client.get("/intern/beboere/naevn", {"q": "gruppen"}).json()
    assert data["groups"] == [{"id": group.pk, "name": "Testgruppen", "members": 1}]
    assert empty.pk not in [g["id"] for g in data["groups"]]


# --- who is told ----------------------------------------------------------------------------------


def test_a_comment_mention_notifies_the_named_resident_once(
    client: Client, live_here: Callable[..., Resident]
) -> None:
    author, commenter, named = (live_here(f"{n}@gahk.dk") for n in "abc")
    notice = Notice.objects.create(author=author, body="Hvem har en stige?")

    client.force_login(commenter)
    client.post(
        f"/intern/opslagstavle/{notice.pk}/kommentar", {"body": f"Spørg {token(named)} og {token(commenter)}"}
    )

    assert told() == [named.pk]  # never the commenter themself
    row = Notification.objects.get(category=categories.MENTIONS)
    assert row.title == f"{commenter.full_name} nævnte dig i en kommentar"
    assert row.body == f"Spørg @{named.full_name} og @{commenter.full_name}"


def test_a_mentioned_post_author_gets_the_mention_instead_of_the_reply(
    client: Client, live_here: Callable[..., Resident]
) -> None:
    author, commenter = live_here("a@gahk.dk"), live_here("b@gahk.dk")
    notice = Notice.objects.create(author=author, body="Stige?")

    client.force_login(commenter)
    client.post(f"/intern/opslagstavle/{notice.pk}/kommentar", {"body": f"{token(author)} ja"})

    assert list(Notification.objects.values_list("category", flat=True)) == [categories.MENTIONS]


def test_editing_a_notice_only_notifies_newly_mentioned(
    client: Client, live_here: Callable[..., Resident]
) -> None:
    author, first, second = (live_here(f"{n}@gahk.dk") for n in "abc")

    client.force_login(author)
    client.post("/intern/opslagstavle/opret", {"category": Category.NYT, "body": f"Hej {token(first)}"})
    assert told() == [first.pk]

    notice = Notice.objects.get()
    client.post(
        f"/intern/opslagstavle/{notice.pk}/rediger",
        {"category": Category.NYT, "body": f"Hej {token(first)} og {token(second)}"},
    )
    assert told() == sorted([first.pk, second.pk])


def test_a_group_mention_reaches_the_group_this_month(
    client: Client, live_here: Callable[..., Resident]
) -> None:
    group = Workgroup.objects.create(name="Testgruppen")
    author = live_here("a@gahk.dk", workgroup=group)
    member = live_here("b@gahk.dk", workgroup=group)
    direct = live_here("c@gahk.dk", workgroup=group)
    outsider = live_here("d@gahk.dk")

    client.force_login(author)
    body = f"@[Testgruppen](g{group.pk}) og {token(direct)}: møde i aften"
    client.post("/intern/opslagstavle/opret", {"category": Category.NYT, "body": body})

    assert told() == sorted([member.pk, direct.pk])  # not the author, not the outsider
    assert outsider.pk not in told()
    titles = dict(
        Notification.objects.filter(category=categories.MENTIONS).values_list("recipient_id", "title")
    )
    assert titles[member.pk] == f"{author.full_name} nævnte Testgruppen i et opslag"
    assert titles[direct.pk] == f"{author.full_name} nævnte dig i et opslag"


def test_a_former_resident_is_not_notified(
    client: Client, make_resident: Callable[..., Resident], live_here: Callable[..., Resident]
) -> None:
    author = live_here("a@gahk.dk")
    former = make_resident(email="former@gahk.dk")

    client.force_login(author)
    client.post("/intern/opslagstavle/opret", {"category": Category.NYT, "body": f"Hej {token(former)}"})
    assert told() == []


def test_den_hurtige_mention_replaces_the_room_notification(
    client: Client, live_here: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Room notifications are push-only (no rows), so the fan-out itself is what is checked."""
    from notifications import services

    author, named, other = (live_here(f"{n}@gahk.dk") for n in "abc")
    fanouts: dict[str, set[int]] = {}
    real_notify = services.notify

    def recording_notify(recipients: object, **kwargs: object) -> int:
        ids = services._ids(recipients)  # type: ignore[arg-type]
        fanouts[str(kwargs["category"])] = set(ids) - {kwargs.get("exclude")}
        return real_notify(ids, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(services, "notify", recording_notify)
    client.force_login(author)
    client.post(
        "/intern/den-hurtige/opret",
        {"content": f"{token(named)} kommer du?", "duration": min(VALID_DURATIONS)},
    )

    assert told() == [named.pk]
    room = next(ids for key, ids in fanouts.items() if key.startswith(categories.CHAT_ROOM_PREFIX))
    assert other.pk in room
    assert named.pk not in room


def test_den_hurtige_reply_mention(client: Client, live_here: Callable[..., Resident]) -> None:
    author, replier, named = (live_here(f"{n}@gahk.dk") for n in "abc")
    post = QuickPost.objects.create(author=author, content="Boremaskine?")

    client.force_login(replier)
    client.post(
        f"/intern/den-hurtige/{post.pk}/kommentar", {"content": f"{token(named)} har en", "notify": "op"}
    )
    assert told() == [named.pk]


def test_reparationer_mentions_in_task_and_note(client: Client, live_here: Callable[..., Resident]) -> None:
    reporter, named, noted = (live_here(f"{n}@gahk.dk") for n in "abc")

    client.force_login(reporter)
    client.post(
        "/intern/reparationer/opret", {"title": "Utæt vandhane", "description": f"Spørg {token(named)}"}
    )
    assert told() == [named.pk]

    task = RepairTask.objects.get()
    client.post(f"/intern/reparationer/{task.pk}/kommentar", {"body": f"{token(noted)} kig lige"})
    assert told() == sorted([named.pk, noted.pk])


def test_a_private_event_only_notifies_mentioned_invitees_and_hosts(
    client: Client, live_here: Callable[..., Resident]
) -> None:
    host, guest, outsider = (live_here(f"{n}@gahk.dk") for n in "abc")
    event = Event.objects.create(
        organiser=host,
        title="Middag",
        starts_at=(timezone.now() + datetime.timedelta(days=3)).replace(second=0, microsecond=0),
        visibility=Visibility.KUN_INVITEREDE,
    )
    EventInvite.objects.create(event=event, resident=guest, invited_by=host)

    client.force_login(guest)
    client.post(f"/intern/begivenheder/{event.pk}/kommentar", {"body": f"{token(host)} {token(outsider)}"})

    assert told() == [host.pk]  # the outsider cannot see the event
    # The host was mentioned, so they get that rather than a second "kommenterede".
    assert not Notification.objects.filter(category=categories.EVENTS_COMMENTS).exists()


def test_a_mention_in_a_new_notice_replaces_the_new_post_notification(
    client: Client, live_here: Callable[..., Resident]
) -> None:
    author, named, other = (live_here(f"{n}@gahk.dk") for n in "abc")

    client.force_login(author)
    client.post("/intern/opslagstavle/opret", {"category": Category.NYT, "body": f"Hej {token(named)}"})

    rows = set(Notification.objects.values_list("recipient_id", "category"))
    assert rows == {(named.pk, categories.MENTIONS), (other.pk, categories.POSTS_NEW)}


def test_an_event_description_mention_notifies_on_create_and_on_edit(
    client: Client, live_here: Callable[..., Resident]
) -> None:
    host, first, second, other = (live_here(f"{n}@gahk.dk") for n in "abcd")
    when = timezone.localtime(timezone.now() + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    data = {"title": "Filmaften", "location": "TV-stuen", "starts_at": when, "visibility": Visibility.AABENT}

    client.force_login(host)
    client.post("/intern/begivenheder/opret", {**data, "description": f"Med {token(first)}"})

    rows = set(Notification.objects.values_list("recipient_id", "category"))
    assert (first.pk, categories.MENTIONS) in rows
    assert (first.pk, categories.EVENTS_NEW) not in rows  # the mention instead
    assert (other.pk, categories.EVENTS_NEW) in rows
    title = Notification.objects.get(category=categories.MENTIONS).title
    assert title == f"{host.full_name} nævnte dig i Filmaften"

    event = Event.objects.get()
    client.post(
        f"/intern/begivenheder/{event.pk}/rediger",
        {**data, "description": f"Med {token(first)} og {token(second)}"},
    )
    assert told() == sorted([first.pk, second.pk])


def test_a_private_event_description_only_notifies_invitees(
    client: Client, live_here: Callable[..., Resident]
) -> None:
    host, outsider = live_here("host@gahk.dk"), live_here("out@gahk.dk")
    guests = [live_here(f"g{n}@gahk.dk") for n in range(MIN_INVITEES)]
    when = timezone.localtime(timezone.now() + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")

    client.force_login(host)
    client.post(
        "/intern/begivenheder/opret",
        {
            "title": "Middag",
            "starts_at": when,
            "visibility": Visibility.KUN_INVITEREDE,
            "invitees": [g.pk for g in guests],
            "description": f"{token(guests[0])} og {token(outsider)}",
        },
    )

    assert Event.objects.exists()
    assert told() == [guests[0].pk]


def test_a_group_mention_skips_last_months_members(
    client: Client, live_here: Callable[..., Resident]
) -> None:
    """Group and month must hold for the same residency: someone who was in the group last month
    and is in another group now is not a member."""
    group = Workgroup.objects.create(name="Testgruppen")
    other_group = Workgroup.objects.create(name="Andengruppen")
    author = live_here("a@gahk.dk")
    member = live_here("b@gahk.dk", workgroup=group)
    moved = live_here("c@gahk.dk", workgroup=other_group)
    year, month = active_period()
    last_year, last_month = (year, month - 1) if month > 1 else (year - 1, 12)
    room = Residency.objects.get(resident=moved).room
    Residency.objects.create(resident=moved, room=room, year=last_year, month=last_month, workgroup=group)

    client.force_login(author)
    client.post(
        "/intern/opslagstavle/opret", {"category": Category.NYT, "body": f"@[Testgruppen](g{group.pk})"}
    )

    assert told() == [member.pk]
