"""intern ↔ gahk.dk: request signing, publishing, the 2-minute sync (revisions, applications, visits),
the nightly rebuild and the update nudge — against a fake gahk.dk that checks every signature."""

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from io import BytesIO
from typing import Any

import pytest
import requests
from django.core import mail
from django.core.files.base import ContentFile
from django.test import Client
from django.utils import timezone
from PIL import Image

from admissions.models import Application
from cms import tasks
from cms.gahk_web import (
    GahkWebClient,
    GahkWebDisabled,
    GahkWebRejected,
    GahkWebUnavailable,
    sign,
    signed_headers,
)
from cms.models import CmsImage, GahkWebSync, Page, Publication
from residents.models import Resident
from stats.models import DailyVisitCount, PageViewDay

KEY = "test-sync-key"
BASE = "https://gahk.example"


class FakeGahkWeb:
    """gahk.dk's /api/* as the contract describes it, in memory. Refuses a bad signature with 401."""

    def __init__(self) -> None:
        self.images: dict[str, bytes] = {}
        self.revision: str | None = None
        self.version = "abc123"
        self.published: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.outbox: dict[str, list[Any]] = {"applications": [], "visits": []}
        self.acks: list[dict[str, Any]] = []
        self.forget_after_missing = False  # simulate images vanishing between the two calls
        self.down = False

    # requests.Session.request's signature, as GahkWebClient calls it
    def request(
        self, method: str, url: str, data: bytes = b"", headers: dict[str, str] | None = None, **_: object
    ) -> requests.Response:
        if self.down:
            raise requests.ConnectionError("connection refused")
        headers = headers or {}
        path = url.removeprefix(BASE)
        self.calls.append((method, path))
        if path != "/api/health":
            expected = sign(KEY, headers.get("X-Gahk-Timestamp", ""), method, path, data or b"")
            stamp = int(headers.get("X-Gahk-Timestamp", "0"))
            if (
                not hmac.compare_digest(expected, headers.get("X-Gahk-Signature", ""))
                or abs(time.time() - stamp) > 300
            ):
                return self._json(401, {"error": "bad signature"})
        body = json.loads(data) if data and method == "POST" and path != "/api/publish" else None
        if method == "GET" and path == "/api/status":
            return self._json(
                200,
                {
                    "version": self.version,
                    "revision": self.revision,
                    "published_at": None,
                    "rendered_at": None,
                    "outbox": {"applications": 0, "visit_days": 0},
                    "update": {
                        "checked_at": None,
                        "available_version": "def456",
                        "last_result": None,
                        "last_error": None,
                    },
                },
            )
        if method == "POST" and path == "/api/images/missing":
            assert body is not None
            return self._json(200, {"missing": [sha for sha in body["sha256"] if sha not in self.images]})
        if method == "PUT" and path.startswith("/api/images/"):
            sha = path.rsplit("/", 1)[1]
            if hashlib.sha256(data).hexdigest() != sha or headers.get("Content-Type") not in (
                "image/jpeg",
                "image/png",
            ):
                return self._json(400, {"error": "bad image"})
            self.images[sha] = data
            return self._response(204, b"")
        if method == "POST" and path == "/api/publish":
            snapshot = json.loads(data)
            if self.forget_after_missing:
                self.forget_after_missing = False
                self.images.clear()
            missing = [image["sha256"] for image in snapshot["images"] if image["sha256"] not in self.images]
            if missing:
                return self._json(409, {"error": "missing images", "missing": missing})
            self.published.append(snapshot)
            self.revision = snapshot["revision"]
            return self._json(200, {"revision": self.revision, "pages_written": len(snapshot["pages"]) + 3})
        if method == "GET" and path == "/api/outbox":
            return self._json(200, self.outbox)
        if method == "POST" and path == "/api/outbox/ack":
            self.acks.append(body or {})
            return self._json(200, {"deleted_applications": 0, "deleted_visit_days": 0})
        if method == "POST" and path == "/api/preview":
            assert body is not None and set(body) == {"kind", "item", "media_base"}
            html = f"<h1>{body['item']['title']}</h1>{body['item'].get('body', '')}"
            return self._response(200, html.encode(), "text/html; charset=utf-8")
        if method == "POST" and path == "/api/rebuild":
            if self.revision is None:
                return self._json(409, {"error": "no snapshot has been published yet"})
            return self._json(200, {"pages_written": 7})
        if method == "POST" and path == "/api/update":
            return self._json(
                200,
                {"updated": True, "version": "def456", "available_version": "def456", "detail": "installed"},
            )
        return self._json(404, {"error": "no such endpoint"})

    @staticmethod
    def _response(status: int, content: bytes, content_type: str = "application/json") -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response._content = content
        response.headers["Content-Type"] = content_type
        return response

    def _json(self, status: int, payload: dict[str, Any]) -> requests.Response:
        return self._response(status, json.dumps(payload).encode())


