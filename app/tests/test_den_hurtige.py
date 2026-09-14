"""Den Hurtige: feed lifecycle, notification targeting, and the subscribe endpoint.

Notification *targeting* is tested by collapsing `core.push._run_in_background` to a direct call
and swapping `core.push._dispatch` for a recorder, so the assertions are about who would be
notified — this feature's policy. The shared transport (the delivery loop, the subscribe endpoint,
the VAPID checks, per-topic consent) is tested in test_push.py.
"""

import json
import re
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core import push
from core.models import PushSubscription
from den_hurtige import access, channels
from den_hurtige.channels import Channel

# The VAPID checks moved to core with the push stack; check_channels stayed. See core/checks.py.
from den_hurtige.checks import check_channels
from den_hurtige.models import (
    DEFAULT_DURATION_MINUTES,
    QUICK_EMOJI,
    ChannelMute,
    QuickComment,
    QuickPost,
    QuickReaction,
)
from den_hurtige.views import posts_for, reactions_for
from residents.models import Resident, Role

FEED_URL = "/intern/den-hurtige/"

# The rollout gate exactly as den_hurtige.access ships it, captured at import — before the autouse
# fixture below lifts it. Hardcoding a copy here is what made the Inspektionen tests fail when they
# were added to the real tuple: the fixture kept restoring a narrower, stale set.
#
# The `or` is the other half of that lesson, and is now the half doing the work: the rollout is over
# and the shipped value IS None, so without a fallback `rollout_limited` would restore "no gate at
# all" and quietly turn every test in the staged-rollout section into one that asserts nothing while
# still passing. The explicit tuple keeps them exercising the gate mechanism, which outlived the
# trial — den_hurtige.channels.allowed mirrors it per channel.
GATED_ROLES = access.ACCESS_ROLES or (Role.ADMINISTRATOR, Role.INSPEKTION)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def rollout_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the rollout gate open for the whole module.

    Redundant against the shipped value now that the trial is over and ACCESS_ROLES is None, and
    kept deliberately: it states what every test outside the "staged rollout" section assumes, so
    re-gating the feature for some future reason fails those tests loudly instead of silently
    turning all of them into 403 assertions. The rollout tests turn the gate back on via
    `rollout_limited`, which is why GATED_ROLES above has an explicit fallback.
    """
    monkeypatch.setattr(access, "ACCESS_ROLES", None)


@pytest.fixture
def rollout_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the gate back on — runs after the autouse fixture, so it wins."""
    monkeypatch.setattr(access, "ACCESS_ROLES", GATED_ROLES)


def subscribe(user: Resident, endpoint: str) -> PushSubscription:
    """Register a fake device for `user`, opted in to Den Hurtige (explicit, not relying on the
    field default — these tests are about who this feature reaches)."""
    return PushSubscription.objects.create(
        user=user,
        endpoint=endpoint,
        auth="a" * 22,
        p256dh="p" * 87,
        user_agent="pytest",
        wants_den_hurtige=True,
    )


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

    # Patched on core.push, where the transport lives: den_hurtige.services now only decides *who*
    # is notified, which is exactly what these tests are about.
    monkeypatch.setattr(push, "_dispatch", fake_dispatch)
    monkeypatch.setattr(push, "_run_in_background", lambda fn: fn())
    return recorded


# --- PWA plumbing -------------------------------------------------------------------------------


def test_service_worker_is_served_from_the_site_root(client: Client) -> None:
    """A service worker's scope is capped at its own directory, so only a root-served /sw.js can
    receive pushes for /intern/. Also proves the CMS slug catch-all does not swallow the path."""
    response = client.get("/sw.js")

    assert response.status_code == 200
    assert response["Content-Type"] == "application/javascript"
    body = response.content.decode()
    assert "addEventListener('push'" in body
    assert "{%" not in body and "{{" not in body  # unrendered template syntax would break the worker


def test_manifest_points_at_the_feed_and_ships_every_icon_it_references() -> None:
    """The manifest is hand-maintained JSON that nothing else validates: a start_url outside `scope`
    or a missing icon file makes browsers refuse to install the app, with no server-side error."""
    static_dir = Path(django_settings.BASE_DIR) / "static"
    manifest = json.loads((static_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["start_url"] == FEED_URL
    assert FEED_URL.startswith(manifest["scope"])
    assert {icon["purpose"] for icon in manifest["icons"]} >= {"any", "maskable"}
    for icon in manifest["icons"]:
        assert (static_dir / icon["src"].removeprefix("/static/")).is_file(), icon["src"]


# --- staged rollout ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "",
        "opret",
        "1/kommentar",
        "1/traad",
        "1/slet",
        "1/reaktion",
        "abonner",
        "lyd/generelt",
        "tv-rezz/",
    ],
)
def test_every_endpoint_is_closed_to_non_administrators(
    client: Client, make_resident: Callable[..., Resident], rollout_limited: None, path: str
) -> None:
    """While ACCESS_ROLES is set, a plain resident must not reach any of it — not just the page.
    Parametrised over every route so a new view cannot quietly be added outside the gate."""
    client.force_login(make_resident(email="beboer@gahk.dk"))

    url = FEED_URL + path
    # The page routes are GETs (the bare feed, a named channel, a thread); the rest are POSTs.
    response = client.get(url) if path in ("", "tv-rezz/", "1/traad") else client.post(url)

    assert response.status_code == 403


def test_administrators_get_in(
    client: Client, make_resident: Callable[..., Resident], rollout_limited: None
) -> None:
    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))

    response = client.get(FEED_URL)

    assert response.status_code == 200
    assert "Under test" in response.content.decode()  # testers are told it is not live yet


def test_the_poll_stays_quiet_for_non_administrators(
    client: Client, make_resident: Callable[..., Resident], rollout_limited: None
) -> None:
    """204 rather than 403: htmx would swap a 403 body into the feed. Still leaks nothing."""
    author = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    QuickPost.objects.create(author=author, content="Hemmelig testbesked")
    client.force_login(make_resident(email="beboer@gahk.dk"))

    response = client.get(FEED_URL + "opslag")

    assert response.status_code == 204
    assert b"Hemmelig testbesked" not in response.content


def test_the_sidebar_only_advertises_the_page_to_those_who_can_open_it(
    client: Client, make_resident: Callable[..., Resident], rollout_limited: None
) -> None:
    """A visible link that answers 403 is worse than no link."""
    client.force_login(make_resident(email="beboer@gahk.dk"))
    assert FEED_URL not in client.get("/intern/").content.decode()

    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))
    assert FEED_URL in client.get("/intern/").content.decode()


