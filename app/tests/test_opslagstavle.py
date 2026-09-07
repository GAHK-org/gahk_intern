"""Opslagstavlen — the noticeboard replacing the kollegium's Facebook group.

The renderer itself is tested in test_markdown.py (pure, no DB) and the shared push transport in
test_push.py. What is here is the feature: who may do what, how the list orders and paginates, the
image claim/release lifecycle, the orphan-upload sweep, and the notification *policy*.

Several tests assert the **absence** of things Den Hurtige does — the 20-second poll, the zoom
lockdown, a purge on page load. Those look like omissions and are decisions; without a test they get
"fixed" by the next person who reads the sibling feature and assumes it is the house style.
"""

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from core import push
from core.models import PushSubscription, Room, Workgroup
from den_hurtige import access as den_hurtige_access
from opslagstavle import access
from opslagstavle.models import (
    MAX_PINNED,
    Category,
    Notice,
    NoticeComment,
    NoticeImage,
    NoticeReaction,
)
from residents.models import Residency, Resident, Role, active_period

BOARD = "/intern/opslagstavle/"
pytestmark = pytest.mark.django_db


GATED_ROLES = access.ACCESS_ROLES or (Role.ADMINISTRATOR, Role.INSPEKTION)


@pytest.fixture(autouse=True)
def rollout_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the rollout gate for the whole module.

    opslagstavlen is limited to the trial group while its mechanics are being tested
    (opslagstavle.access.ACCESS_ROLES), but that restriction is temporary and every test outside the
    "staged rollout" section is about behaviour that outlives it. Without this they would all have to
    hand their residents an administrator role, which would quietly stop them testing what a normal
    resident experiences — a beboer posting, commenting and reacting is most of this file.
    """
    monkeypatch.setattr(access, "ACCESS_ROLES", None)


@pytest.fixture
def rollout_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the gate back on — runs after the autouse fixture, so it wins."""
    monkeypatch.setattr(access, "ACCESS_ROLES", GATED_ROLES)


@pytest.fixture
def media_tmp(settings: object, tmp_path: Path) -> Path:
    """Redirect MEDIA_ROOT so uploads land in a tmp dir, not the repo's app/media/."""
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    return tmp_path


@pytest.fixture
def beboer(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="beboer@gahk.dk", first_name="Bo", last_name="Beboer")


@pytest.fixture
def other(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="anden@gahk.dk", first_name="Ann", last_name="Anden")


@pytest.fixture
def inspektion(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="ins@gahk.dk", roles=(Role.INSPEKTION,))


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch, settings: object) -> list[tuple[list[int], dict]]:
    """Records (recipient user ids, payload) for every notification the code would send."""
    settings.VAPID_PUBLIC_KEY = "test-public-key"  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = "test-private-key"  # type: ignore[attr-defined]
    recorded: list[tuple[list[int], dict]] = []

    def fake_dispatch(subscriptions: object, payload: dict) -> int:
        ids = sorted(subscriptions.values_list("user_id", flat=True))  # type: ignore[attr-defined]
        recorded.append((ids, payload))
        return len(ids)

    monkeypatch.setattr(push, "_dispatch", fake_dispatch)
    monkeypatch.setattr(push, "_run_in_background", lambda fn: fn())
    return recorded


def make_notice(author: Resident, **kwargs: object) -> Notice:
    return Notice.objects.create(
        author=author,
        body=kwargs.pop("body", "Noget **indhold**."),  # type: ignore[arg-type]
        **kwargs,
    )


def give_embedsgruppe(resident: Resident, name: str, room_number: int) -> None:
    """Put `resident` in the `name` embedsgruppe for the active month — the pill's only source."""
    year, month = active_period()
    Residency.objects.create(
        resident=resident,
        room=Room.objects.create(
            legacy_index=room_number, number=room_number, floor="stuen", side="mod gaden"
        ),
        workgroup=Workgroup.objects.get_or_create(name=name)[0],
        year=year,
        month=month,
    )