@pytest.fixture
def gahk_web(settings: Any, monkeypatch: pytest.MonkeyPatch) -> FakeGahkWeb:  # noqa: ANN401
    fake = FakeGahkWeb()
    settings.GAHK_WEB_URL = BASE
    settings.GAHK_WEB_KEY = KEY
    original_init = GahkWebClient.__init__

    def init(
        self: GahkWebClient, base_url: str | None = None, key: str | None = None, session: object = None
    ) -> None:
        original_init(self, base_url, key, session or fake)  # type: ignore[arg-type]

    monkeypatch.setattr(GahkWebClient, "__init__", init)
    return fake


@pytest.fixture
def front_page() -> Page:
    return Page.objects.create(pk=1, slug="velkommen", header="Velkommen", body="<p>Forside</p>")


def _library_image() -> CmsImage:
    buffer = BytesIO()
    Image.new("RGB", (20, 20), "blue").save(buffer, format="JPEG")
    image = CmsImage(caption="Blå")
    image.file.save("blaa.jpg", ContentFile(buffer.getvalue()), save=True)
    return image


# --------------------------------------------------------------------------------------- signing
def test_the_signature_matches_the_contract() -> None:
    body = b'{"a":1}'
    reference = hmac.new(
        b"k", f"1700000000\nPOST\n/api/publish\n{hashlib.sha256(body).hexdigest()}".encode(), hashlib.sha256
    ).hexdigest()
    assert sign("k", "1700000000", "post", "/api/publish", body) == reference
    assert signed_headers("k", "POST", "/api/publish", body, now=1700000000.9) == {
        "X-Gahk-Timestamp": "1700000000",
        "X-Gahk-Signature": reference,
    }
    empty = hmac.new(b"k", f"5\nGET\n/api/status\n{hashlib.sha256(b'').hexdigest()}".encode(), hashlib.sha256)
    assert sign("k", "5", "GET", "/api/status", b"") == empty.hexdigest()


def test_the_signed_path_has_no_query_string_and_keeps_a_base_path() -> None:
    seen: dict[str, Any] = {}

    class Session:
        def request(
            self, method: str, url: str, data: bytes = b"", headers: dict[str, str] | None = None, **_: object
        ) -> requests.Response:
            seen.update(url=url, headers=headers)
            return FakeGahkWeb._response(200, b"{}")

    GahkWebClient("http://127.0.0.1:8810/", "k", Session()).status()  # type: ignore[arg-type]
    headers = seen["headers"]
    assert seen["url"] == "http://127.0.0.1:8810/api/status"
    assert headers["X-Gahk-Signature"] == sign("k", headers["X-Gahk-Timestamp"], "GET", "/api/status", b"")


def test_errors_are_typed() -> None:
    class Session:
        def __init__(self, status: int) -> None:
            self.status = status

        def request(self, *args: Any, **kwargs: Any) -> requests.Response:  # noqa: ANN401
            return FakeGahkWeb._response(self.status, b'{"error": "nope"}')

    with pytest.raises(GahkWebDisabled):
        GahkWebClient(BASE, "").status()
    with pytest.raises(GahkWebUnavailable):
        GahkWebClient(BASE, "k", Session(502)).status()  # type: ignore[arg-type]
    with pytest.raises(GahkWebRejected) as rejected:
        GahkWebClient(BASE, "k", Session(400)).status()  # type: ignore[arg-type]
    assert rejected.value.status == 400 and "nope" in str(rejected.value)


# --------------------------------------------------------------------------------------- publish
@pytest.mark.django_db
def test_without_a_key_every_task_is_a_quiet_no_op(settings: Any) -> None:  # noqa: ANN401
    settings.GAHK_WEB_KEY = ""
    assert tasks.publish_gahk_web(reason="save") == "disabled"
    assert tasks.sync_gahk_web() == {"enabled": False}
    assert tasks.rebuild_gahk_web() == "disabled"
    assert tasks.nudge_gahk_web_update() == {"enabled": False}
    assert not Publication.objects.exists()