def test_clearing_access_roles_opens_it_to_every_resident(
    client: Client,
    make_resident: Callable[..., Resident],
    rollout_limited: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented way to end the rollout is ACCESS_ROLES = None. Asserted end to end so the
    switch cannot rot: the roles are read per request, not bound when the views are imported, so
    flipping the constant is genuinely all it takes."""
    client.force_login(make_resident(email="beboer@gahk.dk"))
    assert client.get(FEED_URL).status_code == 403  # gate on

    monkeypatch.setattr(access, "ACCESS_ROLES", None)  # the one documented edit

    assert client.get(FEED_URL).status_code == 200
    assert client.get(FEED_URL + "opslag").status_code == 200
    assert FEED_URL in client.get("/intern/").content.decode()  # and the sidebar follows


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

    assert client.get(FEED_URL).status_code == 403


# --- feed & lifecycle -------------------------------------------------------------------------


def test_feed_requires_login(client: Client) -> None:
    response = client.get(FEED_URL)
    assert response.status_code == 302
    assert "/intern/admin/login" in response["Location"]


def test_feed_purges_expired_posts(client: Client, make_resident: Callable[..., Resident]) -> None:
    user = make_resident(email="a@gahk.dk")
    stale = QuickPost.objects.create(
        author=user, content="Kaffe for en time siden", expires_at=timezone.now() - timedelta(minutes=1)
    )
    fresh = QuickPost.objects.create(author=user, content="Kaffe i køkkenet nu")

    client.force_login(user)
    body = client.get(FEED_URL).content.decode()

    assert not QuickPost.objects.filter(pk=stale.pk).exists()  # hard-deleted, not just hidden
    assert QuickPost.objects.filter(pk=fresh.pk).exists()
    assert "Kaffe i køkkenet nu" in body
    assert "Kaffe for en time siden" not in body


def test_purge_expired_reports_post_count_not_cascaded_rows(
    make_resident: Callable[..., Resident],
) -> None:
    user = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(
        author=user, content="udløbet", expires_at=timezone.now() - timedelta(minutes=1)
    )
    QuickComment.objects.create(post=post, author=user, content="en kommentar")

    assert QuickPost.objects.purge_expired() == 1  # not 2 (the comment cascades with it)


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (1440, "1 døgn"),  # was the default, and the string that started all this
        (1441, "1 døgn"),
        (2880, "2 døgn"),  # the default since the move off 1 døgn
        # Two minutes after posting with that default. Floored to days this read "1 døgn" -- 2878
        # minutes is 1.998 days -- so a message announced its own duration wrongly for a whole day.
        (2878, "2 døgn"),
        (2160, "2 døgn"),  # 36h: rounds to the nearer of the two buckets
        (2159, "1 døgn"),  # just under 1.5 døgn, so the other way
        (4320, "3 døgn"),
        (1439, "23 timer"),  # genuinely 23h59m left, and says so
        (1435, "23 timer"),
        (720, "12 timer"),
        (120, "2 timer"),
        (119, "1 time"),
        (60, "1 time"),
        (59, "59 min"),
        (45, "45 min"),
        (1, "1 min"),
        (0, "udløbet"),
        (-30, "udløbet"),
    ],
)
def test_the_expiry_label_reads_as_a_duration_not_a_pile_of_minutes(minutes: int, expected: str) -> None:
    """The feed used to render "udløber om 1439 min" on every message posted with the default
    duration.

    Rounded to the nearest minute rather than floored: expires_at - now for a post created a moment
    ago is 1439.99 minutes, and flooring the way minutes_left does would label a brand new 1-døgn
    message "23 timer" — a whole bucket down, in front of the person who just posted it. That case
    is pinned by test_the_feed_shows_the_short_expiry_label, which goes through a real post; here
    1439 means genuinely 23h59m left, which "23 timer" states correctly.

    A pure unit test: this is arithmetic on a timedelta, so it needs no database. The boundaries
    are the point — 59/60 and 119/120 are where an off-by-one would live.
    """
    post = QuickPost(expires_at=timezone.now() + timedelta(minutes=minutes))

    assert post.expires_label == expected


def test_the_feed_shows_the_short_expiry_label(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """And shows it as a glyph plus a duration, with the sentence kept in the title. The words
    "udløber om" were most of the header at 11.5px, and the pair of them with a long name is what
    wrapped the delete cross onto its own row."""
    author = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=author, content="Kaffe")
    client.force_login(author)

    body = client.get(FEED_URL).content.decode()

    assert "2 døgn" in body  # the model default, DEFAULT_DURATION_MINUTES
    assert "min</span>" not in body  # no bare minute count in the header any more
    assert 'title="Udløber om 2 døgn"' in body  # the full sentence survives for anyone who hovers
    assert 'class="msg-meta msg-time"' in body  # time and expiry are separate spans now,
    assert 'class="msg-meta msg-expiry"' in body  # so each can be sized independently


def test_create_post_honours_the_chosen_duration(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    user = make_resident(email="a@gahk.dk")
    client.force_login(user)

    client.post(FEED_URL + "opret", {"content": "Fælles opvask", "duration": "120"})

    post = QuickPost.objects.get()
    assert 118 <= post.minutes_left <= 120


def test_create_post_rejects_an_unknown_duration(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    user = make_resident(email="a@gahk.dk")
    client.force_login(user)

    client.post(FEED_URL + "opret", {"content": "Fest", "duration": "99999"})

    # Against the constant, not a hardcoded 60: this assertion went stale the moment the
    # default duration changed.
    assert QuickPost.objects.get().minutes_left <= DEFAULT_DURATION_MINUTES


def test_create_post_stores_an_attached_image(
    client: Client, make_resident: Callable[..., Resident], pushes: list, settings: object, tmp_path: Path
) -> None:
    """MEDIA_ROOT is redirected at the tmp dir: the happy path actually writes a file, and the repo's
    app/media/ is not a dumping ground for test uploads."""
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    user = make_resident(email="a@gahk.dk")
    client.force_login(user)
    image = SimpleUploadedFile("kaffe.jpg", b"\xff\xd8\xff" + b"x" * 512, content_type="image/jpeg")

    client.post(FEED_URL + "opret", {"content": "Se her", "duration": "60", "image": image})

    post = QuickPost.objects.get()
    assert post.image.name.startswith("quick_posts/")
    assert post.image.read() == b"\xff\xd8\xff" + b"x" * 512
    assert (tmp_path / post.image.name).is_file()


@pytest.mark.parametrize("remove", ["expire", "delete"])
def test_an_attached_image_is_erased_with_its_post(
    make_resident: Callable[..., Resident],
    settings: object,
    tmp_path: Path,
    remove: str,
    django_capture_on_commit_callbacks: Callable,
) -> None:
    """The feature promises posts disappear. Django has not deleted FileField files on row delete
    since 1.3, so without the post_delete receiver the text would expire on schedule while the photo
    stayed on disk forever. Both removal paths are covered: purge_expired() issues a *bulk* delete
    that never calls Model.delete(), so it is the one most likely to leak."""
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    user = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(
        author=user,
        content="Se her",
        image=SimpleUploadedFile("k.jpg", b"jpegbytes", content_type="image/jpeg"),
    )
    stored = tmp_path / post.image.name
    assert stored.is_file()

    # core.files defers the storage delete to commit, so the callbacks have to be run for the
    # file to actually go.
    with django_capture_on_commit_callbacks(execute=True):
        if remove == "expire":
            QuickPost.objects.filter(pk=post.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
            QuickPost.objects.purge_expired()
        else:
            post.delete()

    assert not stored.exists(), "the image outlived its post"


@pytest.mark.parametrize(
    ("filename", "content_type", "max_mb", "reason"),
    [
        ("evil.pdf", "application/pdf", 5, "not an image"),
        ("huge.jpg", "image/jpeg", 0, "over the size cap"),  # cap 0 → any file is too big
        # An SVG is a document: served from our own /media/ origin a direct navigation executes its
        # <script> as us. This feed accepted it until the validators were consolidated in
        # core.uploads, because the old check only asked whether the content type began "image/".
        ("evil.svg", "image/svg+xml", 5, "an SVG can carry script"),
        # content_type is a client-supplied hint; the extension is what the file is served as.
        ("evil.svg", "image/png", 5, "a disguised extension"),
    ],
)
def test_create_post_drops_a_bad_image_but_keeps_the_message(
    client: Client,
    make_resident: Callable[..., Resident],
    pushes: list,
    settings: object,
    tmp_path: Path,
    filename: str,
    content_type: str,
    max_mb: int,
    reason: str,
) -> None:
    """A rejected attachment must not take the post down with it — the text is the point, the image
    is a nicety, and re-typing an urgent message because a file was wrong is the worst outcome."""
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    settings.QUICK_POST_MAX_MB = max_mb  # type: ignore[attr-defined]
    user = make_resident(email="a@gahk.dk")
    client.force_login(user)
    upload = SimpleUploadedFile(filename, b"x" * 2048, content_type=content_type)

    response = client.post(
        FEED_URL + "opret", {"content": "Vigtig besked", "duration": "60", "image": upload}, follow=True
    )

    post = QuickPost.objects.get()
    assert post.content == "Vigtig besked"
    assert not post.image, f"{reason}: the file should not have been stored"
    assert not any(tmp_path.rglob("*")), "nothing should have been written to MEDIA_ROOT"
    assert any("blev ikke gemt" in str(m) for m in response.context["messages"])


def test_delete_post_is_author_or_administrator_only(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    author = make_resident(email="a@gahk.dk")
    other = make_resident(email="b@gahk.dk")
    admin = make_resident(email="c@gahk.dk", roles=(Role.ADMINISTRATOR,))
    post = QuickPost.objects.create(author=author, content="mit opslag")

    client.force_login(other)
    assert client.post(f"{FEED_URL}{post.pk}/slet").status_code == 403
    assert QuickPost.objects.filter(pk=post.pk).exists()

    client.force_login(admin)
    assert client.post(f"{FEED_URL}{post.pk}/slet").status_code == 302
    assert not QuickPost.objects.filter(pk=post.pk).exists()


def test_den_hurtige_pages_leak_no_template_syntax(
    client: Client, make_resident: Callable[..., Resident], settings: object
) -> None:
    """Django's {# … #} is single-line only; a multi-line one renders verbatim onto the page. This
    has bitten base.html and both feed templates already — assert every Den Hurtige surface is
    clean, with content present so the post/comment branches actually render."""
    # Any non-empty pair: this only needs `push_configured` to be true so the subscribe bar renders
    # and its markup is covered. Whether a pair is *valid* is test_push.py's business.
    settings.VAPID_PUBLIC_KEY = "test-public-key"  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = "test-private-key"  # type: ignore[attr-defined]
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Kaffe i køkkenet")
    QuickComment.objects.create(post=post, author=author, content="Jeg kommer")
    client.force_login(author)

    for path in (FEED_URL, FEED_URL + "opslag", f"{FEED_URL}{post.pk}/traad"):
        body = client.get(path).content.decode()
        assert "Kaffe i køkkenet" in body, f"{path} rendered no posts — the check would be vacuous"
        for leaked in ("{#", "#}", "{%", "%}", "{{", "}}"):
            assert leaked not in body, f"{path} leaked {leaked!r}"

    # And the panel fragment, which is a different template from the page above.
    fragment = client.get(f"{FEED_URL}{post.pk}/traad", HTTP_HX_REQUEST="true").content.decode()
    assert "Jeg kommer" in fragment, "the thread fragment rendered no replies"
    for leaked in ("{#", "#}", "{%", "%}", "{{", "}}"):
        assert leaked not in fragment, f"the thread fragment leaked {leaked!r}"


def test_the_poll_resolves_roles_once_per_request(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The access gate, the sidebar's effective_roles and can_preview all ask for the role set. Each
    used to hit the DB, so this endpoint — which every open tab calls every 20 seconds — cost three
    RoleAssignment queries and three active_period lookups. residents.permissions.real_roles now
    memoises per request; this pins that down, since the regression would be silent."""
    author = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=author, content="Kaffe")
    client.force_login(author)
    client.get(FEED_URL + "opslag")  # warm any one-off caches

    with CaptureQueriesContext(connection) as captured:
        client.get(FEED_URL + "opslag")

    sql = [q["sql"] for q in captured.captured_queries]
    assert sum("residents_roleassignment" in s for s in sql) == 1
    assert sum("residents_residency" in s for s in sql) == 1