def png(name: str = "billede.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"\x89PNG\r\n\x1a\n" + b"x" * 64, content_type="image/png")


# --- access ---------------------------------------------------------------------------------------


def test_the_board_requires_login(client: Client) -> None:
    response = client.get(BOARD)

    assert response.status_code == 302
    assert "/intern/admin/login" in response["Location"]


def test_every_resident_can_read_and_post(client: Client, beboer: Resident) -> None:
    """No role needed, and no rollout gate: the feature replaces a group everyone was already in."""
    client.force_login(beboer)

    assert client.get(BOARD).status_code == 200
    response = client.post(BOARD + "opret", {"category": Category.BEGIVENHED, "body": "På fredag."})

    assert response.status_code == 302
    assert Notice.objects.get().author_id == beboer.pk


def test_a_resident_who_cannot_open_den_hurtige_can_still_use_the_board(
    client: Client, beboer: Resident, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The board's access must not depend on Den Hurtige's.

    Both features now carry their own rollout gate, which makes this more load-bearing than when it
    was written, not less: two identically named ACCESS_ROLES constants are exactly the setup where
    one gate silently starts answering for the other. Den Hurtige's is re-applied here rather than
    assumed, so the assertion holds whichever way either switch happens to be set. This is the
    invariant behind the shared subscribe endpoint being @login_required with a per-topic check
    inside.
    """
    monkeypatch.setattr(den_hurtige_access, "ACCESS_ROLES", (Role.ADMINISTRATOR, Role.INSPEKTION))
    client.force_login(beboer)

    assert client.get("/intern/den-hurtige/").status_code == 403
    assert client.get(BOARD).status_code == 200


def test_the_sidebar_advertises_the_board_and_the_page_opens_for_the_same_user(
    client: Client, beboer: Resident
) -> None:
    """A visible link that answers 403 is worse than no link. Asserted together, in one test, so the
    two halves cannot drift apart."""
    client.force_login(beboer)

    assert BOARD in client.get("/intern/").content.decode()
    assert client.get(BOARD).status_code == 200


# --- posting and editing --------------------------------------------------------------------------


@pytest.mark.parametrize(("field", "value"), [("body", ""), ("category", "ikke-en-kategori")])
def test_an_invalid_post_is_rejected(client: Client, beboer: Resident, field: str, value: str) -> None:
    client.force_login(beboer)
    data = {"category": Category.NYT, "body": "B"}
    data[field] = value

    response = client.post(BOARD + "opret", data)

    assert response.status_code == 200  # the form is re-rendered, not a redirect
    assert not Notice.objects.exists()


def test_an_over_long_body_is_refused(client: Client, beboer: Resident) -> None:
    """Capped server-side as well as with maxlength: the LIST page renders every post's Markdown, so
    one enormous body would slow the whole board rather than a single post."""
    from opslagstavle.models import MAX_BODY_CHARS

    client.force_login(beboer)

    response = client.post(BOARD + "opret", {"category": Category.NYT, "body": "x" * (MAX_BODY_CHARS + 1)})

    assert response.status_code == 200
    assert not Notice.objects.exists()


def test_the_list_shows_rendered_markdown_not_source(client: Client, beboer: Resident) -> None:
    make_notice(beboer, body="En **vigtig** ting")
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert "<strong>vigtig</strong>" in body
    assert "**vigtig**" not in body


def test_a_markdown_table_survives_to_the_page(client: Client, beboer: Resident) -> None:
    """The værelsesrunde results are a table, and they are the motivating post for this feature."""
    notice = make_notice(beboer, body="| Værelse | Beboer |\n|---|---|\n| 003 | Bo |")
    client.force_login(beboer)

    body = client.get(f"{BOARD}{notice.pk}").content.decode()

    assert "<table>" in body
    assert "|---|" not in body  # not left as literal source


def test_an_author_can_edit_their_own_post_and_it_is_marked_edited(client: Client, beboer: Resident) -> None:
    notice = make_notice(beboer)
    client.force_login(beboer)

    client.post(
        f"{BOARD}{notice.pk}/rediger",
        {"category": Category.NYT, "body": "Rettet indhold"},
    )

    notice.refresh_from_db()
    assert notice.body == "Rettet indhold"
    assert notice.edited_at is not None


def test_a_never_edited_post_has_no_edit_marker(client: Client, beboer: Resident) -> None:
    notice = make_notice(beboer)

    assert notice.edited_at is None


def test_pinning_does_not_mark_a_post_edited(client: Client, beboer: Resident, inspektion: Resident) -> None:
    """`edited_at` is set by the edit view, never auto_now — otherwise Inspektionen pinning a post
    would stamp it "Redigeret", which readers see and which would be a lie."""
    notice = make_notice(beboer)
    client.force_login(inspektion)

    client.post(f"{BOARD}{notice.pk}/fastgoer")

    notice.refresh_from_db()
    assert notice.is_pinned
    assert notice.edited_at is None


def test_another_resident_cannot_edit_a_post(client: Client, beboer: Resident, other: Resident) -> None:
    notice = make_notice(beboer)
    client.force_login(other)

    assert client.get(f"{BOARD}{notice.pk}/rediger").status_code == 403


def test_inspektionen_cannot_rewrite_someone_elses_post(
    client: Client, beboer: Resident, inspektion: Resident
) -> None:
    """Moderators may delete but never edit. A post keeps its author's name on it, so silently
    changing the words — words that may already have replies referring to them — is worse than
    removing it: deleting is visible, editing is not."""
    notice = make_notice(beboer, body="Originalen")
    client.force_login(inspektion)

    response = client.post(f"{BOARD}{notice.pk}/rediger", {"category": Category.NYT, "body": "x"})

    assert response.status_code == 403
    notice.refresh_from_db()
    assert notice.body == "Originalen"


# --- moderation -----------------------------------------------------------------------------------


def test_an_author_can_delete_their_own_post(client: Client, beboer: Resident) -> None:
    notice = make_notice(beboer)
    client.force_login(beboer)

    client.post(f"{BOARD}{notice.pk}/slet")

    assert not Notice.objects.exists()


def test_a_resident_cannot_delete_someone_elses_post(
    client: Client, beboer: Resident, other: Resident
) -> None:
    notice = make_notice(beboer)
    client.force_login(other)

    assert client.post(f"{BOARD}{notice.pk}/slet").status_code == 403
    assert Notice.objects.exists()


@pytest.mark.parametrize("role", [Role.INSPEKTION, Role.ADMINISTRATOR])
def test_a_moderator_can_delete_anyones_post(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident], role: str
) -> None:
    notice = make_notice(beboer)
    client.force_login(make_resident(email=f"{role}@gahk.dk", roles=(role,)))

    client.post(f"{BOARD}{notice.pk}/slet")

    assert not Notice.objects.exists()


def test_only_moderators_see_the_pin_control(client: Client, beboer: Resident, inspektion: Resident) -> None:
    notice = make_notice(beboer)
    pin_url = f"{BOARD}{notice.pk}/fastgoer"

    client.force_login(beboer)
    assert pin_url not in client.get(f"{BOARD}{notice.pk}").content.decode()

    client.force_login(inspektion)
    assert pin_url in client.get(f"{BOARD}{notice.pk}").content.decode()


def test_previewing_as_a_beboer_hides_the_moderation_controls(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """Access reads *effective* roles, so the admin preview tool tells the truth about what a plain
    resident sees. That real/effective split is the security boundary in this codebase."""
    notice = make_notice(beboer)
    admin = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    client.force_login(admin)
    pin_url = f"{BOARD}{notice.pk}/fastgoer"
    assert pin_url in client.get(f"{BOARD}{notice.pk}").content.decode()

    session = client.session
    session["preview_roles"] = []  # view as a plain beboer
    session.save()

    assert pin_url not in client.get(f"{BOARD}{notice.pk}").content.decode()


def test_a_comment_can_be_deleted_by_its_author(client: Client, beboer: Resident) -> None:
    notice = make_notice(beboer)
    comment = NoticeComment.objects.create(notice=notice, author=beboer, body="Min")
    client.force_login(beboer)

    client.post(f"{BOARD}kommentar/{comment.pk}/slet")

    assert not NoticeComment.objects.exists()


def test_a_moderator_can_delete_any_comment(
    client: Client, beboer: Resident, other: Resident, inspektion: Resident
) -> None:
    notice = make_notice(beboer)
    comment = NoticeComment.objects.create(notice=notice, author=other, body="Andens")
    client.force_login(inspektion)

    client.post(f"{BOARD}kommentar/{comment.pk}/slet")

    assert not NoticeComment.objects.exists()


def test_the_post_author_cannot_delete_a_reply_to_their_own_post(
    client: Client, beboer: Resident, other: Resident
) -> None:
    """Deliberately excluded. Letting people moderate the replies to their own post invites exactly
    the disputes Inspektionen exists to settle, and "he deleted my comment" is a worse problem for
    the kollegium than an unwelcome reply staying up until someone impartial looks at it."""
    notice = make_notice(beboer)
    comment = NoticeComment.objects.create(notice=notice, author=other, body="Kritik")
    client.force_login(beboer)

    assert client.post(f"{BOARD}kommentar/{comment.pk}/slet").status_code == 403
    assert NoticeComment.objects.exists()


def test_a_bystander_cannot_delete_a_comment(
    client: Client, beboer: Resident, other: Resident, make_resident: Callable[..., Resident]
) -> None:
    notice = make_notice(beboer)
    comment = NoticeComment.objects.create(notice=notice, author=other, body="Hej")
    client.force_login(make_resident(email="tredje@gahk.dk"))

    assert client.post(f"{BOARD}kommentar/{comment.pk}/slet").status_code == 403


# --- pinning --------------------------------------------------------------------------------------


def test_pinning_records_who_and_when(client: Client, beboer: Resident, inspektion: Resident) -> None:
    """ "Who pinned this, and when" is the first question Inspektionen will be asked — which a boolean
    flag could not answer."""
    notice = make_notice(beboer)
    client.force_login(inspektion)

    client.post(f"{BOARD}{notice.pk}/fastgoer")

    notice.refresh_from_db()
    assert notice.pinned_at is not None
    assert notice.pinned_by_id == inspektion.pk


def test_pinning_is_a_toggle(client: Client, beboer: Resident, inspektion: Resident) -> None:
    notice = make_notice(beboer)
    client.force_login(inspektion)

    client.post(f"{BOARD}{notice.pk}/fastgoer")
    client.post(f"{BOARD}{notice.pk}/fastgoer")

    notice.refresh_from_db()
    assert notice.pinned_at is None
    assert notice.pinned_by is None


def test_a_resident_cannot_pin(client: Client, beboer: Resident) -> None:
    notice = make_notice(beboer)
    client.force_login(beboer)

    assert client.post(f"{BOARD}{notice.pk}/fastgoer").status_code == 403
    notice.refresh_from_db()
    assert not notice.is_pinned


def test_pinned_posts_are_shown_first(client: Client, beboer: Resident, inspektion: Resident) -> None:
    old = make_notice(beboer, body="Gammelt men fastgjort")
    make_notice(beboer, body="Nyere")
    old.pinned_at = timezone.now()
    old.pinned_by = inspektion
    old.save()
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert body.index("Gammelt men fastgjort") < body.index("Nyere")


def test_pinned_posts_appear_on_every_page(client: Client, beboer: Resident, inspektion: Resident) -> None:
    """The reason pinned posts are a separate queryset rather than an ordering inside the paginator.
    Ordered in, they would show on page 1 only — which makes pinning pointless the moment the board
    is a few pages deep."""
    from opslagstavle.views import PAGE_SIZE

    pinned = make_notice(beboer, body="Altid øverst")
    pinned.pinned_at = timezone.now()
    pinned.pinned_by = inspektion
    pinned.save()
    for i in range(PAGE_SIZE + 3):
        make_notice(beboer, body=f"Opslag {i}")
    client.force_login(beboer)

    page_two = client.get(BOARD, {"page": 2}).content.decode()

    assert "Altid øverst" in page_two


def test_pinned_ordering_does_not_depend_on_nulls_placement(beboer: Resident, inspektion: Resident) -> None:
    """Postgres orders DESC as NULLS FIRST and SQLite as NULLS LAST, so a single
    `order_by("-pinned_at", …)` would put pinned posts on top in one backend and unpinned on top in
    the other — correct locally, wrong in production. The two-queryset split sidesteps that; this
    pins the behaviour so reintroducing one order_by is caught."""
    unpinned = make_notice(beboer, body="Ikke fastgjort")
    first = make_notice(beboer, body="Fastgjort først")
    second = make_notice(beboer, body="Fastgjort sidst")
    first.pinned_at = timezone.now() - timedelta(hours=1)
    first.save()
    second.pinned_at = timezone.now()
    second.save()

    assert [n.body for n in Notice.objects.pinned()] == ["Fastgjort sidst", "Fastgjort først"]
    assert [n.body for n in Notice.objects.unpinned()] == [unpinned.body]


def test_the_pin_limit_is_enforced(client: Client, beboer: Resident, inspektion: Resident) -> None:
    """A pinned post is both permanently on top AND exempt from the purge, so without a cap "pin"
    quietly becomes "keep forever"."""
    for i in range(MAX_PINNED):
        n = make_notice(beboer, body=f"Fastgjort {i}")
        n.pinned_at = timezone.now()
        n.save()
    extra = make_notice(beboer, body="En for meget")
    client.force_login(inspektion)

    client.post(f"{BOARD}{extra.pk}/fastgoer")

    extra.refresh_from_db()
    assert not extra.is_pinned
    assert Notice.objects.pinned().count() == MAX_PINNED


def test_unpinning_frees_a_slot(client: Client, beboer: Resident, inspektion: Resident) -> None:
    pinned = [make_notice(beboer, body=f"F{i}") for i in range(MAX_PINNED)]
    for n in pinned:
        n.pinned_at = timezone.now()
        n.save()
    extra = make_notice(beboer, body="Ny")
    client.force_login(inspektion)

    client.post(f"{BOARD}{pinned[0].pk}/fastgoer")  # unpin one
    client.post(f"{BOARD}{extra.pk}/fastgoer")  # now this fits

    extra.refresh_from_db()
    assert extra.is_pinned


# --- list, filter, pagination ---------------------------------------------------------------------


def test_the_category_filter_narrows_the_list(client: Client, beboer: Resident) -> None:
    make_notice(beboer, body="Fest", category=Category.BEGIVENHED)
    make_notice(beboer, body="Runden", category=Category.VAERELSESRUNDE)
    client.force_login(beboer)

    body = client.get(BOARD, {"kategori": Category.VAERELSESRUNDE}).content.decode()

    assert "Runden" in body
    assert "Fest" not in body


def test_an_unknown_category_falls_back_to_everything(client: Client, beboer: Resident) -> None:
    """A bad querystring should show the board, not a 404 — the value comes from a link someone may
    have kept after the category set changed."""
    make_notice(beboer, body="Noget")
    client.force_login(beboer)

    response = client.get(BOARD, {"kategori": "findes-ikke"})

    assert response.status_code == 200
    assert "Noget" in response.content.decode()


def test_every_category_chip_links_to_a_working_filter(client: Client, beboer: Resident) -> None:
    client.force_login(beboer)

    for value, _label in Category.choices:
        assert client.get(BOARD, {"kategori": value}).status_code == 200


def test_the_list_paginates(client: Client, beboer: Resident) -> None:
    from opslagstavle.views import PAGE_SIZE

    for i in range(PAGE_SIZE + 1):
        make_notice(beboer, body=f"Opslag {i}")
    client.force_login(beboer)

    assert client.get(BOARD).context["page_obj"].paginator.num_pages == 2
    assert client.get(BOARD, {"page": 2}).status_code == 200


def test_an_out_of_range_page_falls_back(client: Client, beboer: Resident) -> None:
    make_notice(beboer)
    client.force_login(beboer)

    assert client.get(BOARD, {"page": 99}).status_code == 200
    assert client.get(BOARD, {"page": "ikke-et-tal"}).status_code == 200


def test_the_board_does_not_poll_itself(client: Client, beboer: Resident) -> None:
    """Den Hurtige polls every 5s because its messages die in 30 minutes. A paginated multi-year
    archive must not: polling fights the pager, throws away the reader's place, and re-renders every
    post's Markdown server-side every 20 seconds per open tab."""
    make_notice(beboer)
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert 'hx-trigger="every' not in body


def test_the_board_keeps_pinch_zoom(client: Client, beboer: Resident) -> None:
    """`no-zoom`/`chat-page` exist for the one-handed chat. A long post with a table needs zoom."""
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert "no-zoom" not in body
    assert "user-scalable=no" not in body


def test_board_pages_leak_no_template_syntax(client: Client, beboer: Resident) -> None:
    """Django's {# … #} is single-line only; spread over two it renders verbatim onto the page. This
    has reached production twice elsewhere in this project."""
    notice = make_notice(beboer)
    NoticeComment.objects.create(notice=notice, author=beboer, body="Svar")
    client.force_login(beboer)

    for path in (BOARD, f"{BOARD}{notice.pk}", BOARD + "opret", f"{BOARD}{notice.pk}/rediger"):
        body = client.get(path).content.decode()
        assert "{%" not in body, path
        assert "{{" not in body, path


# --- comments and reactions -----------------------------------------------------------------------


def test_a_resident_can_comment(client: Client, beboer: Resident, other: Resident) -> None:
    notice = make_notice(beboer)
    client.force_login(other)

    client.post(f"{BOARD}{notice.pk}/kommentar", {"body": "God idé"})

    assert NoticeComment.objects.get().author_id == other.pk


def test_a_comment_is_plain_text_and_markdown_is_not_rendered(client: Client, beboer: Resident) -> None:
    """Comments are deliberately not Markdown — that keeps the embedded-image lifecycle to one model
    and the compose toolbar single-purpose."""
    notice = make_notice(beboer)
    NoticeComment.objects.create(notice=notice, author=beboer, body="**ikke fed**")
    client.force_login(beboer)

    body = client.get(f"{BOARD}{notice.pk}").content.decode()

    assert "**ikke fed**" in body
    assert "<strong>ikke fed</strong>" not in body


def test_a_comment_cannot_inject_html(client: Client, beboer: Resident) -> None:
    notice = make_notice(beboer)
    NoticeComment.objects.create(notice=notice, author=beboer, body="<script>alert(1)</script>")
    client.force_login(beboer)

    body = client.get(f"{BOARD}{notice.pk}").content.decode()

    assert "&lt;script&gt;" in body
    assert "<script>alert(1)</script>" not in body


def test_comments_and_reactions_die_with_their_post(client: Client, beboer: Resident) -> None:
    notice = make_notice(beboer)
    NoticeComment.objects.create(notice=notice, author=beboer, body="x")
    NoticeReaction.objects.create(notice=notice, author=beboer, emoji="👍")

    notice.delete()

    assert not NoticeComment.objects.exists()
    assert not NoticeReaction.objects.exists()


def test_a_reaction_toggles_off_when_tapped_again(client: Client, beboer: Resident) -> None:
    notice = make_notice(beboer)
    client.force_login(beboer)

    client.post(f"{BOARD}{notice.pk}/reaktion", {"emoji": "👍"})
    assert NoticeReaction.objects.count() == 1

    client.post(f"{BOARD}{notice.pk}/reaktion", {"emoji": "👍"})
    assert not NoticeReaction.objects.exists()


def test_a_second_emoji_replaces_your_first(client: Client, beboer: Resident) -> None:
    """One person, one emoji per notice — the unique constraint is what makes that safe under a
    double tap."""
    notice = make_notice(beboer)
    client.force_login(beboer)

    client.post(f"{BOARD}{notice.pk}/reaktion", {"emoji": "👍"})
    client.post(f"{BOARD}{notice.pk}/reaktion", {"emoji": "🎉"})

    assert [r.emoji for r in NoticeReaction.objects.all()] == ["🎉"]


def test_only_emoji_are_accepted_as_reactions(client: Client, beboer: Resident) -> None:
    """The grammar itself is tested once, in test_emoji/test_den_hurtige — this only asserts the
    board is wired to the shared validation rather than accepting arbitrary text."""
    notice = make_notice(beboer)
    client.force_login(beboer)

    client.post(f"{BOARD}{notice.pk}/reaktion", {"emoji": "LOL"})

    assert not NoticeReaction.objects.exists()


def test_the_board_costs_no_extra_query_per_reaction(
    client: Client, beboer: Resident, django_assert_num_queries: Callable
) -> None:
    """Counts are computed in Python over prefetched rows, so adding reactions must not add queries.
    Without the prefetch this is an N+1 across a whole page of posts."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    notice = make_notice(beboer)
    client.force_login(beboer)
    with CaptureQueriesContext(connection) as before:
        client.get(BOARD)

    for i in range(5):
        NoticeReaction.objects.create(
            notice=notice, author=Resident.objects.create(email=f"r{i}@gahk.dk"), emoji="👍"
        )
    with CaptureQueriesContext(connection) as after:
        client.get(BOARD)

    assert len(after.captured_queries) == len(before.captured_queries)


def test_a_byline_shows_the_authors_embedsgruppe(client: Client, beboer: Resident) -> None:
    """On the card and, on the detail page, on a comment too — both bylines name a person, so both
    answer "who is that"."""
    give_embedsgruppe(beboer, "Repperne", room_number=201)
    notice = make_notice(beboer)
    client.force_login(beboer)

    assert "Repperne" in client.get(BOARD).content.decode()

    NoticeComment.objects.create(notice=notice, author=beboer, body="Enig.")
    page = client.get(f"{BOARD}{notice.pk}").content.decode()

    assert page.count("Repperne") == 2  # the post's byline and the comment's


def test_a_byline_keeps_the_embedsgruppe_the_author_had_when_they_posted(
    client: Client, beboer: Resident
) -> None:
    """The point of storing it rather than looking it up. Embedsgrupper rotate monthly, so on a
    board that keeps two years of posts a live lookup would relabel the whole archive every time
    indstilling saves a månedsliste — and a post about the kitchen written by that month's
    Køkkengruppen would end up attributed to whichever group its author moved on to."""
    give_embedsgruppe(beboer, "Køkkengruppen", room_number=204)
    make_notice(beboer)

    Residency.objects.filter(resident=beboer).update(
        workgroup=Workgroup.objects.get_or_create(name="Repperne")[0]
    )
    client.force_login(beboer)
    page = client.get(BOARD).content.decode()

    assert "Køkkengruppen" in page
    assert "Repperne" not in page


def test_editing_a_post_does_not_restamp_its_embedsgruppe(client: Client, beboer: Resident) -> None:
    """`save()` stamps on the way in only — the same reasoning as edited_at not being auto_now.
    Fixing a typo must not silently reattribute a post to the author's current group."""
    give_embedsgruppe(beboer, "Køkkengruppen", room_number=205)
    notice = make_notice(beboer)
    Residency.objects.filter(resident=beboer).update(
        workgroup=Workgroup.objects.get_or_create(name="Repperne")[0]
    )
    client.force_login(beboer)

    client.post(f"{BOARD}{notice.pk}/rediger", {"category": Category.NYT, "body": "Rettet."})

    notice.refresh_from_db()
    assert notice.body == "Rettet."
    assert notice.author_embedsgruppe == "Køkkengruppen"


def test_a_byline_shows_no_pill_when_the_author_has_no_embedsgruppe(
    client: Client, beboer: Resident, other: Resident
) -> None:
    """An alumnus has no residency, and their posts stay on the board for years. An empty pill
    beside their name would read as a group whose name failed to load."""
    give_embedsgruppe(beboer, "Repperne", room_number=202)
    make_notice(other)
    client.force_login(beboer)

    page = client.get(BOARD).content.decode()

    assert "Ann Anden" in page
    assert "chip-embedsgruppe" not in page


def test_the_board_looks_up_no_embedsgruppe_at_all(
    client: Client, beboer: Resident, make_resident: Callable
) -> None:
    """The snapshot is a column on a row the board already fetched, so rendering a page of pills
    costs nothing. A lookup here would be an N+1 across every post on the page."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    give_embedsgruppe(beboer, "Repperne", room_number=203)
    make_notice(beboer)
    for i in range(5):
        author = make_resident(email=f"a{i}@gahk.dk", first_name=f"A{i}", last_name="Author")
        give_embedsgruppe(author, "Viceværterne", room_number=210 + i)
        make_notice(author)
    client.force_login(beboer)

    with CaptureQueriesContext(connection) as captured:
        page = client.get(BOARD).content.decode()

    assert page.count("chip-embedsgruppe") == 6  # one pill per post, all six rendered
    assert [q["sql"] for q in captured.captured_queries if "core_workgroup" in q["sql"]] == []


def test_the_backfill_reads_the_month_each_post_was_actually_written_in(
    beboer: Resident,
) -> None:
    """The migration's backfill is not a guess: a post carries created_at, and Residency stores one
    row per (resident, month), so the group its author was in that month is on record. A resident
    who has since moved to another group must not have their old posts relabelled — which is the
    whole reason the field exists, and would be undetectable if the backfill got it wrong."""
    import importlib

    from django.apps import apps as real_apps

    backfill = importlib.import_module(
        "opslagstavle.migrations.0004_author_embedsgruppe"
    ).backfill_embedsgruppe

    year, month = active_period()
    then = (year - 1, month)
    give_embedsgruppe(beboer, "Repperne", room_number=206)  # their group now
    Residency.objects.create(
        resident=beboer,
        room=Room.objects.create(legacy_index=207, number=207, floor="stuen", side="mod gaden"),
        workgroup=Workgroup.objects.get_or_create(name="Køkkengruppen")[0],
        year=then[0],
        month=then[1],
    )
    old = make_notice(beboer)
    recent = make_notice(beboer)
    # Backdate one post into `then`, and clear both snapshots so the backfill has work to do —
    # exactly the state the migration finds on a board that predates the field.
    Notice.objects.filter(pk=old.pk).update(
        created_at=timezone.now().replace(year=then[0]), author_embedsgruppe=""
    )
    Notice.objects.filter(pk=recent.pk).update(author_embedsgruppe="")

    backfill(real_apps, None)

    assert Notice.objects.get(pk=old.pk).author_embedsgruppe == "Køkkengruppen"
    assert Notice.objects.get(pk=recent.pk).author_embedsgruppe == "Repperne"


# --- images ---------------------------------------------------------------------------------------


def test_an_upload_returns_a_usable_url_and_starts_unclaimed(
    client: Client, beboer: Resident, media_tmp: Path
) -> None:
    client.force_login(beboer)

    response = client.post(BOARD + "billede", {"file": png()})

    assert response.status_code == 201
    payload = json.loads(response.content)
    assert payload["url"].startswith("/media/opslag/")
    image = NoticeImage.objects.get()
    assert image.notice_id is None  # claimed later, when a save references it
    assert image.uploaded_by_id == beboer.pk


def test_upload_requires_login(client: Client, media_tmp: Path) -> None:
    response = client.post(BOARD + "billede", {"file": png()})

    assert response.status_code == 302
    assert not NoticeImage.objects.exists()


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("evil.svg", "image/svg+xml"),  # a document that executes script from our own origin
        ("evil.svg", "image/png"),  # a disguised extension: the extension is what it is served as
        ("doc.pdf", "application/pdf"),
    ],
)
def test_a_dangerous_upload_is_refused(
    client: Client, beboer: Resident, media_tmp: Path, filename: str, content_type: str
) -> None:
    client.force_login(beboer)
    upload = SimpleUploadedFile(filename, b"x" * 64, content_type=content_type)

    response = client.post(BOARD + "billede", {"file": upload})

    assert response.status_code == 400
    assert "error" in json.loads(response.content)
    assert not NoticeImage.objects.exists()


def test_an_oversized_image_is_refused_at_the_boards_own_limit(
    client: Client, beboer: Resident, media_tmp: Path, settings: object
) -> None:
    """Proves NOTICE_IMAGE_MAX_MB is the setting being read — not the CMS's, whose value an ops change
    might move for unrelated reasons."""
    settings.NOTICE_IMAGE_MAX_MB = 0  # type: ignore[attr-defined]
    client.force_login(beboer)

    response = client.post(BOARD + "billede", {"file": png()})

    assert response.status_code == 400
    assert not NoticeImage.objects.exists()


def test_saving_a_post_claims_the_images_it_references(
    client: Client, beboer: Resident, media_tmp: Path
) -> None:
    client.force_login(beboer)
    url = json.loads(client.post(BOARD + "billede", {"file": png()}).content)["url"]

    client.post(BOARD + "opret", {"category": Category.NYT, "body": f"![]({url})"})

    image = NoticeImage.objects.get()
    assert image.notice_id == Notice.objects.get().pk


def test_editing_a_post_releases_an_image_it_no_longer_references(
    client: Client, beboer: Resident, media_tmp: Path
) -> None:
    """Released, not deleted: the nightly sweep collects it a day later, so removing an image and
    immediately re-adding it cannot lose the file."""
    client.force_login(beboer)
    url = json.loads(client.post(BOARD + "billede", {"file": png()}).content)["url"]
    client.post(BOARD + "opret", {"category": Category.NYT, "body": f"![]({url})"})
    notice = Notice.objects.get()

    client.post(
        f"{BOARD}{notice.pk}/rediger",
        {"category": Category.NYT, "body": "billedet er fjernet"},
    )

    image = NoticeImage.objects.get()
    assert image.notice_id is None
    assert NoticeImage.objects.count() == 1  # still on disk, awaiting the grace period


def test_editing_cannot_delete_another_posts_image(client: Client, beboer: Resident, media_tmp: Path) -> None:
    """The release step is scoped to `notice.images`, so editing post B can never touch post A's."""
    client.force_login(beboer)
    url_a = json.loads(client.post(BOARD + "billede", {"file": png("a.png")}).content)["url"]
    client.post(BOARD + "opret", {"category": Category.NYT, "body": f"![]({url_a})"})
    notice_a = Notice.objects.get(body__contains=url_a)

    client.post(BOARD + "opret", {"category": Category.NYT, "body": "ingen billeder"})

    assert NoticeImage.objects.get().notice_id == notice_a.pk


def test_an_image_someone_else_uploaded_cannot_be_claimed(
    client: Client, beboer: Resident, other: Resident, media_tmp: Path
) -> None:
    """Pasting another post's image URL renders it, but must not transfer ownership — otherwise
    deleting your own post would take down a picture that was never yours."""
    client.force_login(beboer)
    url = json.loads(client.post(BOARD + "billede", {"file": png()}).content)["url"]

    client.force_login(other)
    client.post(BOARD + "opret", {"category": Category.NYT, "body": f"![]({url})"})

    assert NoticeImage.objects.get().notice_id is None


def test_deleting_a_post_deletes_its_image_and_the_file(
    client: Client,
    beboer: Resident,
    media_tmp: Path,
    django_capture_on_commit_callbacks: Callable,
) -> None:
    client.force_login(beboer)
    url = json.loads(client.post(BOARD + "billede", {"file": png()}).content)["url"]
    client.post(BOARD + "opret", {"category": Category.NYT, "body": f"![]({url})"})
    notice = Notice.objects.get()
    stored = media_tmp / NoticeImage.objects.get().file.name
    assert stored.is_file()

    with django_capture_on_commit_callbacks(execute=True):
        client.post(f"{BOARD}{notice.pk}/slet")

    assert not NoticeImage.objects.exists()
    assert not stored.exists(), "the image outlived its post"


def test_an_image_in_a_code_fence_is_not_claimed(client: Client, beboer: Resident, media_tmp: Path) -> None:
    """core.markdown walks the token stream, so a URL shown as a code sample is not a reference —
    the thing a body-scanning sweep could never get right."""
    client.force_login(beboer)
    url = json.loads(client.post(BOARD + "billede", {"file": png()}).content)["url"]

    client.post(
        BOARD + "opret",
        {"category": Category.NYT, "body": f"```\n![]({url})\n```"},
    )

    assert NoticeImage.objects.get().notice_id is None


# --- no retention, and the orphan-upload sweep ------------------------------------------------------------------------------------


def _age(notice: Notice, days: int) -> Notice:
    """Backdate created_at, which is auto_now_add and so cannot be set on create."""
    Notice.objects.filter(pk=notice.pk).update(created_at=timezone.now() - timedelta(days=days))
    notice.refresh_from_db()
    return notice


def test_purge_never_deletes_a_post_however_old(beboer: Resident) -> None:
    """THE POLICY, and the regression test for it: opslagstavlen keeps its archive.

    There used to be a two-year window here (five before user testing shortened it). The board is
    the kollegium's record of værelsesrunden results, practical notices and who announced what, and
    a window quietly threw that half away — which was the half worth having when it replaced a
    Facebook group. Nothing but the author or a moderator removes a post now.
    """
    from django.core.management import call_command

    _age(make_notice(beboer, body="Fra dengang"), 365 * 12)
    _age(make_notice(beboer, body="Sidste år"), 400)
    make_notice(beboer, body="Nyt")

    call_command("purge_notices")

    assert Notice.objects.count() == 3


def test_purge_dry_run_deletes_nothing(beboer: Resident, media_tmp: Path) -> None:
    from django.core.management import call_command

    _age(make_notice(beboer), 365 * 5)
    NoticeImage.objects.create(file=png(), uploaded_by=beboer)
    NoticeImage.objects.update(uploaded_at=timezone.now() - timedelta(days=7))

    call_command("purge_notices", "--dry-run")

    assert Notice.objects.exists()
    assert NoticeImage.objects.exists()


def test_a_bulk_delete_still_erases_the_image_files(
    beboer: Resident, media_tmp: Path, django_capture_on_commit_callbacks: Callable
) -> None:
    """A bulk queryset delete never calls Model.delete() but DOES fire post_delete — which is exactly
    why NoticeImage cleans up in a receiver.

    The retention purge used to be the caller that proved this, and it is gone; the property is not.
    Any bulk removal (a moderator clearing a spam run, a future feature, a shell) must still take the
    files with it, and against object storage a leaked file is one nothing on any page can reach.
    """
    from django.core.management import call_command

    notice = make_notice(beboer)
    image = NoticeImage.objects.create(notice=notice, file=png(), uploaded_by=beboer)
    stored = media_tmp / image.file.name
    assert stored.is_file()

    with django_capture_on_commit_callbacks(execute=True):
        Notice.objects.filter(pk=notice.pk).delete()

    assert not stored.exists()
    call_command("purge_notices")  # and the sweep finds nothing left to do


def test_purge_sweeps_an_old_unclaimed_upload(
    beboer: Resident, media_tmp: Path, django_capture_on_commit_callbacks: Callable
) -> None:
    """The abandoned-draft case, and now the command's ONLY job: the composer was opened, pictures
    added, the tab closed. Without this every abandoned draft leaves a file in the bucket forever,
    unreachable from any page and noticed by nobody."""
    from django.core.management import call_command

    image = NoticeImage.objects.create(file=png(), uploaded_by=beboer)
    NoticeImage.objects.update(uploaded_at=timezone.now() - timedelta(days=7))
    stored = media_tmp / image.file.name

    with django_capture_on_commit_callbacks(execute=True):
        call_command("purge_notices")

    assert not NoticeImage.objects.exists()
    assert not stored.exists()


def test_purge_keeps_a_freshly_uploaded_unclaimed_image(beboer: Resident, media_tmp: Path) -> None:
    """Within the grace period: somebody is still writing the post."""
    from django.core.management import call_command

    NoticeImage.objects.create(file=png(), uploaded_by=beboer)

    call_command("purge_notices")

    assert NoticeImage.objects.exists()


def test_purge_keeps_an_image_its_post_still_uses(beboer: Resident, media_tmp: Path) -> None:
    """Claimed images are not orphans, however old the post gets — which is the whole board now."""
    from django.core.management import call_command

    notice = _age(make_notice(beboer), 365 * 6)
    NoticeImage.objects.create(notice=notice, file=png(), uploaded_by=beboer)
    NoticeImage.objects.update(uploaded_at=timezone.now() - timedelta(days=365 * 6))

    call_command("purge_notices")

    assert NoticeImage.objects.exists()


def test_purge_is_idempotent(beboer: Resident) -> None:
    from django.core.management import call_command

    NoticeImage.objects.create(file=png(), uploaded_by=beboer)
    NoticeImage.objects.update(uploaded_at=timezone.now() - timedelta(days=7))

    call_command("purge_notices")
    call_command("purge_notices")

    assert not NoticeImage.objects.exists()


def test_opening_the_board_deletes_nothing(client: Client, beboer: Resident) -> None:
    """Deliberately unlike Den Hurtige, which purges on every feed load. Its promise is "gone in 30
    minutes", so a missed cron is visibly wrong within the hour; here nothing a reader can see is
    affected at all, and the posts are now kept regardless."""
    old = _age(make_notice(beboer), 365 * 4)
    client.force_login(beboer)

    client.get(BOARD)

    assert Notice.objects.filter(pk=old.pk).exists()


# --- notifications --------------------------------------------------------------------------------


def subscribe(user: Resident, endpoint: str, **topics: bool) -> PushSubscription:
    return PushSubscription.objects.create(
        user=user,
        endpoint=endpoint,
        auth="a" * 22,
        p256dh="p" * 87,
        wants_den_hurtige=topics.get("den_hurtige", False),
        wants_opslagstavle=topics.get("opslagstavle", True),
    )


def test_a_new_post_notifies_board_subscribers_except_the_author(
    client: Client, beboer: Resident, other: Resident, pushes: list
) -> None:
    subscribe(beboer, "https://push.example/author")
    subscribe(other, "https://push.example/reader")
    client.force_login(beboer)

    client.post(BOARD + "opret", {"category": Category.NYT, "body": "Indhold"})

    assert len(pushes) == 1
    recipients, payload = pushes[0]
    assert recipients == [other.pk]
    assert payload["head"] == beboer.full_name


def test_a_den_hurtige_only_subscriber_gets_nothing_from_the_board(
    client: Client, beboer: Resident, other: Resident, pushes: list
) -> None:
    """The point of splitting the topics: a device that wants urgent chat messages must not start
    receiving noticeboard posts."""
    subscribe(other, "https://push.example/chat", den_hurtige=True, opslagstavle=False)
    client.force_login(beboer)

    client.post(BOARD + "opret", {"category": Category.NYT, "body": "x"})

    assert pushes == [] or pushes[0][0] == []


def test_the_notification_links_to_the_post_not_the_board(
    client: Client, beboer: Resident, other: Resident, pushes: list
) -> None:
    """`?page=4` is not a stable address for a thing, and stops being the right one as soon as
    anyone posts again."""
    subscribe(other, "https://push.example/reader")
    client.force_login(beboer)

    client.post(BOARD + "opret", {"category": Category.NYT, "body": "x"})

    notice = Notice.objects.get()
    assert pushes[0][1]["url"] == f"{BOARD}{notice.pk}"


def test_the_notification_body_is_plain_text(
    client: Client, beboer: Resident, other: Resident, pushes: list
) -> None:
    """A lock screen showing `**Vigtigt**` is the difference between a notification people read and
    one they dismiss."""
    subscribe(other, "https://push.example/reader")
    client.force_login(beboer)

    client.post(BOARD + "opret", {"category": Category.NYT, "body": "**meget** vigtigt"})

    assert "**" not in pushes[0][1]["body"]
    assert "meget vigtigt" in pushes[0][1]["body"]


def test_a_comment_notifies_only_the_post_author(
    client: Client, beboer: Resident, other: Resident, make_resident: Callable[..., Resident], pushes: list
) -> None:
    """A board thread with twenty replies must not be twenty dorm-wide pushes — which is why there is
    deliberately no "underret alle" checkbox here, unlike Den Hurtige."""
    bystander = make_resident(email="tredje@gahk.dk")
    subscribe(beboer, "https://push.example/author")
    subscribe(bystander, "https://push.example/bystander")
    notice = make_notice(beboer)
    client.force_login(other)

    client.post(f"{BOARD}{notice.pk}/kommentar", {"body": "Svar"})

    assert len(pushes) == 1
    assert pushes[0][0] == [beboer.pk]


def test_commenting_on_your_own_post_notifies_nobody(client: Client, beboer: Resident, pushes: list) -> None:
    subscribe(beboer, "https://push.example/author")
    notice = make_notice(beboer)
    client.force_login(beboer)

    client.post(f"{BOARD}{notice.pk}/kommentar", {"body": "Tilføjelse"})

    assert pushes == []


def test_reacting_notifies_nobody(client: Client, beboer: Resident, other: Resident, pushes: list) -> None:
    """A board where every 👍 buzzes a hundred phones is exactly the noise this replaces — and it
    would stop people reacting at all."""
    subscribe(beboer, "https://push.example/author")
    notice = make_notice(beboer)
    client.force_login(other)

    client.post(f"{BOARD}{notice.pk}/reaktion", {"emoji": "👍"})

    assert pushes == []


def test_a_resident_can_subscribe_to_the_board_without_den_hurtige(client: Client, beboer: Resident) -> None:
    """The board is open to everyone, so its subscribe endpoint must be too — even for a resident
    whom Den Hurtige's rollout still excludes."""
    client.force_login(beboer)
    body = json.dumps(
        {
            "status_type": "subscribe",
            "topic": "opslagstavle",
            "subscription": {
                "endpoint": "https://fcm.googleapis.com/fcm/send/board",
                "keys": {"auth": "a" * 22, "p256dh": "p" * 87},
            },
        }
    )

    response = client.post(BOARD + "abonner", data=body, content_type="application/json")

    assert response.status_code == 201
    subscription = PushSubscription.objects.get()
    assert subscription.wants_opslagstavle is True
    assert subscription.wants_den_hurtige is False


# --- preview --------------------------------------------------------------------------------------


def test_the_preview_matches_what_gets_saved(client: Client, beboer: Resident) -> None:
    """The reason the preview is a server round-trip rather than client-side Markdown: a second
    implementation with its own allowlist would eventually disagree, and the author would see one
    thing while the board showed another."""
    source = "## Overskrift\n\n**fed** og <script>alert(1)</script>\n\n- punkt"
    client.force_login(beboer)

    previewed = client.post(BOARD + "forhaandsvisning", {"body": source}).content.decode()
    notice = make_notice(beboer, body=source)
    rendered = client.get(f"{BOARD}{notice.pk}").content.decode()

    inner = previewed.split('class="board-body prose-gahk">', 1)[1].rsplit("</div>", 1)[0].strip()
    assert inner
    assert inner in rendered


def test_preview_requires_login(client: Client) -> None:
    assert client.post(BOARD + "forhaandsvisning", {"body": "x"}).status_code == 302


# --- feed layout (from user testing) --------------------------------------------------------------
#
# Bodies are triple-quoted literals with real newlines so the markdown reads as markdown.

ONE_IMAGE_BODY = """Se her:

![et](/media/opslag/a.jpg)"""

THREE_IMAGE_BODY = """Billeder fra festen:

![et](/media/opslag/a.jpg)

![to](/media/opslag/b.jpg)

![tre](/media/opslag/c.jpg)"""


def test_a_post_with_several_images_collapses_to_one_thumbnail_in_the_feed(
    client: Client, beboer: Resident
) -> None:
    """User testing: a photo-heavy post filled the viewport and pushed every other post below the
    fold. The feed shows the text plus a single fixed-size thumbnail instead."""
    make_notice(beboer, body=THREE_IMAGE_BODY)
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    # Counted as media references, not "<img": base.html has chrome images of its own.
    assert body.count('src="/media/opslag/') == 1, "more than one picture reached the feed"
    assert "notice-gallery" in body
    assert "3 billeder" in body
    assert "Billeder fra festen" in body  # the text itself is still there


def test_a_post_with_one_image_still_shows_it_inline(client: Client, beboer: Resident) -> None:
    """A single picture reads as part of the post rather than as a gallery, so it is left alone —
    collapsing it would be a regression, not a fix."""
    make_notice(beboer, body=ONE_IMAGE_BODY)
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert "/media/opslag/a.jpg" in body
    assert "notice-gallery" not in body


def test_the_detail_page_shows_every_image(client: Client, beboer: Resident) -> None:
    """Collapsing is a feed-density fix only: opening the post must show the whole thing."""
    notice = make_notice(beboer, body=THREE_IMAGE_BODY)
    client.force_login(beboer)

    body = client.get(f"{BOARD}{notice.pk}").content.decode()

    assert body.count('src="/media/opslag/') == 3
    assert "notice-gallery" not in body


def test_the_thumbnail_is_the_first_image(client: Client, beboer: Resident) -> None:
    make_notice(beboer, body=THREE_IMAGE_BODY)
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert "/media/opslag/a.jpg" in body
    assert "/media/opslag/b.jpg" not in body


def test_a_feed_card_is_tappable_as_a_whole(client: Client, beboer: Resident) -> None:
    """User testing: people tapped the card, not the heading. The permalink on the timestamp is
    stretched over the card in CSS (`.is-clickable`), so it stays ONE real link — keyboard reachable
    and right-clickable — rather than a JS click handler or a card wrapped in an anchor."""
    notice = make_notice(beboer)
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert "is-clickable" in body
    assert f'class="notice-link" href="{BOARD}{notice.pk}"' in body


def test_the_detail_card_is_not_clickable(client: Client, beboer: Resident) -> None:
    """No overlay on the permalink: you are already there, and an overlay would break selecting text
    out of the post — which is the page people copy a room number from."""
    notice = make_notice(beboer)
    client.force_login(beboer)

    body = client.get(f"{BOARD}{notice.pk}").content.decode()

    assert "is-clickable" not in body


def test_the_moderation_controls_stay_reachable_on_a_clickable_card(
    client: Client, beboer: Resident, inspektion: Resident
) -> None:
    """The stretched link covers the card, so anything interactive has to be raised above it. If the
    markup ever stops putting the controls in `.notice-actions`, the CSS that lifts them stops
    applying and pin/delete become untappable in the feed."""
    make_notice(beboer)
    client.force_login(inspektion)

    body = client.get(BOARD).content.decode()

    assert "notice-actions" in body
    assert "/fastgoer" in body


# --- who reacted --------------------------------------------------------------------------------


def test_the_board_shows_who_reacted_and_with_what(client: Client, beboer: Resident) -> None:
    """Same widget as Den Hurtige, from the same core.reactions rows — so the panel names people
    here too rather than only showing a count."""
    notice = make_notice(beboer)
    reactor = Resident.objects.create(email="m@gahk.dk", first_name="Mette", last_name="Hansen")
    NoticeReaction.objects.create(notice=notice, author=reactor, emoji="👍")
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert "who-list" in body
    assert "Mette Hansen" in body


def test_the_board_reaction_panels_are_overlays_with_a_backdrop(client: Client, beboer: Resident) -> None:
    """Anchored beside its summary the picker opened off the edge of a phone once an item had a few
    reactions; both panels are fixed-position overlays over a real backdrop element instead."""
    notice = make_notice(beboer)
    NoticeReaction.objects.create(notice=notice, author=beboer, emoji="👍")
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert 'class="pop who-picker"' in body
    assert 'class="pop emoji-picker"' in body
    # Pairing rather than a fixed count: every .pop on the page must bring its own backdrop, and the
    # page gained a third one (the moderation menu) after this test was first written.
    assert body.count('class="pop-backdrop"') == body.count('class="pop-panel"')
    assert body.count('class="pop-backdrop"') >= 2


def test_no_reader_panel_on_the_board_when_nobody_has_reacted(client: Client, beboer: Resident) -> None:
    make_notice(beboer)
    client.force_login(beboer)

    assert "who-picker" not in client.get(BOARD).content.decode()


def test_the_board_panels_are_one_per_emoji(client: Client, beboer: Resident) -> None:
    """The board used to render a single 👥 panel listing everyone, grouped by emoji. It is now one
    panel per emoji, opened from the pill — the same widget Den Hurtige has, so the same control no
    longer answers a different question depending on which page you are on."""
    notice = make_notice(beboer)
    mette = Resident.objects.create(email="m@gahk.dk", first_name="Mette", last_name="Hansen")
    anders = Resident.objects.create(email="a@gahk.dk", first_name="Anders", last_name="Bo")
    NoticeReaction.objects.create(notice=notice, author=beboer, emoji="👍")
    NoticeReaction.objects.create(notice=notice, author=mette, emoji="👍")
    NoticeReaction.objects.create(notice=notice, author=anders, emoji="🎉")
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert body.count('class="who-row"') == 3  # one per reaction, not one per emoji
    assert body.count('class="pop who-picker"') == 2  # one panel per emoji, not one for the notice

    # Slice on the panel ids, not the bare keys — the pills reference the same keys in data-who.
    thumb = body[body.index(f'id="who-{notice.pk}-1"') : body.index(f'id="who-{notice.pk}-2"')]
    assert "Mette Hansen" in thumb
    assert "Anders Bo" not in thumb  # he used the other emoji

    # The 👥 pill is gone: the pills themselves are now the way in.
    assert "reaction-who" not in body


def test_holding_a_board_reaction_pill_is_what_opens_the_reader_panel(
    client: Client, beboer: Resident
) -> None:
    """Each pill points at its own panel by id, which is what frontend/src/feed.ts binds the hold,
    hover, right-click and Shift+Enter gestures to. That module is delegated from `document` and
    keys on `.reaction[data-who]`, so it covers this page without knowing anything about it —
    but only if the markup carries the hook."""
    notice = make_notice(beboer)
    NoticeReaction.objects.create(notice=notice, author=beboer, emoji="👍")
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert f'data-who="who-{notice.pk}-1"' in body
    assert f'id="who-{notice.pk}-1"' in body
    assert 'aria-haspopup="dialog"' in body
    # Tapping a pill must still be the toggle, never the panel.
    assert f'hx-post="{BOARD}{notice.pk}/reaktion"' in body


def test_the_add_reaction_button_spells_itself_out_on_an_untouched_notice(
    client: Client, beboer: Resident
) -> None:
    """On a notice with no reactions the picker is the only thing in the row, where a bare glyph
    reads as an empty slot rather than the button that fills it. CSS keys off `.is-empty`."""
    notice = make_notice(beboer)
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert 'class="reactions is-empty"' in body
    assert "Reagér" in body

    NoticeReaction.objects.create(notice=notice, author=beboer, emoji="👍")
    assert 'class="reactions is-empty"' not in client.get(BOARD).content.decode()


def test_the_toggled_row_still_carries_the_reader_panel(client: Client, beboer: Resident) -> None:
    """The toggle re-renders only this partial, so the panels and their data-who hooks have to come
    back with it — otherwise reacting would silently strip the gestures until the next full load."""
    notice = make_notice(beboer)
    client.force_login(beboer)

    body = client.post(f"{BOARD}{notice.pk}/reaktion", {"emoji": "👍"}).content.decode()

    assert f'data-who="who-{notice.pk}-1"' in body
    assert f'id="who-{notice.pk}-1"' in body


def test_toggling_a_reaction_returns_the_row_as_it_now_is(client: Client, beboer: Resident) -> None:
    """Regression: the toggle renders the row from a queryset read AFTER the write. Fetching the
    notice with the reactions prefetch instead built that cache before apply_toggle ran, so the tap
    came back with the row exactly as it had been — the new reaction missing until the next load."""
    notice = make_notice(beboer)
    client.force_login(beboer)

    body = client.post(f"{BOARD}{notice.pk}/reaktion", {"emoji": "👍"}).content.decode()

    assert "👍" in body or "👍" in body
    assert "Bo Beboer" in body  # the reader panel, populated from the same fresh read


# --- the author heads the post --------------------------------------------------------------------


def test_a_post_has_no_headline_field(client: Client, beboer: Resident) -> None:
    """The compose form must not offer one either -- a leftover input would put a title back into
    posts that nothing renders."""
    client.force_login(beboer)

    body = client.get(BOARD + "opret").content.decode()

    assert 'name="title"' not in body
    assert "Overskrift</label>" not in body  # the toolbar's heading button keeps that tooltip


def test_the_card_leads_with_its_author(client: Client, beboer: Resident) -> None:
    make_notice(beboer, body="Saunaen er repareret.")
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert "notice-author" in body
    assert "Bo Beboer" in body
    assert "BB" in body  # initials avatar


def test_the_timestamp_is_the_permalink(client: Client, beboer: Resident) -> None:
    """It replaced the title link as the card's single stretched link. The author's name was the
    other candidate and would have read as a link to a profile that does not exist."""
    notice = make_notice(beboer)
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert f'class="notice-link" href="{BOARD}{notice.pk}"' in body
    assert "<time datetime=" in body


def test_the_detail_page_does_not_stretch_the_permalink(client: Client, beboer: Resident) -> None:
    """On the detail page you are already there, so the timestamp is plain text -- a link to the
    page you are on is noise, and the stretch would swallow the comment form."""
    notice = make_notice(beboer)
    client.force_login(beboer)

    body = client.get(f"{BOARD}{notice.pk}").content.decode()

    assert "notice-link" not in body
    assert "<time datetime=" in body


def test_a_new_post_notifies_with_the_author_as_the_head(
    client: Client, beboer: Resident, other: Resident, pushes: list
) -> None:
    """The head used to be the title, with the author crammed into the front of the body. With no
    title, repeating the name in both lines would waste the one line a lock screen gives you."""
    subscribe(other, "https://push.example/other")
    client.force_login(beboer)

    client.post(BOARD + "opret", {"category": Category.NYT, "body": "Saunaen er repareret."})

    payload = pushes[-1][1]
    assert payload["head"] == "Bo Beboer"
    assert payload["body"] == "Saunaen er repareret."


def test_every_delete_control_asks_first(client: Client, beboer: Resident) -> None:
    """All three of them: the post, its comments, and Den Hurtige's messages. The comment and
    message controls are bare x buttons, which is what gets caught by a thumb on a phone, and none
    of these deletes can be undone."""
    notice = make_notice(beboer)
    NoticeComment.objects.create(notice=notice, author=beboer, body="En kommentar")
    client.force_login(beboer)

    detail = client.get(f"{BOARD}{notice.pk}").content.decode()

    assert detail.count("return confirm(") == 2  # the post and its one comment
    assert "En kommentar" in detail


def test_a_trimmed_excerpt_says_so(client: Client, beboer: Resident) -> None:
    """A bare "…" reads as the author trailing off rather than as a trimmed post. The marker comes
    from truncatewords_html's own ellipsis, so it appears exactly when something was cut."""
    make_notice(beboer, body=" ".join(f"ord{i}" for i in range(80)))
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert "see-more" in body
    assert "Se mere" in body


def test_a_short_post_is_not_marked_as_trimmed(client: Client, beboer: Resident) -> None:
    make_notice(beboer, body="Saunaen er repareret.")
    client.force_login(beboer)

    assert "see-more" not in client.get(BOARD).content.decode()


def test_the_detail_page_never_marks_a_post_as_trimmed(client: Client, beboer: Resident) -> None:
    """`full` renders the whole body, so a "Se mere" there would point at the page you are on."""
    notice = make_notice(beboer, body=" ".join(f"ord{i}" for i in range(80)))
    client.force_login(beboer)

    body = client.get(f"{BOARD}{notice.pk}").content.decode()

    assert "see-more" not in body
    assert "ord79" in body  # the whole post is there


# --- staged rollout -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "",
        "opret",
        "forhaandsvisning",
        "billede",
        "abonner",
        "1",
        "1/rediger",
        "1/slet",
        "1/fastgoer",
        "1/kommentar",
        "1/reaktion",
        "kommentar/1/slet",
    ],
)
def test_every_endpoint_is_closed_to_a_plain_resident(
    client: Client, beboer: Resident, rollout_limited: None, path: str
) -> None:
    """Parametrised over every route so a new view cannot quietly be added outside the gate. A
    partial that answered 200 to someone the page 403s would hand out the board through the back
    door."""
    client.force_login(beboer)
    url = BOARD + path

    response = client.get(url) if path in ("", "1") else client.post(url)

    assert response.status_code == 403