@pytest.mark.django_db
def test_publishing_uploads_the_missing_images_first(gahk_web: FakeGahkWeb, front_page: Page) -> None:
    image = _library_image()
    front_page.body = f'<img src="{image.url}">'
    front_page.save()

    assert tasks.publish_gahk_web(reason="save") == "ok"

    image.refresh_from_db()
    assert list(gahk_web.images) == [image.sha256]
    [snapshot] = gahk_web.published
    assert snapshot["images"][0]["sha256"] == image.sha256
    publication = Publication.objects.get()
    assert (
        publication.status,
        publication.revision,
        publication.images_uploaded,
        publication.pages_written,
    ) == (
        "ok",
        snapshot["revision"],
        1,
        4,
    )
    assert GahkWebSync.load().remote_revision == snapshot["revision"]
    assert ("POST", "/api/images/missing") in gahk_web.calls


@pytest.mark.django_db
def test_a_409_uploads_what_gahk_dk_names_and_publishes_again_once(
    gahk_web: FakeGahkWeb, front_page: Page
) -> None:
    image = _library_image()
    front_page.background_image = image.url
    front_page.save()
    gahk_web.forget_after_missing = True

    tasks.publish_gahk_web(reason="save")

    assert [call for call in gahk_web.calls if call == ("POST", "/api/publish")] == [
        ("POST", "/api/publish")
    ] * 2
    assert len([call for call in gahk_web.calls if call[0] == "PUT"]) == 2
    assert Publication.objects.get().status == "ok"


@pytest.mark.django_db
def test_an_unchanged_revision_is_skipped_unless_asked_by_hand(
    gahk_web: FakeGahkWeb, front_page: Page
) -> None:
    tasks.publish_gahk_web(reason="save")
    tasks.publish_gahk_web(reason="save")
    tasks.publish_gahk_web(reason="manual")

    assert list(Publication.objects.order_by("id").values_list("reason", "status")) == [
        ("save", "ok"),
        ("save", "skipped"),
        ("manual", "ok"),
    ]
    assert len(gahk_web.published) == 2


@pytest.mark.django_db
def test_an_unreachable_gahk_dk_is_recorded_not_raised(gahk_web: FakeGahkWeb, front_page: Page) -> None:
    gahk_web.down = True

    assert tasks.publish_gahk_web(reason="save") == "failed"

    publication = Publication.objects.get()
    assert publication.status == "failed" and "ConnectionError" in publication.error
    assert "ConnectionError" in GahkWebSync.load().last_error