def test_feed_items_returns_only_the_post_list(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The poll target is a fragment, not a page — swapping a full document into #js-feed would
    nest a second <html> and a second compose form inside the feed."""
    user = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=user, content="Kaffe i køkkenet")
    client.force_login(user)

    body = client.get(FEED_URL + "opslag").content.decode()

    assert "Kaffe i køkkenet" in body
    assert "<html" not in body
    assert "Slå op" not in body  # the compose form lives outside the polled region


def test_feed_items_picks_up_a_post_made_after_the_page_loaded(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    reader = make_resident(email="a@gahk.dk")
    poster = make_resident(email="b@gahk.dk")
    client.force_login(reader)
    assert "Fest i kælderen" not in client.get(FEED_URL).content.decode()

    QuickPost.objects.create(author=poster, content="Fest i kælderen")

    assert "Fest i kælderen" in client.get(FEED_URL + "opslag").content.decode()


def test_feed_items_expires_posts_live(client: Client, make_resident: Callable[..., Resident]) -> None:
    user = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=user, content="udløbet", expires_at=timezone.now() - timedelta(minutes=1))
    client.force_login(user)

    body = client.get(FEED_URL + "opslag").content.decode()

    assert "udløbet" not in body
    assert not QuickPost.objects.exists()  # the poll purges too, so the feed drains itself


def test_feed_items_returns_204_for_an_expired_session(client: Client) -> None:
    """htmx follows redirects, so @login_required here would swap the login page into the feed.
    204 means 'no update' and leaks nothing."""
    response = client.get(FEED_URL + "opslag")

    assert response.status_code == 204
    assert response.content == b""


# --- notification targeting -------------------------------------------------------------------


def test_new_post_notifies_subscribers_except_the_author(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    author = make_resident(email="a@gahk.dk")
    other = make_resident(email="b@gahk.dk")
    subscribe(author, "https://push.example/author")
    subscribe(other, "https://push.example/other")

    client.force_login(author)
    client.post(FEED_URL + "opret", {"content": "Der er kage i køkkenet", "duration": "60"})

    (recipients, payload) = pushes[0]
    assert recipients == [other.pk]
    assert "Der er kage i køkkenet" in payload["body"]
    assert payload["url"] == FEED_URL


def test_comment_notifies_only_the_original_poster_by_default(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    author = make_resident(email="a@gahk.dk")
    commenter = make_resident(email="b@gahk.dk")
    bystander = make_resident(email="c@gahk.dk")
    for user in (author, commenter, bystander):
        subscribe(user, f"https://push.example/{user.pk}")
    post = QuickPost.objects.create(author=author, content="Nogen der har en boremaskine?")

    client.force_login(commenter)
    client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "Ja, kom forbi 42", "notify": "op"})

    (recipients, _payload) = pushes[0]
    assert recipients == [author.pk]


def test_comment_can_notify_everyone_except_the_commenter(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    author = make_resident(email="a@gahk.dk")
    commenter = make_resident(email="b@gahk.dk")
    bystander = make_resident(email="c@gahk.dk")
    for user in (author, commenter, bystander):
        subscribe(user, f"https://push.example/{user.pk}")
    post = QuickPost.objects.create(author=author, content="Fest i kælderen?")

    client.force_login(commenter)
    client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "Ja! Kl 21", "notify": "alle"})

    (recipients, _payload) = pushes[0]
    assert recipients == sorted([author.pk, bystander.pk])


def test_commenting_on_your_own_post_notifies_nobody(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    author = make_resident(email="a@gahk.dk")
    subscribe(make_resident(email="b@gahk.dk"), "https://push.example/other")
    post = QuickPost.objects.create(author=author, content="Kaffe kl 15")

    client.force_login(author)
    client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "Rettelse: kl 16", "notify": "op"})

    assert pushes == []  # the only candidate recipient would have been the commenter


def test_nothing_is_sent_when_vapid_keys_are_missing(
    client: Client, make_resident: Callable[..., Resident], pushes: list, settings: object
) -> None:
    settings.VAPID_PUBLIC_KEY = ""  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = ""  # type: ignore[attr-defined]
    author = make_resident(email="a@gahk.dk")
    subscribe(make_resident(email="b@gahk.dk"), "https://push.example/other")

    client.force_login(author)
    client.post(FEED_URL + "opret", {"content": "Hej", "duration": "60"})

    assert pushes == []


# --- emoji reactions -----------------------------------------------------------------------------

THUMB = "\U0001f44d"
PARTY = "\U0001f389"


def react(client: Client, post: QuickPost, emoji: str) -> object:
    return client.post(f"{FEED_URL}{post.pk}/reaktion", {"emoji": emoji})


def test_a_reaction_toggles_off_when_tapped_again(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The unique constraint is what makes this a toggle rather than a counter: tapping your own
    emoji again removes it instead of adding a second row."""
    user = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=user, content="Kaffe")
    client.force_login(user)

    react(client, post, THUMB)
    assert QuickReaction.objects.count() == 1

    react(client, post, THUMB)
    assert QuickReaction.objects.count() == 0