def test_the_trial_group_gets_in(client: Client, inspektion: Resident, rollout_limited: None) -> None:
    client.force_login(inspektion)

    response = client.get(BOARD)

    assert response.status_code == 200
    assert "Under test" in response.content.decode()  # testers are told it is not live yet


def test_the_sidebar_only_advertises_the_board_to_those_who_can_open_it(
    client: Client, beboer: Resident, inspektion: Resident, rollout_limited: None
) -> None:
    """A visible link that answers 403 is worse than no link."""
    client.force_login(beboer)
    assert BOARD not in client.get("/intern/").content.decode()

    client.force_login(inspektion)
    assert BOARD in client.get("/intern/").content.decode()


def test_clearing_access_roles_opens_it_to_every_resident(
    client: Client, beboer: Resident, rollout_limited: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented way to end the rollout is ACCESS_ROLES = None. Asserted end to end so the
    switch cannot rot: the roles are read per request, not bound when the views are imported."""
    client.force_login(beboer)
    assert client.get(BOARD).status_code == 403

    monkeypatch.setattr(access, "ACCESS_ROLES", None)  # the one documented edit

    assert client.get(BOARD).status_code == 200
    assert BOARD in client.get("/intern/").content.decode()  # and the sidebar follows


def test_preview_as_a_plain_resident_is_locked_out(
    client: Client, make_resident: Callable[..., Resident], rollout_limited: None
) -> None:
    """The gate reads *effective* roles, so an admin previewing as a beboer sees what a beboer sees
    — which is the whole point of the preview tool during a staged rollout."""
    admin = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    client.force_login(admin)
    session = client.session
    session["preview_roles"] = []
    session.save()

    assert client.get(BOARD).status_code == 403


def test_a_gated_out_resident_is_not_notified(
    client: Client, beboer: Resident, inspektion: Resident, pushes: list, rollout_limited: None
) -> None:
    """Gating the page but still pushing would land people on a 403 when they tapped. A
    notification you are not allowed to read is worse than no feature at all."""
    subscribe(beboer, "https://push.example/beboer")
    client.force_login(inspektion)

    client.post(BOARD + "opret", {"category": Category.NYT, "body": "Indhold"})

    assert pushes[-1][0] == []  # the beboer opted in, but cannot open the board


def test_the_trial_group_is_still_notified(
    client: Client,
    inspektion: Resident,
    make_resident: Callable[..., Resident],
    pushes: list,
    rollout_limited: None,
) -> None:
    """The narrowing must not silence the people the trial is for."""
    tester = make_resident(email="ins2@gahk.dk", roles=(Role.INSPEKTION,))
    subscribe(tester, "https://push.example/tester")
    client.force_login(inspektion)

    client.post(BOARD + "opret", {"category": Category.NYT, "body": "Indhold"})

    assert pushes[-1][0] == [tester.pk]


# --- linking a post to a begivenhed ------------------------------------------------------------------
#
# "Announce on opslagstavlen; sign up on begivenheder" is what both features' docstrings have always
# said, with no way to get from one to the other until this field existed. The interesting part is
# not the FK, it is the two places the two features disagree about lifetime and about who may see
# what: an event lives a week past its date and a post lives two years, and an event may be private
# while the board is not.


@pytest.fixture
def events_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift begivenheder's own rollout gate.

    It is behind one too, and both the chip and the form field are gated on the reader being able to
    open the feature. These tests are about the link, not about either gate.

    Redundant against the shipped value now that begivenheder is open to everyone, and kept for the
    same reason den_hurtige's equivalent is: it states what these tests need instead of inheriting
    it, so re-gating that feature cannot silently change what this section is testing. The two
    tests that ARE about the gate sit at the end of this section and set it themselves.
    """
    from events import access as events_access

    monkeypatch.setattr(events_access, "ACCESS_ROLES", None)


def _event(organiser: Resident, **extra: object) -> object:
    from events.models import Event

    defaults: dict = {
        "title": "Fællesspisning",
        "starts_at": timezone.now() + timedelta(days=3),
    }
    defaults.update(extra)
    return Event.objects.create(organiser=organiser, **defaults)


def test_a_post_can_name_the_event_it_is_about(client: Client, beboer: Resident, events_open: None) -> None:
    event = _event(beboer)
    client.force_login(beboer)

    client.post(
        BOARD + "opret",
        {"category": Category.BEGIVENHED, "body": "Vi spiser sammen på torsdag.", "event": event.pk},
    )

    assert Notice.objects.get().event_id == event.pk


def test_the_card_links_to_the_event(client: Client, beboer: Resident, events_open: None) -> None:
    event = _event(beboer, title="Fællesspisning i gården")
    make_notice(beboer, event=event)
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert "Fællesspisning i gården" in body
    assert f"/intern/begivenheder/{event.pk}" in body


def test_a_private_event_cannot_be_linked(client: Client, beboer: Resident, events_open: None) -> None:
    """The choices are the only ids a POST can name, and that is the security boundary rather than a
    convenience: opslagstavlen is read by the whole house, so a chip naming a private party would
    announce it to everyone who was not invited."""
    from events.models import Visibility

    hidden = _event(beboer, title="Hemmelig fest", visibility=Visibility.KUN_INVITEREDE)
    client.force_login(beboer)

    client.post(
        BOARD + "opret",
        {"category": Category.NYT, "body": "Indhold", "event": hidden.pk},
    )

    assert not Notice.objects.exists()


def test_an_event_made_private_after_the_fact_stops_being_shown(
    client: Client, beboer: Resident, events_open: None
) -> None:
    """The form cannot help here — the link was legal when it was made. The template checks
    visibility again on the way out, which is the check that actually protects the party."""
    from events.models import Event, Visibility

    event = _event(beboer, title="Hemmelig fest")
    make_notice(beboer, event=event)
    Event.objects.filter(pk=event.pk).update(visibility=Visibility.KUN_INVITEREDE)
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert "Hemmelig fest" not in body


def test_deleting_the_event_keeps_the_post(client: Client, beboer: Resident, events_open: None) -> None:
    """SET_NULL, and this is the whole reason for it. An event is deleted a week after it is held; a
    post lives about two years. CASCADE would mean Tuesday's dinner quietly deleting the post that
    announced it — with its comments and reactions — on Wednesday."""
    event = _event(beboer)
    notice = make_notice(beboer, event=event, body="Vi spiste sammen.")

    event.delete()

    notice.refresh_from_db()
    assert notice.event_id is None
    assert Notice.objects.filter(pk=notice.pk).exists()


def test_a_post_whose_event_is_gone_still_renders(
    client: Client, beboer: Resident, events_open: None
) -> None:
    """The state a purge leaves behind is an ordinary one, not an error — every day-old event puts
    some post into it."""
    event = _event(beboer)
    make_notice(beboer, event=event, body="Vi spiste sammen.")
    event.delete()
    client.force_login(beboer)

    response = client.get(BOARD)

    assert response.status_code == 200
    assert "Vi spiste sammen." in response.content.decode()


def test_the_link_is_optional(client: Client, beboer: Resident) -> None:
    """Most posts are not about an event, so the field must never become a hurdle to posting."""
    client.force_login(beboer)

    client.post(BOARD + "opret", {"category": Category.NYT, "body": "Vaskemaskinen er i stykker."})

    assert Notice.objects.get().event_id is None


def test_no_event_chip_while_begivenheder_is_behind_its_own_gate(
    client: Client, beboer: Resident, events_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two features, two independent rollout gates, and each has to check the other before linking
    into it. A chip that 403s whoever taps it is worse than no chip."""
    from events import access as events_access

    event = _event(beboer, title="Fællesspisning i gården")
    make_notice(beboer, event=event)
    monkeypatch.setattr(events_access, "ACCESS_ROLES", (Role.ADMINISTRATOR,))
    client.force_login(beboer)

    body = client.get(BOARD).content.decode()

    assert "Fællesspisning i gården" not in body
    assert f"/intern/begivenheder/{event.pk}" not in body


def test_the_event_field_is_absent_while_begivenheder_is_gated(
    client: Client, beboer: Resident, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removed from the form, not disabled — so a POST naming an event is ignored rather than merely
    unrendered.

    THE GATE IS APPLIED HERE rather than taken for granted. This test used to say "the events gate
    is on by default, hence no `events_open` here", and it passed only because begivenheder's
    shipped ACCESS_ROLES happened to be a role tuple. It broke the day that feature opened to the
    whole house — which is precisely the day the assertion still has to hold, because re-gating
    begivenheder later must take this form field with it. A test that depends on a rollout value
    is testing the value, not the behaviour.
    """
    from events import access as events_access

    monkeypatch.setattr(events_access, "ACCESS_ROLES", (Role.ADMINISTRATOR,))
    event = _event(beboer)
    client.force_login(beboer)

    assert "Handler om" not in client.get(BOARD + "opret").content.decode()

    client.post(BOARD + "opret", {"category": Category.NYT, "body": "Indhold", "event": event.pk})

    assert Notice.objects.get().event_id is None