@pytest.mark.django_db
def test_an_unexpected_error_still_closes_the_publication(
    gahk_web: FakeGahkWeb, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = _library_image()
    front_page.background_image = image.url
    front_page.save()

    def broken_read(self: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError("bucket says no")

    monkeypatch.setattr(tasks, "_upload_missing", broken_read)

    with pytest.raises(RuntimeError):
        tasks.publish_gahk_web(reason="save")

    publication = Publication.objects.get()
    assert publication.status == "failed" and "bucket says no" in publication.error
    assert publication.finished_at is not None


@pytest.mark.django_db
def test_a_content_save_publishes_after_the_commit(
    gahk_web: FakeGahkWeb,
    front_page: Page,
    make_resident: Callable[..., Resident],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    from cms.services import save_page

    editor = make_resident(email="e@gahk.dk")
    with django_capture_on_commit_callbacks(execute=True):
        save_page(None, {"header": "Kontakt", "slug": "kontakt"}, editor)

    assert [page["path"] for page in gahk_web.published[-1]["pages"]] == ["", "kontakt"]
    assert Publication.objects.get().triggered_by == editor


# ------------------------------------------------------------------------------------------ sync
def _application(kind: str = "rundvisning", **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    base = {
        "full_name": "Ada Lovelace",
        "email": "ada@example.com",
        "gender": "female",
        "age": "21",
        "heard_about_us": "plakat",
        "motivation": "Jeg vil gerne bo her.",
    }
    if kind == "rundvisning":
        base |= {"study_year": "2", "year_left": "3", "university": "DTU", "field_of_study": "Fysik"}
    else:
        base |= {"occupation": "Studerende"}
    return {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "submitted_at": "2026-11-10T09:30:00+01:00",
        "fields": base | fields,
    }


@pytest.mark.django_db
def test_a_revision_mismatch_republishes(gahk_web: FakeGahkWeb, front_page: Page) -> None:
    gahk_web.revision = "0" * 64

    report = tasks.sync_gahk_web()

    assert report["in_step"] is False and report["errors"] == []
    assert Publication.objects.get().reason == "reconcile"
    assert gahk_web.revision == gahk_web.published[0]["revision"]
    state = GahkWebSync.load()
    assert state.remote_version == "abc123" and state.update_available_version == "def456"
    assert state.status_checked_at is not None and state.outbox_pulled_at is not None

    assert tasks.sync_gahk_web()["in_step"] is True
    assert Publication.objects.count() == 1


@pytest.mark.django_db
def test_a_publish_that_breaks_still_lets_the_outbox_be_pulled(
    gahk_web: FakeGahkWeb, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    gahk_web.revision = "0" * 64  # gahk.dk is on another revision, so sync publishes
    item = _application()
    gahk_web.outbox["applications"] = [item]

    def broken_publish(*args: object, **kwargs: object) -> None:
        raise RuntimeError("bucket says no")

    monkeypatch.setattr(tasks, "publish", broken_publish)

    report = tasks.sync_gahk_web()

    assert report["errors"] == ["bucket says no"]
    assert str(Application.objects.get().external_id) == item["id"]
    assert gahk_web.acks[0]["applications"] == [item["id"]]
    assert "bucket says no" in GahkWebSync.load().last_error


@pytest.mark.django_db
def test_applications_are_stored_once_and_acknowledged(
    gahk_web: FakeGahkWeb, front_page: Page, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    tour, sublet = _application(), _application("fremleje", motivation="x" * 2000)
    gahk_web.outbox["applications"] = [tour, sublet]
    mail.outbox = []

    with django_capture_on_commit_callbacks(execute=True):
        tasks.sync_gahk_web()
    with django_capture_on_commit_callbacks(execute=True):
        tasks.sync_gahk_web()  # the ack "got lost": the same items again

    assert Application.objects.count() == 2
    stored = Application.objects.get(external_id=tour["id"])
    assert (stored.type, stored.full_name, stored.university) == ("rundvisning", "Ada Lovelace", "DTU")
    assert stored.submitted_at.isoformat() == "2026-11-10T08:30:00+00:00"
    assert [ack["applications"] for ack in gahk_web.acks] == [[tour["id"], sublet["id"]]] * 2
    assert len(mail.outbox) == 2  # one auto-reply per applicant, not per pull
    assert mail.outbox[0].to == ["ada@example.com"]


@pytest.mark.django_db
def test_malformed_applications_are_dropped_or_kept_as_the_contract_says(
    gahk_web: FakeGahkWeb, front_page: Page
) -> None:
    too_long = _application(motivation="a" * 501)  # rundvisning caps motivation at 500
    bad_email = _application(email="ikke en adresse")
    bad_choice = _application(heard_about_us="<script>")
    not_text = _application(full_name=["Ada"])
    no_fields = {"id": str(uuid.uuid4()), "kind": "fremleje", "submitted_at": "2026-11-10T09:30:00+01:00"}
    naive_time = _application() | {"submitted_at": "2026-11-10 09:30"}
    future_kind = _application() | {"kind": "venteliste"}
    no_id = _application() | {"id": "42"}
    gahk_web.outbox["applications"] = [
        too_long,
        bad_email,
        bad_choice,
        not_text,
        no_fields,
        naive_time,
        future_kind,
        no_id,
        "x",
    ]

    tasks.sync_gahk_web()

    assert not Application.objects.exists()
    dropped = [too_long, bad_email, bad_choice, not_text, no_fields, naive_time]
    assert gahk_web.acks[0]["applications"] == [item["id"] for item in dropped]  # never the kept or id-less
    assert GahkWebSync.load().rejected_applications == len(dropped)


@pytest.mark.django_db
def test_a_500_character_motivation_is_accepted(gahk_web: FakeGahkWeb, front_page: Page) -> None:
    gahk_web.outbox["applications"] = [_application(motivation=("a " * 249) + "ab")]
    tasks.sync_gahk_web()
    assert len(Application.objects.get().motivation) == 500


@pytest.mark.django_db
def test_a_long_fremleje_motivation_that_gahk_dk_accepted_is_stored(
    gahk_web: FakeGahkWeb, front_page: Page
) -> None:
    """gahk.dk caps a form post at 64 KiB and the fremleje motivation at nothing else, so intern
    must take any field up to that size: dropping it would also delete gahk.dk's only copy."""
    gahk_web.outbox["applications"] = [_application("fremleje", motivation="a" * 60_000)]
    tasks.sync_gahk_web()
    assert len(Application.objects.get().motivation) == 60_000


@pytest.mark.django_db
def test_a_submission_time_in_the_future_is_stored_as_now(gahk_web: FakeGahkWeb, front_page: Page) -> None:
    item = _application() | {"submitted_at": (timezone.now() + timedelta(days=30)).isoformat()}
    gahk_web.outbox["applications"] = [item]
    tasks.sync_gahk_web()
    assert Application.objects.get().submitted_at <= timezone.now()


@pytest.mark.django_db
def test_visits_are_upserted_and_complete_days_acknowledged(gahk_web: FakeGahkWeb, front_page: Page) -> None:
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    gahk_web.outbox["visits"] = [
        {"day": yesterday.isoformat(), "path": "*", "views": 40, "visitors": 12},
        {"day": yesterday.isoformat(), "path": "faciliteter", "views": 9, "visitors": 4},
        {"day": today.isoformat(), "path": "*", "views": 3, "visitors": 2},
        {"day": "not a day", "path": "*", "views": 1, "visitors": 1},
        {"day": today.isoformat(), "path": "x", "views": -1, "visitors": 1},
    ]
    tasks.sync_gahk_web()
    # Later the same day: today's totals have grown, and gahk.dk sends them again.
    gahk_web.outbox["visits"] = [{"day": today.isoformat(), "path": "*", "views": 8, "visitors": 5}]
    tasks.sync_gahk_web()

    assert DailyVisitCount.objects.get(date=yesterday).count == 12
    assert DailyVisitCount.objects.get(date=today).count == 5  # overwritten, not added
    assert PageViewDay.objects.get(date=yesterday, path="faciliteter").views == 9
    assert PageViewDay.objects.count() == 3
    assert gahk_web.acks[0] == {"applications": [], "visits_through": yesterday.isoformat()}
    assert len(gahk_web.acks) == 1  # nothing complete to acknowledge the second time


@pytest.mark.django_db
def test_an_unreachable_gahk_dk_is_stored_for_the_overview(gahk_web: FakeGahkWeb, front_page: Page) -> None:
    gahk_web.down = True
    report = tasks.sync_gahk_web()
    assert len(report["errors"]) == 2
    assert GahkWebSync.load().last_error_at is not None


# --------------------------------------------------------------------------------------- preview
@pytest.mark.django_db
def test_a_preview_is_cleaned_like_a_save_and_stored_nowhere(gahk_web: FakeGahkWeb) -> None:
    from cms.services import render_preview

    html = render_preview(
        "page", {"header": "Kladde", "body": "<p>Hej</p><script>x()</script>", "slug": "kladde"}
    )

    assert html == "<h1>Kladde</h1><p>Hej</p>"
    assert not Page.objects.exists() and not gahk_web.published
    assert "<h1>Fest</h1>" in render_preview("news", {"title": "Fest", "body": "<p>x</p>"})
    assert "<h1>Havedag</h1>" in render_preview("event", {"title": "Havedag", "starts_on": "nonsense"})


# --------------------------------------------------------------------------- nightly and update
@pytest.mark.django_db
def test_the_nightly_rebuild_publishes_when_gahk_dk_has_nothing(
    gahk_web: FakeGahkWeb, front_page: Page
) -> None:
    assert tasks.rebuild_gahk_web() == "ok"  # 409 "no snapshot yet" → a full publish
    assert gahk_web.published
    assert tasks.rebuild_gahk_web() == "ok"
    assert list(Publication.objects.order_by("id").values_list("reason", "status", "pages_written")) == [
        ("nightly", "ok", 4),
        ("nightly", "ok", 7),
    ]


@pytest.mark.django_db
def test_the_update_nudge_stores_what_the_webhost_says(gahk_web: FakeGahkWeb) -> None:
    tasks.nudge_gahk_web_update()

    state = GahkWebSync.load()
    assert (state.remote_version, state.update_available_version) == ("def456", "def456")
    assert state.update_result.startswith("Opdateret til def456") and state.update_error == ""
    assert state.update_checked_at is not None

    gahk_web.down = True
    tasks.nudge_gahk_web_update()
    assert "ConnectionError" in GahkWebSync.load().update_error


# ------------------------------------------------------------------------- the old public routes
@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "/optagelse/ansoeg",
        "/optagelse/fremlej",
        "/optagelse/send_rundvisning",
        "/optagelse/send_fremleje",
        "/optagelse/ansoeg/success",
        "/faciliteter",
        "/velkommen",
    ],
)
def test_the_public_site_is_not_served_here(path: str) -> None:
    assert Client().get(path).status_code == 404


@pytest.mark.django_db
def test_begivenheder_is_the_intranet_events_app_and_needs_a_login() -> None:
    response = Client().get("/begivenheder/")
    assert response.status_code == 302 and "login" in response.headers["Location"]