def test_reactions_count_per_emoji_and_flag_your_own(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Counts are per emoji across people; `mine` is what makes your own pill render active."""
    author = make_resident(email="a@gahk.dk")
    other = make_resident(email="b@gahk.dk")
    third = make_resident(email="c@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Fest i kaelderen")

    client.force_login(author)
    react(client, post, THUMB)
    client.force_login(third)
    react(client, post, PARTY)
    client.force_login(other)
    response = react(client, post, THUMB)  # a third person, same emoji as the author

    rows = {r["emoji"]: r for r in reactions_for(post, other.pk)}
    assert rows[THUMB]["count"] == 2
    assert rows[PARTY]["count"] == 1
    assert rows[THUMB]["mine"] is True  # `other` used this one
    assert rows[PARTY]["mine"] is False  # ...but not this one
    assert b"<html" not in response.content  # a fragment, not a whole page


@pytest.mark.parametrize(
    "emoji",
    [
        "LOL",
        "123",
        "",
        "x" * 80,
        "<b>hi</b>",
        THUMB + " ok",  # an emoji smuggling text alongside it
    ],
)
def test_only_emoji_are_accepted_as_reactions(
    client: Client, make_resident: Callable[..., Resident], emoji: str
) -> None:
    """Allowing any emoji means arbitrary text reaches the database, so the validator is a real
    gate rather than a formality."""
    user = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=user, content="Kaffe")
    client.force_login(user)

    react(client, post, emoji)

    assert not QuickReaction.objects.exists()


@pytest.mark.parametrize(
    "emoji",
    [
        THUMB,
        "\U0001f469\u200d\U0001f467",  # ZWJ sequence
        "\U0001f44d\U0001f3fd",  # skin-tone modifier
        "\u2764\ufe0f",  # VS16 presentation selector
    ],
)
def test_real_emoji_are_accepted(client: Client, make_resident: Callable[..., Resident], emoji: str) -> None:
    """Plain, ZWJ-joined, skin-toned and VS16 forms all have to get through."""
    user = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=user, content="Kaffe")
    client.force_login(user)

    react(client, post, emoji)

    assert QuickReaction.objects.get().emoji == emoji


def test_reacting_notifies_nobody(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    """Reactions are deliberately silent: a dorm-wide feed where every thumbs-up buzzes every phone
    is the noise this feature exists to remove."""
    author = make_resident(email="a@gahk.dk")
    reactor = make_resident(email="b@gahk.dk")
    subscribe(author, "https://push.example/author")
    post = QuickPost.objects.create(author=author, content="Kaffe")

    client.force_login(reactor)
    react(client, post, THUMB)

    assert pushes == []


def test_reactions_die_with_their_post(client: Client, make_resident: Callable[..., Resident]) -> None:
    user = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=user, content="Kaffe")
    client.force_login(user)
    react(client, post, THUMB)

    QuickPost.objects.filter(pk=post.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    QuickPost.objects.purge_expired()

    assert not QuickReaction.objects.exists()


# --- chat layout ---------------------------------------------------------------------------------


def test_the_feed_reads_oldest_first(client: Client, make_resident: Callable[..., Resident]) -> None:
    """Chat order: the newest message sits next to the composer at the bottom. The model's own
    ordering stays newest-first for the admin, so this is asserted on the rendered page."""
    user = make_resident(email="a@gahk.dk")
    first = QuickPost.objects.create(author=user, content="Foerste besked")
    second = QuickPost.objects.create(author=user, content="Anden besked")
    client.force_login(user)

    body = client.get(FEED_URL).content.decode()

    assert body.index("Foerste besked") < body.index("Anden besked")
    assert list(QuickPost.objects.all()) == [second, first]  # admin still sees newest-first


def test_consecutive_messages_from_one_person_are_grouped(
    make_resident: Callable[..., Resident],
) -> None:
    """Without grouping a short back-and-forth renders as a stack of cards — the forum look this
    redesign removes."""
    author = make_resident(email="a@gahk.dk")
    other = make_resident(email="b@gahk.dk")
    QuickPost.objects.create(author=author, content="Foerst")
    QuickPost.objects.create(author=author, content="Og saa")  # same author, seconds apart
    QuickPost.objects.create(author=other, content="Svar")  # a different author breaks the group

    request = RequestFactory().get(FEED_URL)
    request.user = author

    assert [p.grouped for p in posts_for(request, channels.DEFAULT)] == [False, True, False]


def test_a_continuation_repeats_no_name_and_the_run_carries_one_avatar(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The formatting bug this replaced: a follow-up message from the same person dropped the name
    but kept the whole header line, so it rendered a stray "14:32 · ⏱ 45 min" attached to nobody.

    Asserted through the rendered feed rather than on the flags, because the flags were already
    right — it was the template that spent them wrongly. One name and one filled avatar per run is
    the contract: the name introduces it, the avatar sits beside where it ends."""
    author = make_resident(email="a@gahk.dk", first_name="Ada", last_name="Byron")
    # Named explicitly so neither resident's initials can collide with the author's: make_resident
    # otherwise generates names, and "count the initials" then passes or fails on the draw.
    reader = make_resident(email="b@gahk.dk", first_name="Rasmus", last_name="Toft")
    QuickPost.objects.create(author=author, content="Foerste")
    QuickPost.objects.create(author=author, content="Anden")
    client.force_login(reader)  # not the author, so nothing is right-aligned away

    body = client.get(FEED_URL).content.decode()

    assert body.count('class="msg-name"') == 1  # the run is introduced once, not per message

    # One avatar box per message (they hold the column open), but only the run's LAST one is
    # filled. Counted by matching the box and asking whether it has any content, rather than by
    # searching for "AB": initials are two letters and turn up inside unrelated markup.
    boxes = re.findall(r'<div class="msg-avatar" aria-hidden="true">(.*?)</div>', body, re.DOTALL)
    assert len(boxes) == 2
    assert [bool(b.strip()) for b in boxes] == [False, True]
    assert "AB" in boxes[1]

    # Both messages still carry their own clock: a run can span five minutes, and in a feed that
    # deletes itself the per-message countdown is most of the point.
    assert body.count('class="msg-meta msg-time"') == 2


def test_the_feed_marks_messages_as_swipe_targets_but_not_the_thread_parent(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """frontend/src/feed.ts finds swipeable messages by [data-msg-swipe], and reaches the gesture's
    effect through the controls rendered beside it — the "N svar" anchor and the delete form —
    rather than by building a request of its own. This pins that contract from the template side.

    The thread parent must NOT carry it: it is rendered inside the panel a right-swipe opens, so
    swiping it would re-open the thread being read."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Kaffe om fem")
    client.force_login(author)

    feed_body = client.get(FEED_URL).content.decode()

    assert "data-msg-swipe" in feed_body
    assert 'class="msg-hint msg-hint-thread"' in feed_body
    assert 'class="msg-hint msg-hint-del"' in feed_body  # own message, so delete is reachable
    assert 'class="msg-replies"' in feed_body

    parent_body = client.get(f"{FEED_URL}{post.pk}/traad").content.decode()

    assert "data-msg-swipe" not in parent_body
    assert "msg-hint" not in parent_body


def test_the_end_of_a_run_is_marked_for_the_avatar_and_the_tail(
    make_resident: Callable[..., Resident],
) -> None:
    """`grouped` says "something of mine is above me", which is what decides the name and the top
    corners. The avatar sits at the BOTTOM of a run and the bubble's tail hangs off its last
    message, so both need the opposite fact — and a Django template cannot look ahead to the next
    message to work it out. Hence group_end, set in the same pass (views.posts_for).

    The last message of the feed ends its run by definition: nothing follows it to break it. That
    case is the one worth pinning down, because getting it wrong leaves the NEWEST message — the
    one everybody is looking at — as the only one with no avatar and no tail."""
    author = make_resident(email="a@gahk.dk")
    other = make_resident(email="b@gahk.dk")
    QuickPost.objects.create(author=author, content="Foerst")
    QuickPost.objects.create(author=author, content="Og saa")  # same run, so the first one is not the end
    QuickPost.objects.create(author=other, content="Svar")  # breaks it, so "Og saa" was the end

    request = RequestFactory().get(FEED_URL)
    request.user = author

    posts = posts_for(request, channels.DEFAULT)

    assert [p.group_end for p in posts] == [False, True, True]
    # The two flags are independent, not opposites: a lone message both starts and ends its run.
    assert [(p.grouped, p.group_end) for p in posts][2] == (False, True)


def test_a_single_message_ends_its_own_run(
    make_resident: Callable[..., Resident],
) -> None:
    """The one-message feed, which is what a quiet channel looks like most of the week."""
    author = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=author, content="Alene")

    request = RequestFactory().get(FEED_URL)
    request.user = author

    (post,) = posts_for(request, channels.DEFAULT)

    assert post.grouped is False
    assert post.group_end is True


def test_the_feed_costs_no_extra_query_per_reaction(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Reactions are counted in Python over a prefetch. A per-post aggregate would be an N+1 on a
    page that re-renders itself every 20 seconds."""
    user = make_resident(email="a@gahk.dk")
    client.force_login(user)
    for n in range(3):
        post = QuickPost.objects.create(author=user, content=f"Besked {n}")
        react(client, post, THUMB)
    client.get(FEED_URL + "opslag")  # warm

    with CaptureQueriesContext(connection) as captured:
        client.get(FEED_URL + "opslag")

    hits = [q for q in captured.captured_queries if "den_hurtige_quickreaction" in q["sql"]]
    assert len(hits) == 1, "one prefetch for the whole page, not one query per message"


def test_a_message_notification_is_titled_with_the_sender(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    """Every platform already labels a notification with the app it came from — iOS renders
    "from Den Hurtige" under the title, taken from the manifest name. Repeating it in the title
    spent the most valuable line on the lock screen saying nothing, and pushed who-said-what into
    the body. Sender in the title, message in the body, as in any chat app."""
    author = make_resident(email="a@gahk.dk", first_name="Magnus", last_name="Pedersen")
    subscribe(make_resident(email="b@gahk.dk"), "https://push.example/other")

    client.force_login(author)
    client.post(FEED_URL + "opret", {"content": "Hello, world", "duration": "60"})

    (_recipients, payload) = pushes[0]
    assert payload["head"] == "Magnus Pedersen"
    assert payload["body"] == "Hello, world"
    # The two halves of the redundancy that made this look wrong on the phone:
    assert "Den Hurtige" not in payload["head"]
    assert "Magnus Pedersen" not in payload["body"]


def test_a_reply_notification_says_who_replied(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    """A reply still has to be distinguishable from a new message, but "svarede" carries that
    without naming the app again."""
    author = make_resident(email="a@gahk.dk")
    commenter = make_resident(email="b@gahk.dk", first_name="Magnus", last_name="Pedersen")
    subscribe(author, "https://push.example/author")
    post = QuickPost.objects.create(author=author, content="Kaffe")

    client.force_login(commenter)
    client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "Hello, world", "notify": "op"})

    (_recipients, payload) = pushes[0]
    assert payload["head"] == "Magnus Pedersen svarede"
    assert payload["body"] == "Hello, world"
    assert "Den Hurtige" not in payload["head"]


def test_posting_shows_no_confirmation_message(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    """The message appearing in the feed is the confirmation. A toast on every send is noise in a
    chat — but the warnings that change what was posted must still come through."""
    user = make_resident(email="a@gahk.dk")
    client.force_login(user)

    response = client.post(
        FEED_URL + "opret", {"content": "Kaffe i koekkenet", "duration": "60"}, follow=True
    )

    assert list(response.context["messages"]) == []


def test_a_rejected_image_still_warns(
    client: Client, make_resident: Callable[..., Resident], pushes: list, settings: object, tmp_path: Path
) -> None:
    """Removing the success toast must not silence the warnings — those tell you the post is not
    what you thought you sent."""
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    client.force_login(make_resident(email="a@gahk.dk"))

    response = client.post(
        FEED_URL + "opret",
        {
            "content": "Se her",
            "duration": "60",
            "image": SimpleUploadedFile("evil.pdf", b"x" * 100, content_type="application/pdf"),
        },
        follow=True,
    )

    assert any("blev ikke gemt" in str(m) for m in response.context["messages"])


def test_the_reaction_picker_offers_one_tap_emoji(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """A bare text input was the first attempt: on a desktop browser that is a cursor and no help.
    The shortlist has to render as real buttons, with the free field kept for anything else."""
    user = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=user, content="Kaffe")
    client.force_login(user)

    html = client.get(FEED_URL).content.decode()

    assert html.count('class="emoji-choice"') == len(QUICK_EMOJI)
    for emoji in QUICK_EMOJI:
        assert emoji in html
    assert 'class="emoji-other"' in html  # "any emoji" is still reachable
    assert 'name="emoji"' in html


def test_a_quick_emoji_button_reacts(client: Client, make_resident: Callable[..., Resident]) -> None:
    """The shortlist posts the same endpoint as the pills, so it toggles identically."""
    user = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=user, content="Kaffe")
    client.force_login(user)

    react(client, post, QUICK_EMOJI[0])

    assert QuickReaction.objects.get().emoji == QUICK_EMOJI[0]


def test_the_heart_in_the_shortlist_matches_what_keyboards_send(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """U+2764 without U+FE0F would count separately from the heart every phone keyboard emits,
    silently splitting one reaction into two columns."""
    user = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=user, content="Kaffe")
    other = make_resident(email="b@gahk.dk")
    client.force_login(user)

    react(client, post, "\u2764\ufe0f")  # from the picker
    client.force_login(other)
    react(client, post, "\u2764\ufe0f")  # typed on a phone

    assert "\u2764\ufe0f" in QUICK_EMOJI
    assert [r["count"] for r in reactions_for(post, user.pk)] == [2]


# --- one reaction per person ---------------------------------------------------------------------


def test_a_second_emoji_replaces_your_first(client: Client, make_resident: Callable[..., Resident]) -> None:
    """One person, one reaction per message. Picking a different emoji moves yours instead of
    stacking a second, so nobody can carpet a message in five reactions."""
    user = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=user, content="Kaffe")
    client.force_login(user)

    react(client, post, THUMB)
    react(client, post, PARTY)

    assert QuickReaction.objects.count() == 1
    assert QuickReaction.objects.get().emoji == PARTY
    assert [r["emoji"] for r in reactions_for(post, user.pk)] == [PARTY]


def test_different_people_still_react_independently(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The limit is per person, not per message — a message can still collect many reactions."""
    author = make_resident(email="a@gahk.dk")
    other = make_resident(email="b@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Kaffe")

    client.force_login(author)
    react(client, post, THUMB)
    client.force_login(other)
    react(client, post, PARTY)

    assert QuickReaction.objects.count() == 2


@pytest.mark.parametrize(
    "pasted",
    [
        "\U0001f44d\U0001f389",  # two pasted together
        "\U0001f44d\U0001f389\U0001f525",  # three
        "\U0001f44d\U0001f44d",  # the same one twice
    ],
)
def test_pasting_several_emoji_is_rejected(
    client: Client, make_resident: Callable[..., Resident], pasted: str
) -> None:
    """The "Anden emoji" field accepts a paste, and several emoji are all category So — so a length
    check alone let someone land three of them in a single reaction bubble."""
    user = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=user, content="Kaffe")
    client.force_login(user)

    react(client, post, pasted)

    assert not QuickReaction.objects.exists()


@pytest.mark.parametrize(
    "emoji",
    [
        "\U0001f1e9\U0001f1f0",  # flag: two regional indicators are one emoji
        "1\ufe0f\u20e3",  # keycap: the one place a digit is legitimate
    ],
)
def test_multi_codepoint_emoji_are_still_one_emoji(
    client: Client, make_resident: Callable[..., Resident], emoji: str
) -> None:
    """Rejecting multiple emoji must not reject the single ones that happen to be several code
    points — the naive fix breaks flags, keycaps and family sequences."""
    user = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=user, content="Kaffe")
    client.force_login(user)

    react(client, post, emoji)

    assert QuickReaction.objects.get().emoji == emoji


# --- images on replies ----------------------------------------------------------------------------


def test_a_reply_can_carry_an_image(
    client: Client, make_resident: Callable[..., Resident], pushes: list, settings: object, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    author = make_resident(email="a@gahk.dk")
    replier = make_resident(email="b@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Hvem har mistet en nogle?")
    client.force_login(replier)

    client.post(
        f"{FEED_URL}{post.pk}/kommentar",
        {
            "content": "Den her?",
            "image": SimpleUploadedFile("noegle.jpg", b"\xff\xd8\xffx" * 40, content_type="image/jpeg"),
        },
    )

    comment = QuickComment.objects.get()
    assert comment.image.name.startswith("quick_comments/")
    assert (tmp_path / comment.image.name).is_file()


def test_a_reply_image_is_erased_with_its_post(
    client: Client,
    make_resident: Callable[..., Resident],
    settings: object,
    tmp_path: Path,
    django_capture_on_commit_callbacks: Callable,
) -> None:
    """Replies are removed by cascade, never by Model.delete(), so the file-cleanup receiver has to
    be registered for QuickComment as well or the photo outlives the thread."""
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    user = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=user, content="Kaffe")
    comment = QuickComment.objects.create(
        post=post,
        author=user,
        content="Se her",
        image=SimpleUploadedFile("k.jpg", b"jpegbytes", content_type="image/jpeg"),
    )
    stored = tmp_path / comment.image.name
    assert stored.is_file()

    with django_capture_on_commit_callbacks(execute=True):
        post.delete()  # cascades to the reply

    assert not stored.exists(), "the reply image outlived its thread"


def test_a_bad_reply_image_is_dropped_but_the_reply_survives(
    client: Client, make_resident: Callable[..., Resident], pushes: list, settings: object, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    author = make_resident(email="a@gahk.dk")
    replier = make_resident(email="b@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Kaffe")
    client.force_login(replier)

    response = client.post(
        f"{FEED_URL}{post.pk}/kommentar",
        {
            "content": "Jeg kommer",
            "image": SimpleUploadedFile("evil.pdf", b"x" * 100, content_type="application/pdf"),
        },
        follow=True,
    )

    comment = QuickComment.objects.get()
    assert comment.content == "Jeg kommer"
    assert not comment.image
    assert any("blev ikke gemt" in str(m) for m in response.context["messages"])


def test_the_reply_form_accepts_files(client: Client, make_resident: Callable[..., Resident]) -> None:
    """Without the multipart encoding the browser posts the filename as text and the image is
    silently lost — the kind of thing no server-side test would otherwise notice.

    Reads the THREAD now, not the feed: replies moved into the side panel, so the feed carries a
    "N svar" link and no reply form at all."""
    user = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=user, content="Kaffe")
    client.force_login(user)

    html = client.get(f"{FEED_URL}{post.pk}/traad").content.decode()

    form = html.split('class="reply-form"')[1].split("</form>")[0]
    assert 'enctype="multipart/form-data"' in html.split('class="reply-form"')[0][-120:] or (
        "multipart/form-data" in form
    )
    assert 'type="file"' in form
    assert 'accept="image/*"' in form


# --- zoom lockdown -------------------------------------------------------------------------------


def test_den_hurtige_locks_the_viewport(client: Client, make_resident: Callable[..., Resident]) -> None:
    """One-handed chat use: a stray pinch or a double tap meant for a reaction leaves the composer
    off-screen. The meta covers Android and desktop; feed.ts covers iOS, which ignores it."""
    client.force_login(make_resident(email="a@gahk.dk"))

    html = client.get(FEED_URL).content.decode()

    assert "user-scalable=no" in html
    assert "maximum-scale=1" in html
    assert "no-zoom" in html  # what the CSS and the gesture handlers key off
    assert "chat-page" in html  # the layout hook that fills the scroll area
    assert "viewport-fit=cover" in html  # so the composer can use the safe-area inset


def test_the_rest_of_intern_keeps_pinch_zoom(client: Client, make_resident: Callable[..., Resident]) -> None:
    """Locking zoom site-wide would be a real accessibility regression — the alumneliste and long
    CMS pages are exactly where people pinch. The override has to stay scoped to Den Hurtige."""
    client.force_login(make_resident(email="a@gahk.dk"))

    html = client.get("/intern/").content.decode()

    assert "user-scalable=no" not in html
    assert "no-zoom" not in html


# --- Inspektionen: viewers and moderators ---------------------------------------------------------


def test_inspektionen_can_open_the_chat_during_the_trial(
    client: Client, make_resident: Callable[..., Resident], rollout_limited: None
) -> None:
    """Inspektionen are in the trial alongside the administrators."""
    client.force_login(make_resident(email="insp@gahk.dk", roles=(Role.INSPEKTION,)))

    assert client.get(FEED_URL).status_code == 200
    assert FEED_URL in client.get("/intern/").content.decode()  # and the sidebar advertises it


def test_inspektionen_can_delete_someone_elses_message(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """They keep the kollegium's house rules, so they moderate the chat too."""
    author = make_resident(email="a@gahk.dk")
    inspektion = make_resident(email="insp@gahk.dk", roles=(Role.INSPEKTION,))
    post = QuickPost.objects.create(author=author, content="Noget upassende")

    client.force_login(inspektion)
    response = client.post(f"{FEED_URL}{post.pk}/slet")

    assert response.status_code == 302
    assert not QuickPost.objects.filter(pk=post.pk).exists()


def test_a_plain_resident_still_cannot_delete_other_messages(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Min besked")

    client.force_login(make_resident(email="beboer@gahk.dk"))

    assert client.post(f"{FEED_URL}{post.pk}/slet").status_code == 403
    assert QuickPost.objects.filter(pk=post.pk).exists()


@pytest.mark.parametrize(
    ("roles", "expected"),
    [((Role.INSPEKTION,), True), ((Role.ADMINISTRATOR,), True), ((), False), ((Role.AK,), False)],
)
def test_only_moderators_see_the_delete_button_on_other_messages(
    client: Client, make_resident: Callable[..., Resident], roles: tuple, expected: bool
) -> None:
    """The button has to follow the permission, or people meet a 403 they could not have predicted."""
    author = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=author, content="Andres besked")
    client.force_login(make_resident(email="viewer@gahk.dk", roles=roles))

    html = client.get(FEED_URL).content.decode()

    assert ("Slet besked" in html) is expected


# --- channels -----------------------------------------------------------------------------------


OTHER = "tv-rezz"


def test_the_bare_url_renders_the_default_channel(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """static/manifest.json uses /intern/den-hurtige/ as the PWA's `id`, so it must keep answering
    with a real page forever: a changed id makes every phone treat the next deploy as a *different*
    installed app. Not a redirect, not an index of channels — the default feed itself."""
    client.force_login(make_resident(email="a@gahk.dk"))

    response = client.get(FEED_URL)

    assert response.status_code == 200
    assert response.context["channel"].slug == channels.DEFAULT.slug


def test_posts_do_not_leak_between_channels(client: Client, make_resident: Callable[..., Resident]) -> None:
    author = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=author, content="Kaffe i koekkenet", channel="generelt")
    QuickPost.objects.create(author=author, content="Vi tager i byen", channel=OTHER)
    client.force_login(author)

    default_body = client.get(FEED_URL).content.decode()
    other_body = client.get(f"{FEED_URL}{OTHER}/").content.decode()

    assert "Kaffe i koekkenet" in default_body
    assert "Vi tager i byen" not in default_body
    assert "Vi tager i byen" in other_body
    assert "Kaffe i koekkenet" not in other_body


def test_the_composer_posts_into_the_channel_it_was_rendered_in(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The channel travels in a hidden field rendered server-side, so it cannot disagree with the
    page — and the redirect goes back to that channel rather than dumping you on the default."""
    client.force_login(make_resident(email="a@gahk.dk"))

    response = client.post(FEED_URL + "opret", {"content": "Afgang 21", "kanal": OTHER})

    assert QuickPost.objects.get().channel == OTHER
    assert response["Location"] == f"{FEED_URL}{OTHER}/"


def test_a_post_with_an_unknown_channel_lands_in_the_default_one(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """An urgent message must not be lost to a hidden field the author never saw — the same call
    create_post already makes for an unrecognised duration."""
    client.force_login(make_resident(email="a@gahk.dk"))

    client.post(FEED_URL + "opret", {"content": "Uden kanal", "kanal": "findes-ikke"})

    assert QuickPost.objects.get().channel == channels.DEFAULT.slug


def test_the_composer_offers_the_channels_own_default_duration(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan for tonight and a lost bike key go stale on different schedules.

    Patches in a channel with a duration nothing else uses, because every real channel now defaults
    to the same 2 døgn. Comparing two channels that agree would assert 2880 == 2880 and pass just as
    happily against a view that ignored the channel and always handed back channels.DEFAULT --
    which is the regression this test exists to catch."""
    slow = Channel("langsom", "Langsom", "flash", "", 30)
    monkeypatch.setattr(channels, "CHANNELS", (*channels.CHANNELS, slow))
    monkeypatch.setattr(channels, "BY_SLUG", {c.slug: c for c in channels.CHANNELS})
    client.force_login(make_resident(email="a@gahk.dk"))

    default_page = client.get(FEED_URL)
    slow_page = client.get(f"{FEED_URL}{slow.slug}/")

    assert default_page.context["default_duration"] == channels.DEFAULT.default_duration
    assert slow_page.context["default_duration"] == 30
    assert default_page.context["default_duration"] != 30  # the two really are distinguishable


def test_replying_and_deleting_return_to_the_posts_own_channel(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Actions on a post take the channel from the post, never from the request.

    A reply now lands back on the THREAD rather than the channel: without JS it was written on the
    standalone thread page, and returning to the bottom of the feed loses the conversation you were
    having. Deleting still returns to the channel — the post it belonged to is gone."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Vi tager i byen", channel=OTHER)
    client.force_login(author)

    reply = client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "Jeg er med"})
    assert reply["Location"] == f"{FEED_URL}{post.pk}/traad"

    assert client.post(f"{FEED_URL}{post.pk}/slet")["Location"] == f"{FEED_URL}{OTHER}/"


def test_an_unknown_channel_is_404_on_the_page_and_204_on_the_poll(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The poll must never swap an error body into the middle of the feed — the same reason an
    expired session gets a 204 there rather than a redirect to the login page."""
    client.force_login(make_resident(email="a@gahk.dk"))

    assert client.get(FEED_URL + "findes-ikke/").status_code == 404
    assert client.get(FEED_URL + "opslag?kanal=findes-ikke").status_code == 204


def test_the_poll_without_a_channel_serves_the_default_one(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """A tab left open across the deploy that added channels keeps polling the old URL. It must get
    the default feed, not a 204 that silently freezes the page."""
    author = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=author, content="Kaffe i koekkenet")
    client.force_login(author)

    response = client.get(FEED_URL + "opslag")

    assert response.status_code == 200
    assert "Kaffe i koekkenet" in response.content.decode()


def test_expired_posts_are_purged_across_every_channel_not_just_the_one_being_read(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The sharp edge of the whole feature. Scoping the purge to the channel being viewed would
    leave a quiet channel's expired posts — and their images — sitting there until the half-hourly
    cron, quietly turning "gone in an hour" into "gone in an hour, in the busy channel"."""
    author = make_resident(email="a@gahk.dk")
    stale = QuickPost.objects.create(
        author=author,
        content="Udloebet i den anden kanal",
        channel=OTHER,
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    client.force_login(author)

    client.get(FEED_URL)  # reading the DEFAULT channel

    assert not QuickPost.objects.filter(pk=stale.pk).exists()


def test_the_channel_picker_counts_live_posts_per_channel(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Live, not unread: an expired post must stop being counted without anyone visiting."""
    author = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=author, content="En", channel=OTHER)
    QuickPost.objects.create(author=author, content="To", channel=OTHER)
    QuickPost.objects.create(
        author=author,
        content="Doed",
        channel=OTHER,
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    client.force_login(author)

    tabs = {tab.channel.slug: tab.live for tab in client.get(FEED_URL).context["tabs"]}

    assert tabs[OTHER] == 2
    assert tabs[channels.DEFAULT.slug] == 0


def test_the_channel_picker_costs_one_query(client: Client, make_resident: Callable[..., Resident]) -> None:
    """One aggregate for every channel, not one count per entry — this page polls itself.

    The reply-count annotate on the posts query is also a COUNT, and is excluded here by the table
    it necessarily joins: this test is about the PICKER's counts, and
    test_the_reply_count_costs_no_extra_query_per_message covers the other one."""
    author = make_resident(email="a@gahk.dk")
    for slug in ("generelt", OTHER):
        QuickPost.objects.create(author=author, content=f"Besked i {slug}", channel=slug)
    client.force_login(author)
    client.get(FEED_URL)  # warm

    with CaptureQueriesContext(connection) as captured:
        client.get(FEED_URL)

    counting = [
        q
        for q in captured.captured_queries
        if "COUNT" in q["sql"].upper() and "den_hurtige_quickcomment" not in q["sql"]
    ]
    assert len(counting) == 1, counting


def test_a_channel_can_be_restricted_to_a_role(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-channel gate stacks on top of den_hurtige.access. 404 rather than 403 on purpose: a
    403 would confirm the channel exists to someone who may not read it."""
    secret = Channel("internt", "Internt", "flash", "", 60, roles=(Role.INSPEKTION,))
    monkeypatch.setattr(channels, "CHANNELS", (*channels.CHANNELS, secret))
    monkeypatch.setattr(channels, "BY_SLUG", {**channels.BY_SLUG, "internt": secret})

    client.force_login(make_resident(email="beboer@gahk.dk"))
    assert client.get(FEED_URL + "internt/").status_code == 404
    assert client.get(FEED_URL + "opslag?kanal=internt").status_code == 204
    assert "Internt" not in client.get(FEED_URL).content.decode()

    client.force_login(make_resident(email="insp@gahk.dk", roles=(Role.INSPEKTION,)))
    assert client.get(FEED_URL + "internt/").status_code == 200


# --- channel mutes ------------------------------------------------------------------------------


def test_every_channel_notifies_until_it_is_muted(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    """Opt-out, not opt-in: a new channel that notifies nobody until people find it is a new channel
    nobody posts in."""
    author = make_resident(email="a@gahk.dk")
    other = make_resident(email="b@gahk.dk")
    subscribe(other, "https://push.example/b")
    client.force_login(author)

    client.post(FEED_URL + "opret", {"content": "Afgang 21", "kanal": OTHER})

    assert pushes[0][0] == [other.pk]


def test_a_mute_silences_only_the_channel_it_names(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    author = make_resident(email="a@gahk.dk")
    quiet = make_resident(email="b@gahk.dk")
    subscribe(quiet, "https://push.example/b")
    ChannelMute.objects.create(resident=quiet, channel=OTHER)
    client.force_login(author)

    client.post(FEED_URL + "opret", {"content": "Afgang 21", "kanal": OTHER})
    assert pushes[-1][0] == []

    client.post(FEED_URL + "opret", {"content": "Kaffe", "kanal": "generelt"})
    assert pushes[-1][0] == [quiet.pk]


def test_a_direct_reply_reaches_a_muted_poster(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    """A mute is about the channel's chatter, not about answers to your own message — you are not
    being broadcast at, you are being replied to."""
    poster = make_resident(email="a@gahk.dk")
    subscribe(poster, "https://push.example/a")
    ChannelMute.objects.create(resident=poster, channel=OTHER)
    post = QuickPost.objects.create(author=poster, content="Hvem er med?", channel=OTHER)
    client.force_login(make_resident(email="b@gahk.dk"))

    client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "Jeg er"})

    assert pushes[-1][0] == [poster.pk]


def test_notify_everyone_still_respects_a_mute(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    """Underret alle is a broadcast, and a mute is exactly a refusal of this channel's broadcasts."""
    poster = make_resident(email="a@gahk.dk")
    quiet = make_resident(email="b@gahk.dk")
    subscribe(quiet, "https://push.example/b")
    ChannelMute.objects.create(resident=quiet, channel=OTHER)
    post = QuickPost.objects.create(author=poster, content="Hvem er med?", channel=OTHER)
    client.force_login(poster)

    client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "Vi ses", "notify": "alle"})

    assert pushes[-1][0] == []


def test_the_mute_button_toggles_both_ways(client: Client, make_resident: Callable[..., Resident]) -> None:
    resident = make_resident(email="a@gahk.dk")
    client.force_login(resident)
    url = f"{FEED_URL}lyd/{OTHER}"

    client.post(url)
    assert ChannelMute.objects.filter(resident=resident, channel=OTHER).exists()

    client.post(url)
    assert not ChannelMute.objects.filter(resident=resident, channel=OTHER).exists()


def test_muting_a_channel_that_is_already_muted_does_not_double_up(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Two taps racing each other on a slow connection must not hit uniq_channel_mute and 500 on
    what is meant to be a toggle."""
    resident = make_resident(email="a@gahk.dk")
    ChannelMute.objects.create(resident=resident, channel=OTHER)

    ChannelMute.objects.get_or_create(resident=resident, channel=OTHER)

    assert ChannelMute.objects.filter(resident=resident, channel=OTHER).count() == 1


def test_muting_an_unknown_channel_is_404(client: Client, make_resident: Callable[..., Resident]) -> None:
    client.force_login(make_resident(email="a@gahk.dk"))

    assert client.post(FEED_URL + "lyd/findes-ikke").status_code == 404
    assert not ChannelMute.objects.exists()


def test_a_notification_deep_links_to_the_channel_the_message_is_in(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    """sw.js hands notificationclick whatever `url` the payload carries, so tapping must open the
    feed the message is actually in rather than the default one."""
    author = make_resident(email="a@gahk.dk")
    subscribe(make_resident(email="b@gahk.dk"), "https://push.example/b")
    client.force_login(author)

    client.post(FEED_URL + "opret", {"content": "Afgang 21", "kanal": OTHER})

    assert pushes[-1][1]["url"] == f"{FEED_URL}{OTHER}/"


def test_a_notification_carries_no_tag(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    """Deliberate, and worth pinning: a shared tag makes each notification replace the previous one,
    so a second post would silently overwrite the first. As true per channel as it was feed-wide."""
    author = make_resident(email="a@gahk.dk")
    subscribe(make_resident(email="b@gahk.dk"), "https://push.example/b")
    client.force_login(author)

    client.post(FEED_URL + "opret", {"content": "Kaffe"})

    assert "tag" not in pushes[-1][1]


# --- the channel registry -----------------------------------------------------------------------


def test_the_shipped_channel_registry_is_valid() -> None:
    assert check_channels(None) == []


@pytest.mark.parametrize(
    ("registry", "expected"),
    [
        pytest.param(
            (Channel("dup", "A", "flash", "", 60), Channel("dup", "B", "flash", "", 60)),
            "den_hurtige.E007",
            id="duplicate slug",
        ),
        pytest.param(
            (Channel("opret", "Opret", "flash", "", 60),),
            "den_hurtige.E008",
            id="slug shadowed by a fixed URL segment",
        ),
        pytest.param(
            (Channel("odd", "Odd", "flash", "", 7),),
            "den_hurtige.E009",
            id="duration the composer does not offer",
        ),
    ],
)
def test_a_broken_channel_registry_is_caught_at_startup(
    monkeypatch: pytest.MonkeyPatch, registry: tuple, expected: str
) -> None:
    """The registry is a tuple in code, so it has no unique constraint, no FK and no choices= behind
    it. These checks are what replaces them — each failure mode is otherwise entirely silent."""
    monkeypatch.setattr(channels, "CHANNELS", registry)

    assert expected in [error.id for error in check_channels(None)]


def test_the_default_channel_must_match_the_model_field_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every row written before the channel field existed carries the model default. If the two
    disagree, those posts sit in a channel no tab links to."""
    monkeypatch.setattr(channels, "DEFAULT", Channel("andet", "Andet", "flash", "", 60))

    assert "den_hurtige.E010" in [error.id for error in check_channels(None)]


def test_every_channel_is_reachable_from_every_other_one(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The header must always offer the full set. A horizontally scrolling tab strip failed this in
    practice rather than in code — at five channels it ran off the edge of a phone with nothing to
    say the rest were there — so the picker that replaced it is pinned here."""
    client.force_login(make_resident(email="a@gahk.dk"))

    for page in channels.CHANNELS:
        body = client.get(page.url).content.decode()
        for target in channels.CHANNELS:
            assert f'href="{target.url}"' in body, f"{target.slug} unreachable from {page.slug}"


def test_every_channel_url_resolves(client: Client, make_resident: Callable[..., Resident]) -> None:
    """A slug urls.py cannot route would put a dead link in the tab strip on every page load."""
    client.force_login(make_resident(email="a@gahk.dk"))

    for channel in channels.CHANNELS:
        assert client.get(channel.url).status_code == 200, channel.slug


# --- who reacted --------------------------------------------------------------------------------


def test_reaction_rows_name_the_people_behind_each_emoji(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The panel and the pills are rendered from one list, so `people` must group and order exactly
    as the counts do — most-used first, ties by first use."""
    author = make_resident(email="a@gahk.dk", first_name="Anton", last_name="Storgaard")
    mette = make_resident(email="b@gahk.dk", first_name="Mette", last_name="Hansen")
    anders = make_resident(email="c@gahk.dk", first_name="Anders", last_name="Bo")
    post = QuickPost.objects.create(author=author, content="Kaffe?")
    QuickReaction.objects.create(post=post, author=author, emoji=PARTY)
    QuickReaction.objects.create(post=post, author=mette, emoji=THUMB)
    QuickReaction.objects.create(post=post, author=anders, emoji=THUMB)

    rows = reactions_for(post, author.pk)

    # THUMB has two, so it sorts ahead of PARTY even though PARTY was used first.
    assert [r["emoji"] for r in rows] == [THUMB, PARTY]
    assert rows[0]["people"] == ["Mette Hansen", "Anders Bo"]  # in the order they reacted
    assert rows[1]["people"] == ["Anton Storgaard"]


def test_the_feed_shows_who_reacted_and_with_what(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    author = make_resident(email="a@gahk.dk", first_name="Anton", last_name="Storgaard")
    post = QuickPost.objects.create(author=author, content="Kaffe?")
    QuickReaction.objects.create(post=post, author=author, emoji=THUMB)
    client.force_login(author)

    body = client.get(FEED_URL).content.decode()

    assert "who-list" in body
    assert "Anton Storgaard" in body


def test_no_reader_panel_when_nobody_has_reacted(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """An empty panel behind a button that says "see who reacted" is worse than no button."""
    author = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=author, content="Kaffe?")
    client.force_login(author)

    body = client.get(FEED_URL).content.decode()

    assert "who-picker" not in body


def test_both_reaction_panels_are_overlays_with_a_backdrop(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The panels are fixed-position overlays over a backdrop, not absolutely positioned beside
    their summary: anchored to the "+" button — which sits after the pills — a message with several
    reactions opened the picker off the right edge of a phone. The backdrop must be a real element,
    because a click on a ::before pseudo-element targets its originating element and so could never
    be told apart from a click inside the picker (see frontend/src/feed.ts)."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Kaffe?")
    QuickReaction.objects.create(post=post, author=author, emoji=THUMB)
    client.force_login(author)

    body = client.get(FEED_URL).content.decode()

    assert body.count('class="pop-backdrop"') == 2  # one per panel
    assert body.count('class="pop-panel"') == 2
    assert 'class="pop who-picker"' in body
    assert 'class="pop emoji-picker"' in body


def test_the_reaction_row_survives_a_toggle_with_its_reader_panel(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The toggle re-renders only this row, so it has to carry the panel back with it — otherwise
    reacting would make the "who reacted" button vanish until the next poll."""
    author = make_resident(email="a@gahk.dk", first_name="Anton", last_name="Storgaard")
    post = QuickPost.objects.create(author=author, content="Kaffe?")
    client.force_login(author)

    body = react(client, post, THUMB).content.decode()  # type: ignore[attr-defined]

    assert "who-list" in body
    assert "Anton Storgaard" in body


def test_deleting_a_message_asks_first(client: Client, make_resident: Callable[..., Resident]) -> None:
    """The delete control is a bare x on every message in a scrolling feed, and purge_expired is a
    HARD delete with no history to restore from — so a mis-tap has to be recoverable at the point of
    the tap or not at all. The message text is quoted so a moderator can see which one they are
    about to remove."""
    author = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=author, content="Kaffe i koekkenet")
    client.force_login(author)

    body = client.get(FEED_URL).content.decode()

    assert "return confirm(" in body
    assert "Kaffe i koekkenet" in body


def test_the_confirmation_survives_the_poll_re_rendering_the_row(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """It is an inline onsubmit rather than a delegated listener precisely so the 20-second swap
    cannot leave a message whose delete no longer asks."""
    author = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=author, content="Kaffe")
    client.force_login(author)

    assert "return confirm(" in client.get(FEED_URL + "opslag").content.decode()


def test_the_reader_panel_lists_one_row_per_person(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Comma-joining a whole group onto one line stopped being readable as soon as a post got
    popular. A row each, so the list scrolls and can be scanned.

    There is now one panel PER EMOJI rather than one listing everybody, so the emoji no longer has
    to repeat beside every name — it is in the panel's own title. What still has to hold is that a
    person gets a row, that the rows land in the panel belonging to the emoji they used, and that
    the panels come in the same order as the pills above them."""
    author = make_resident(email="a@gahk.dk", first_name="Anton", last_name="Storgaard")
    mette = make_resident(email="b@gahk.dk", first_name="Mette", last_name="Hansen")
    anders = make_resident(email="c@gahk.dk", first_name="Anders", last_name="Bo")
    post = QuickPost.objects.create(author=author, content="Kaffe?")
    for person in (author, mette):
        QuickReaction.objects.create(post=post, author=person, emoji=THUMB)
    QuickReaction.objects.create(post=post, author=anders, emoji=PARTY)
    client.force_login(author)

    body = client.get(FEED_URL).content.decode()

    assert body.count('class="who-row"') == 3  # one per reaction, not one per emoji
    assert body.count('class="pop who-picker"') == 2  # one panel per emoji, not one for the post

    # Slice on the panel ids, not the bare keys -- the pills reference the same keys in data-who.
    thumb_panel = body[body.index(f'id="who-{post.pk}-1"') : body.index(f'id="who-{post.pk}-2"')]
    assert "Anton Storgaard" in thumb_panel
    assert "Mette Hansen" in thumb_panel
    assert "Anders Bo" not in thumb_panel  # he used the other emoji

    # The pills' order carries into the panels: THUMB has two reactions, so its panel comes first.
    assert body.index("Mette Hansen") < body.index("Anders Bo")


def test_holding_a_reaction_pill_is_what_opens_the_reader_panel(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The 👥 pill that used to sit after the reactions is gone, so the pills themselves have to
    carry the way in.

    Each pill points at its own panel by id, which is what frontend/src/feed.ts binds the hold,
    hover, right-click and Shift+Enter gestures to."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Kaffe?")
    QuickReaction.objects.create(post=post, author=author, emoji=THUMB)
    client.force_login(author)

    body = client.get(FEED_URL).content.decode()

    assert f'data-who="who-{post.pk}-1"' in body
    assert f'id="who-{post.pk}-1"' in body
    assert 'aria-haspopup="dialog"' in body
    # No title attribute: a browser only shows one on hover, and hovering already shows the names,
    # so it drew a native tooltip explaining the gesture on top of the tooltip that had just
    # answered the question. See the comment on the pill in _reactions.html.
    assert "title=" not in body.split('class="reaction')[1].split(">")[0]
    # Tapping a pill must still be the toggle, never the panel.
    assert "den_hurtige:toggle_reaction" not in body  # url tag rendered, not left literal
    assert f'hx-post="{FEED_URL}{post.pk}/reaktion"' in body


def test_the_add_reaction_button_spells_itself_out_on_an_untouched_message(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """On a message with no reactions the picker is the only thing in the row, and a bare glyph read
    as an empty slot rather than the control that fills it. The label is CSS-gated on .is-empty so a
    busy message pays nothing for it."""
    author = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=author, content="Kaffe?")
    client.force_login(author)

    body = client.get(FEED_URL).content.decode()

    assert 'class="reactions is-empty"' in body
    assert "add-label" in body


# --- thread panel --------------------------------------------------------------------------------


def test_the_feed_no_longer_loads_replies(client: Client, make_resident: Callable[..., Resident]) -> None:
    """The feed prefetched comments__author for every post on every poll — loading every reply of
    every message, five seconds apart, in order to render the number "3".

    This is the guard that stops it creeping back: it asserts the poll touches the comment table
    ZERO times, and that no reply text reaches the feed at all."""
    author = make_resident(email="a@gahk.dk")
    for n in range(3):
        post = QuickPost.objects.create(author=author, content=f"Besked {n}")
        QuickComment.objects.create(post=post, author=author, content=f"Hemmeligt svar {n}")
    client.force_login(author)
    client.get(FEED_URL + "opslag")  # warm

    with CaptureQueriesContext(connection) as captured:
        body = client.get(FEED_URL + "opslag").content.decode()

    # FROM, not a bare table-name match: the reply-count annotate LEFT JOINs the comment table into
    # the posts query, which is the whole point. What must not exist is a query that SELECTS the
    # replies themselves.
    loaded = [q for q in captured.captured_queries if 'FROM "den_hurtige_quickcomment"' in q["sql"]]
    assert loaded == [], "the feed is loading replies again"
    assert "Hemmeligt svar 0" not in body
    assert "1 svar" in body  # the count, not the replies


def test_the_reply_count_costs_no_extra_query_per_message(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The count is an annotate on the posts query, so N messages stay one query. A per-post
    `post.comments.count()` in the template would be the N+1 this replaced."""
    author = make_resident(email="a@gahk.dk")
    for n in range(4):
        post = QuickPost.objects.create(author=author, content=f"Besked {n}")
        QuickComment.objects.create(post=post, author=author, content="Svar")
    client.force_login(author)
    client.get(FEED_URL + "opslag")  # warm

    with CaptureQueriesContext(connection) as captured:
        client.get(FEED_URL + "opslag")

    counted = [
        q
        for q in captured.captured_queries
        if "den_hurtige_quickpost" in q["sql"] and "COUNT" in q["sql"].upper()
    ]
    assert len(counted) == 1, counted


def test_the_feed_shows_a_reply_count_and_no_reply_form(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Replies live in the panel now. The feed carries a link and nothing else — the reply form
    used to be rendered inline on every single message, open or not."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Boremaskine?")
    for text in ("Ja", "Kom forbi"):
        QuickComment.objects.create(post=post, author=author, content=text)
    client.force_login(author)

    body = client.get(FEED_URL).content.decode()

    assert "2 svar" in body
    assert f'href="{FEED_URL}{post.pk}/traad"' in body
    assert "reply-form" not in body
    assert "Kom forbi" not in body


def test_a_message_with_replies_is_marked_differently_from_one_without(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The two states of the "Svar" link must not render as the same object with different words.

    They did. Both were `class="msg-replies"` and nothing else, so a thread on the feed was
    distinguishable only by reading the row — which is not how anyone scans a chat. `.has-replies`
    now carries a pill (styles.css) and the speech-bubble glyph, and the empty state carries
    neither, so the difference survives in the markup where a test can see it.

    Asserted on ONE feed containing both messages rather than on two requests: what matters is
    that they differ from each other in the same view, which is the thing a reader compares.
    """
    author = make_resident(email="a@gahk.dk")
    threaded = QuickPost.objects.create(author=author, content="Boremaskine?")
    QuickComment.objects.create(post=threaded, author=author, content="Ja")
    QuickPost.objects.create(author=author, content="Ingen svar her")
    client.force_login(author)

    body = client.get(FEED_URL).content.decode()

    assert 'class="msg-replies has-replies"' in body
    assert 'class="msg-replies"' in body  # the other message, unmarked

    # The glyph rides in the marked state ONLY, and each anchor is sliced out to ask that of it.
    # Counting "#i-chat" across a span of the page cannot answer it: every message also renders a
    # .msg-hint that references the same sprite symbol for the swipe gesture.
    anchors = dict(re.findall(r'<a class="msg-replies( has-replies)?"(.*?)</a>', body, re.DOTALL))
    assert set(anchors) == {" has-replies", ""}, "expected one marked and one unmarked message"
    assert "#i-chat" in anchors[" has-replies"]
    assert "#i-chat" not in anchors[""]
    # And the count still reads as one unbroken string, which is what the icon must not split.
    assert "1 svar" in anchors[" has-replies"]


def test_the_thread_panel_lives_outside_the_polled_region(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """#js-thread is a sibling of #js-feed, never inside it.

    Inside, the 5s morph would rebuild the panel under the reader and destroy it outright when the
    parent message expired — and #js-feed is overflow-y:auto, so a full-screen phone panel would be
    clipped by it. This asserts the polled fragment cannot contain the panel."""
    author = make_resident(email="a@gahk.dk")
    QuickPost.objects.create(author=author, content="Kaffe")
    client.force_login(author)

    polled = client.get(FEED_URL + "opslag").content.decode()
    page = client.get(FEED_URL).content.decode()

    # The CONTAINER, not the string: every "N svar" link in the feed legitimately carries
    # hx-target="#js-thread", which is how it reaches a container outside its own swap.
    assert 'id="js-thread"' not in polled, "the panel is inside the region that gets morphed"
    assert 'id="js-thread"' in page
    assert page.index('id="js-feed"') < page.index('id="js-thread"')


def test_the_thread_panel_polls_itself(client: Client, make_resident: Callable[..., Resident]) -> None:
    """Being outside #js-feed means it gets no refresh from the feed's poll, so the fragment brings
    its own — on its ROOT, morphed by outerHTML, so the trigger survives its own swap."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Kaffe")
    client.force_login(author)

    fragment = client.get(f"{FEED_URL}{post.pk}/traad", HTTP_HX_REQUEST="true").content.decode()

    assert f'hx-get="{FEED_URL}{post.pk}/traad"' in fragment
    assert 'hx-trigger="every 5s"' in fragment
    assert 'hx-swap="morph:outerHTML"' in fragment


def test_the_reply_composer_is_shielded_from_the_morph(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """idiomorph's ignoreActiveValue only spares the field being typed INTO. Type half a reply, tap
    a reaction, and the panel's own poll would overwrite it with the server's empty value.
    data-morph-skip is what feed.ts keys off to leave the whole form alone."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Kaffe")
    client.force_login(author)

    fragment = client.get(f"{FEED_URL}{post.pk}/traad", HTTP_HX_REQUEST="true").content.decode()

    form = fragment.split('class="reply-form"')[1].split(">")[0]
    assert "data-morph-skip" in form


def test_a_reply_through_htmx_returns_only_the_reply_list(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The list, not the whole panel — the form carries data-morph-skip, so a response that
    replaced the panel would skip the form and leave the sent text sitting in the box."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Boremaskine?")
    client.force_login(author)

    response = client.post(
        f"{FEED_URL}{post.pk}/kommentar", {"content": "Ja, kom forbi"}, HTTP_HX_REQUEST="true"
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "Ja, kom forbi" in body
    assert "reply-form" not in body, "the form is inside the swap target, so it can never reset"


def test_a_reply_error_reaches_the_panel_instead_of_the_session(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Without JS these surfaced on the redirect. A reply posted through htmx never reloads the
    page, so an empty-comment warning would sit unread in the session until something else
    navigated."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Boremaskine?")
    client.force_login(author)

    response = client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "  "}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    # Wording tracks what is actually required now: text OR a photo, not text alone.
    assert "Skriv et svar, eller vedhæft et billede" in response.content.decode()


def test_a_reply_can_be_a_photo_with_no_text(
    client: Client,
    make_resident: Callable[..., Resident],
    settings: object,
    tmp_path: Path,
) -> None:
    """ "Her, se" is a whole answer in a house chat, and requiring a caption for it only produced
    replies reading "billede" and ".".

    The ordering inside views.create_comment is what this pins: the upload has to be resolved BEFORE
    the emptiness check, or a blank `content` is rejected while the photo is still sitting unread in
    request.FILES."""
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    author = make_resident(email="a@gahk.dk")
    helper = make_resident(email="b@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Hvilken slags pære?")
    client.force_login(helper)
    image = SimpleUploadedFile("paere.jpg", bytes.fromhex("ffd8ff") + b"x" * 512, content_type="image/jpeg")

    response = client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "", "image": image})

    assert response.status_code in (200, 302)
    comment = QuickComment.objects.get()
    assert comment.content == ""
    assert comment.image.name.startswith("quick_comments/")


def test_a_reply_with_neither_text_nor_photo_is_still_refused(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Dropping `required` from the input moved this check to the server; it did not remove it.
    An empty press must still produce nothing but a message."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Boremaskine?")
    client.force_login(author)

    response = client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "   "}, HTTP_HX_REQUEST="true")

    assert QuickComment.objects.count() == 0
    assert "Skriv et svar, eller vedhæft et billede" in response.content.decode()


def test_a_rejected_photo_with_no_text_reports_both_what_and_why(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """_validated_image returns None for "nothing attached" and for "attached but refused" alike, so
    a bad photo with no caption falls through to the empty-reply branch. That is the right landing
    place — there is genuinely nothing to save — but on its own it would explain only half of it, so
    the upload warning has to arrive beside it or the photo silently "did not count"."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Hvilken slags pære?")
    client.force_login(author)
    # An SVG: refused by core.uploads because it executes script when opened from our own /media/.
    bad = SimpleUploadedFile(
        "evil.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", content_type="image/svg+xml"
    )

    response = client.post(
        f"{FEED_URL}{post.pk}/kommentar", {"content": "", "image": bad}, HTTP_HX_REQUEST="true"
    )
    body = response.content.decode()

    assert QuickComment.objects.count() == 0
    assert "Billedet blev ikke gemt" in body  # why the photo did not count
    assert "Skriv et svar, eller vedhæft et billede" in body  # and why the reply did not land


def test_a_photo_only_reply_notifies_with_a_body_rather_than_a_blank(
    client: Client,
    make_resident: Callable[..., Resident],
    pushes: list,
    tmp_path: Path,
    settings: object,
) -> None:
    """push.preview("") is "", and a notification with an empty body reads on a lock screen as
    though it failed to load."""
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    author = make_resident(email="a@gahk.dk")
    helper = make_resident(email="b@gahk.dk")
    subscribe(author, "https://push.example/author")
    post = QuickPost.objects.create(author=author, content="Hvilken slags pære?")
    client.force_login(helper)
    image = SimpleUploadedFile("paere.jpg", bytes.fromhex("ffd8ff") + b"x" * 512, content_type="image/jpeg")

    client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "", "image": image})

    (recipients, payload) = pushes[0]
    assert recipients == [author.pk]
    assert payload["body"].strip(), "a blank body reads as a failed notification"
    assert "Billede" in payload["body"]
    assert helper.full_name in payload["head"]


def test_an_expired_post_gives_a_notice_to_the_panel_and_404_to_the_page(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Expiring while somebody has the thread open is the interesting case: the fragment must stop
    polling, or it asks for a deleted message every five seconds forever. The page 404s, because a
    deep link to a message that no longer exists leads nowhere."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(
        author=author, content="Kaffe", expires_at=timezone.now() - timedelta(minutes=1)
    )
    client.force_login(author)

    fragment = client.get(f"{FEED_URL}{post.pk}/traad", HTTP_HX_REQUEST="true")
    assert fragment.status_code == 200
    body = fragment.content.decode()
    assert "udløbet" in body
    assert "hx-trigger" not in body, "the dead panel would keep polling for a deleted message"

    assert client.get(f"{FEED_URL}{post.pk}/traad").status_code == 404


def test_a_thread_in_a_restricted_channel_is_404(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-post endpoints resolve a post by pk alone, which says nothing about whether the
    caller may read the CHANNEL it lives in. That mattered less while they were all writes; a
    thread is a READ, so guessing a pk would otherwise hand over a restricted channel's
    contents."""
    secret = Channel("internt", "Internt", "flash", "", 60, roles=(Role.INSPEKTION,))
    monkeypatch.setattr(channels, "CHANNELS", (*channels.CHANNELS, secret))
    monkeypatch.setattr(channels, "BY_SLUG", {c.slug: c for c in channels.CHANNELS})
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Fortroligt", channel="internt")
    client.force_login(make_resident(email="b@gahk.dk"))

    assert client.get(f"{FEED_URL}{post.pk}/traad").status_code == 404
    # The writes go through the same helper, so they answer the same way.
    assert client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "hej"}).status_code == 404
    assert client.post(f"{FEED_URL}{post.pk}/reaktion", {"emoji": THUMB}).status_code == 404


def test_the_thread_page_stands_alone_without_htmx(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The "N svar" anchor has a real href, so the thread works with no JavaScript at all — that is
    what keeps the no-JS path the old <details> had."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Boremaskine?")
    QuickComment.objects.create(post=post, author=author, content="Ja, kom forbi")
    client.force_login(author)

    body = client.get(f"{FEED_URL}{post.pk}/traad").content.decode()

    assert "<html" in body  # a whole page, not a fragment
    assert "Boremaskine?" in body
    assert "Ja, kom forbi" in body
    assert "reply-form" in body


def test_reactions_still_work_from_inside_a_thread(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The panel is deliberately an in-flow box with no z-index and no resting transform, because a
    stacking context would trap every `.pop` inside it. The pickers being present here is the half
    of that contract a server test can hold."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Kaffe")
    QuickReaction.objects.create(post=post, author=author, emoji=THUMB)
    client.force_login(author)

    fragment = client.get(f"{FEED_URL}{post.pk}/traad", HTTP_HX_REQUEST="true").content.decode()

    assert f'hx-post="{FEED_URL}{post.pk}/reaktion"' in fragment
    assert "pop-backdrop" in fragment
    assert "emoji-picker" in fragment


def test_a_reply_notification_deep_links_to_the_thread(
    client: Client, make_resident: Callable[..., Resident], pushes: list
) -> None:
    """Tapping "Anders svarede" used to land at the bottom of the channel, leaving you to find the
    message it was about — which, in a feed where everything expires, may already be gone."""
    author = make_resident(email="a@gahk.dk")
    commenter = make_resident(email="b@gahk.dk")
    subscribe(author, "https://push.example/author")
    post = QuickPost.objects.create(author=author, content="Boremaskine?")

    client.force_login(commenter)
    client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "Ja"})

    (_recipients, payload) = pushes[0]
    assert payload["url"] == f"{FEED_URL}?traad={post.pk}"


def test_the_channel_page_pre_opens_a_requested_thread(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """What makes that deep link work: ?traad= wires the empty panel container to fetch on load."""
    author = make_resident(email="a@gahk.dk")
    post = QuickPost.objects.create(author=author, content="Kaffe")
    client.force_login(author)

    body = client.get(f"{FEED_URL}?traad={post.pk}").content.decode()

    assert f'hx-get="{FEED_URL}{post.pk}/traad"' in body
    assert 'hx-trigger="load"' in body
    # Junk must not blow the page up — the panel's own request does the validating.
    assert client.get(f"{FEED_URL}?traad=nonsense").status_code == 200


def test_the_feed_morphs_its_poll_instead_of_replacing_the_list(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The poll patches the existing DOM rather than swapping it out.

    A plain innerHTML swap threw away scroll position, collapsed open reply threads and deleted
    half-typed replies, which is why ~40 lines of frontend/src/feed.ts existed to defend against the
    page's own refresh. Morphing leaves untouched nodes untouched, so that defence is gone -- and if
    this attribute ever reverts to a plain swap, the guards will not come back with it.
    """
    client.force_login(make_resident(email="a@gahk.dk"))

    body = client.get(FEED_URL).content.decode()

    assert 'hx-ext="morph"' in body
    assert 'hx-swap="morph:innerHTML"' in body


def test_the_feed_polls_every_five_seconds(client: Client, make_resident: Callable[..., Resident]) -> None:
    """20s read as broken in a chat. The interval only became safe to shorten once the swap stopped
    being destructive -- the two changes belong together, so this pins the pair."""
    client.force_login(make_resident(email="a@gahk.dk"))

    body = client.get(FEED_URL).content.decode()

    assert 'hx-trigger="every 5s"' in body


def test_the_poll_still_returns_the_whole_list(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Deliberately NOT an incremental "only what is new" endpoint. Traffic was never the problem at
    this scale, and sending everything is what keeps deletions, expiry, reaction counts and new
    replies correct without a second reconciliation path. Morphing makes the full response cheap to
    apply, so the two decisions depend on each other."""
    author = make_resident(email="a@gahk.dk")
    for n in range(3):
        QuickPost.objects.create(author=author, content=f"Besked {n}")
    client.force_login(author)

    body = client.get(FEED_URL + "opslag").content.decode()

    for n in range(3):
        assert f"Besked {n}" in body
