"""The transition between deploying the new intern and moving gahk.dk to its webhost (transition/).
TEMPORARY, like the package: phase 3 reverts both.

  * gahk.dk, while this app still answers it: the old public site from today's content, and
    everything else as gahk.dk will answer after the DNS switch.
  * Logins carried over from the old code's ".gahk.dk" session cookie, every new session marked, and
    `end_legacy_sessions` ending the unmarked ones.
"""

import datetime
from collections.abc import Callable
from importlib import import_module
from io import BytesIO, StringIO
from urllib.parse import quote

import pytest
from django.conf import settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.sessions.models import Session
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from admissions.models import Application
from cms.models import CmsImage, Event, NewsItem, Page, PageRedirect
from events import access as events_access
from events import services as events_services
from events.models import Answer, CalendarFeedToken
from events.models import Event as ResidentEvent
from residents.models import Resident
from transition.middleware import LEGACY_SESSION_COOKIE, MARKER, CarryOverLegacySession

pytestmark = pytest.mark.django_db

PUBLIC = "gahk.dk"


@pytest.fixture(autouse=True)
def _public_hosts(settings: object) -> None:
    settings.TRANSITION_PUBLIC_HOSTS = ["gahk.dk", "www.gahk.dk"]  # type: ignore[attr-defined]
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "gahk.dk", "www.gahk.dk", "intern.gahk.dk"]  # type: ignore[attr-defined]
    settings.SITE_URL = "https://intern.gahk.dk"  # type: ignore[attr-defined]
    settings.WIKI_URL = "https://wiki.gahk.dk/"  # type: ignore[attr-defined]


def public(host: str = PUBLIC, **kwargs: object) -> Client:
    return Client(HTTP_HOST=host, **kwargs)  # type: ignore[arg-type]


def _jpeg() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (40, 30), "red").save(buffer, format="JPEG")
    return buffer.getvalue()


def _image(name: str = "gang.jpg", **fields: object) -> CmsImage:
    image = CmsImage(caption="Gangen", **fields)
    image.file.save(name, ContentFile(_jpeg()), save=True)
    return image


def _media_url(image: CmsImage) -> str:
    return f"/media/{quote(image.file.name or '', safe='/')}"


# --- the old public site --------------------------------------------------------------------------


def test_the_front_page_is_the_old_site_built_from_todays_content() -> None:
    picture = _image()
    Page.put_front_page(slug="velkommen", header="Velkommen", body=f'<p>Hej <img src="{picture.url}"></p>')
    NewsItem.objects.create(title="Fest <b>nu</b>", body="<p>x</p>", published_at=timezone.now())
    Event.objects.create(title="Rundvisning", starts_on=timezone.localdate() + datetime.timedelta(days=2))

    response = public().get("/")

    html = response.content.decode()
    assert response.status_code == 200
    assert "Velkommen til GAHK" in html and f'<img src="{picture.url}">' in html
    # Titles are plain text now, escaped wherever they are shown.
    assert "Fest &lt;b&gt;nu&lt;/b&gt;" in html and "Rundvisning" in html
    assert 'href="https://intern.gahk.dk/"' in html  # "Intern ›"
    assert "dist/app.js" not in html


def test_intern_gahk_dk_keeps_the_intranet_at_the_root() -> None:
    response = Client(HTTP_HOST="intern.gahk.dk").get("/")
    assert response.status_code == 302 and response["Location"].startswith(reverse("login"))


def test_without_public_hosts_gahk_dk_is_the_intranet(settings: object) -> None:
    settings.TRANSITION_PUBLIC_HOSTS = []  # type: ignore[attr-defined]
    response = public().get("/")
    assert response.status_code == 302 and response["Location"].startswith(reverse("login"))


def test_www_gets_the_same_site() -> None:
    Page.put_front_page(slug="velkommen", header="Velkommen", body="<p>Hej</p>")
    assert "Velkommen til GAHK" in public("www.gahk.dk").get("/").content.decode()


def test_a_page_with_its_section_and_a_library_background() -> None:
    background = _image("baggrund.jpg")
    Page.objects.create(slug="faciliteter", header="Faciliteter", body="<p>Alt</p>")
    Page.objects.create(
        slug="faciliteter/kokken",
        header="Køkken & <mad>",
        body='<p><img src="public/image/upload/images/a.jpg"></p>',
        background_image=background.url,
    )

    response = public().get("/faciliteter/kokken")

    html = response.content.decode()
    assert response.status_code == 200
    assert "Køkken &amp; &lt;mad&gt;" in html
    assert f"background-image:url('{background.url}')" in html
    # A relative old picture address broke below the root; it is the absolute old address now.
    assert '<img src="/static/legacy/image/upload/images/a.jpg">' in html
    assert 'href="/faciliteter"' in html  # the section's sidebar


def test_a_former_address_and_a_missing_slash_redirect() -> None:
    page = Page.objects.create(slug="kontakt", header="Kontakt", body="")
    PageRedirect.objects.create(old_path="kontakt-os", page=page)

    assert public().get("/kontakt-os?a=1")["Location"] == "/kontakt?a=1"
    response = public().get("/optagelse")
    assert response.status_code == 301 and response["Location"] == "/optagelse/"
    assert public().get("/findes-ikke").status_code == 404


def test_news_and_events() -> None:
    NewsItem.objects.create(title="Nyhed", body="<p>Tekst</p>", published_at=timezone.now())
    Event.objects.create(
        title="Fest", description="<p>Kom</p>", starts_on=timezone.localdate() + datetime.timedelta(days=1)
    )
    html = public().get("/begivenheder/").content.decode()
    assert "Nyhed" in html and "<p>Tekst</p>" in html and "<p>Kom</p>" in html


def test_a_library_image_is_public_on_gahk_dk_only() -> None:
    picture = _image()

    response = public().get(_media_url(picture))
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == picture.file.open("rb").read()  # type: ignore[attr-defined]
    # On intern.gahk.dk it still needs a login (or goes to gahk.dk's copy), as without the transition.
    assert Client(HTTP_HOST="intern.gahk.dk").get(_media_url(picture)).status_code in (301, 302)


def test_other_media_and_a_dotted_path_go_to_intern() -> None:
    """As gahk.dk forwards it: the path as it came; intern.gahk.dk checks the login itself."""
    response = public().get("/media/cms/../profile_pictures/x.jpg")
    assert response.status_code == 308
    assert response["Location"] == "https://intern.gahk.dk/media/cms/../profile_pictures/x.jpg"
    assert public().get("/media/opslag/a.jpg")["Location"] == "https://intern.gahk.dk/media/opslag/a.jpg"


@pytest.mark.parametrize("prefix", ["/static/legacy/image/", "/public/image/"])
def test_an_old_picture_address_goes_to_its_library_copy(prefix: str) -> None:
    picture = _image(legacy_path="upload/images/gang.jpg")

    response = public().get(f"{prefix}upload/images/gang.jpg")
    assert response.status_code == 302 and response["Location"] == picture.url
    assert public().get(f"{prefix}upload/images/findes-ikke.jpg").status_code == 404
    assert public().get(f"{prefix}upload/../x.jpg").status_code == 404


# --- the admission forms --------------------------------------------------------------------------

TOUR = {
    "full_name": "Ansøger Andersen",
    "email": "a@example.com",
    "gender": "female",
    "age": "22",
    "study_year": "2",
    "year_left": "3",
    "university": "DTU",
    "field_of_study": "Kemi",
    "heard_about_us": "hjemmeside",
    "motivation": "Gerne",
}


def test_a_tour_request_is_stored_straight_into_intern(
    mailoutbox: list, django_capture_on_commit_callbacks: Callable[..., object]
) -> None:
    c = public()
    assert c.get("/optagelse/ansoeg").status_code == 200
    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[attr-defined]
        response = c.post("/optagelse/send_rundvisning", TOUR)

    assert response.status_code == 302 and response["Location"] == "/optagelse/ansoeg/success"
    application = Application.objects.get()
    assert application.type == Application.Type.TOUR and application.external_id is None
    assert [m.to for m in mailoutbox] == [["a@example.com"]]
    assert settings.SESSION_COOKIE_NAME not in response.cookies  # no intern session on gahk.dk
    assert public().get("/optagelse/ansoeg/success").status_code == 200


def test_a_sublet_request_and_the_honeypot() -> None:
    sublet = {k: TOUR[k] for k in ("full_name", "email", "gender", "age", "heard_about_us", "motivation")}
    public().post("/optagelse/send_fremleje", {**sublet, "occupation": "Studerende"})
    assert Application.objects.get().type == Application.Type.SUBLET

    response = public().post("/optagelse/send_fremleje", {**sublet, "occupation": "x", "website": "spam"})
    assert response.status_code == 200 and Application.objects.count() == 1


def test_the_landing_page_and_a_stale_form() -> None:
    assert "Anmod om rundvisning" in public().get("/optagelse/").content.decode()
    response = public(enforce_csrf_checks=True).post("/optagelse/send_rundvisning", TOUR)
    assert response.status_code == 403 and "Siden var udløbet" in response.content.decode()
    assert not Application.objects.exists()


# --- everything else, as gahk.dk will answer it --------------------------------------------------


@pytest.mark.parametrize("path", ["/intern", "/intern/", "/intern/den-hurtige/", "/nyintern/ak/"])
def test_reading_an_old_intranet_address_shows_how_to_move(path: str) -> None:
    response = public().get(path)
    html = response.content.decode()
    assert response.status_code == 200
    assert "Den Hurtige er flyttet" in html
    assert 'var target = "https://intern.gahk.dk/" + rest' in html
    assert public().head(path).status_code == 200


def test_the_old_apps_button_says_it_comes_from_the_old_app() -> None:
    """/install would otherwise take the old app for an installed GAHK Intern; on an iPhone the
    button tries Safari first (x-safari-https://)."""
    html = public().get("/intern/").content.decode()
    assert 'id="install-link" href="https://intern.gahk.dk/install?from=old-app"' in html
    assert 'location.href = "x-safari-" + button.href.split("?")[0];' in html


@pytest.mark.parametrize(
    ("path", "target"),
    [
        (
            "/intern/fotoalbum/importer-zip/system?x=1",
            "https://intern.gahk.dk/fotoalbum/importer-zip/system?x=1",
        ),
        ("/intern", "https://intern.gahk.dk/"),
        ("/nyintern/ak/", "https://intern.gahk.dk/ak/"),
        ("/intern//evil.example/", "https://intern.gahk.dk/evil.example/"),
        ("/intern/v%C3%A6relse/a%3Fb", "https://intern.gahk.dk/v%C3%A6relse/a%3Fb"),
    ],
)
def test_any_other_method_to_an_old_intranet_address_is_a_308(path: str, target: str) -> None:
    response = public().post(path)
    assert response.status_code == 308 and response["Location"] == target


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("/admin", "/admin"),
        ("/admin/login/?next=/ak/", "/admin/login/?next=/ak/"),
        ("/django-admin/", "/django-admin/"),
        ("/photo-album/a/b.jpg", "/photo-album/a/b.jpg"),
        ("/health", "/health"),
        ("/healthz/", "/healthz"),
        ("/optagelse/listansoegninger/", "/optagelse/listansoegninger"),
        ("/optagelse/eksport", "/optagelse/eksport"),
        ("/optagelse/showAnsoegning/4", "/optagelse/showAnsoegning/4"),
        ("/optagelse/kasser/4", "/optagelse/kasser/4"),
    ],
)
def test_the_intranets_tools_go_to_intern(path: str, target: str) -> None:
    for method in ("get", "post"):
        response = getattr(public(), method)(path)
        assert response.status_code == 308, (method, path)
        assert response["Location"] == "https://intern.gahk.dk" + target


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("/wiki", "https://wiki.gahk.dk/"),
        ("/wiki/", "https://wiki.gahk.dk/"),
        ("/wiki/index.php?title=Forside", "https://wiki.gahk.dk/index.php?title=Forside"),
        ("/wiki/images/a/ab/X.png", "https://wiki.gahk.dk/images/a/ab/X.png"),
        ("/wiki/Køkkenet", "https://wiki.gahk.dk/wiki/K%C3%B8kkenet"),
        ("/wiki/Hjælp:Indhold", "https://wiki.gahk.dk/wiki/Hj%C3%A6lp:Indhold"),
    ],
)
def test_the_wiki_goes_to_wiki_gahk_dk(path: str, target: str) -> None:
    response = public().get(path)
    assert response.status_code == 308 and response["Location"] == target


def test_a_calendar_feed_with_a_gahk_dk_address_still_answers(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(events_access, "ACCESS_ROLES", None)
    resident = make_resident(email="beboer@gahk.dk")
    event = ResidentEvent.objects.create(
        organiser=resident,
        title="Fællesspisning",
        starts_at=(timezone.now() + datetime.timedelta(days=3)).replace(second=0, microsecond=0),
    )
    events_services.set_answer(event.pk, resident, Answer.JA)

    response = public().get(f"/kalender/{CalendarFeedToken.for_resident(resident).token}.ics")
    body = response.content.decode()
    assert response.status_code == 200 and "Fællesspisning" in body
    assert f"URL:https://intern.gahk.dk/begivenheder/{event.pk}" in body


def test_the_service_worker_is_still_served() -> None:
    response = public().get("/sw.js")
    assert response.status_code == 200 and response["Content-Type"] == "application/javascript"


def test_the_old_cookies_are_left_alone_on_gahk_dk(make_resident: Callable[..., Resident]) -> None:
    """Deleting the old session cookie here would log the visitor out before intern.gahk.dk could
    carry the session over; and nothing on gahk.dk may log anyone in."""
    old = _old_session(make_resident)
    c = public()
    c.cookies[LEGACY_SESSION_COOKIE] = old
    c.cookies["csrftoken"] = "x"
    c.cookies["gahk_editor"] = "1"

    response = c.get("/intern/den-hurtige/")

    assert response.status_code == 200 and not response.cookies
    assert Session.objects.filter(session_key=old).exists()


# --- logins carried over --------------------------------------------------------------------------


def _old_session(make_resident: Callable[..., Resident], **resident: object) -> str:
    """A logged-in session as the old code saved it (no mark), and its key."""
    old = Client()
    old.force_login(make_resident(email=resident.pop("email", "beboer@gahk.dk"), **resident))
    key = old.cookies[settings.SESSION_COOKIE_NAME].value
    assert MARKER not in Session.objects.get(session_key=key).get_decoded()
    return key


def _intern_with_old_cookie(key: str) -> Client:
    c = Client(HTTP_HOST="intern.gahk.dk")
    c.cookies[LEGACY_SESSION_COOKIE] = key
    return c


def test_an_old_login_is_carried_over_to_a_new_key(make_resident: Callable[..., Resident]) -> None:
    old = _old_session(make_resident)
    c = _intern_with_old_cookie(old)

    response = c.get(reverse("dashboard"))

    assert response.status_code == 200
    new = response.cookies[settings.SESSION_COOKIE_NAME]
    assert new.value and new.value != old
    assert not Session.objects.filter(session_key=old).exists()
    data = Session.objects.get(session_key=new.value).get_decoded()
    assert data[MARKER] == 1 and data["_auth_user_id"]
    # The old cookie is deleted on the same response, from both of its domains.
    deletions = {m["domain"] for m in response.cookies.values() if m.key == "sessionid" and m["max-age"] == 0}
    assert deletions == {".gahk.dk", ""}
    # The next request uses the new cookie and stays logged in.
    del c.cookies[LEGACY_SESSION_COOKIE]
    assert c.get(reverse("dashboard")).status_code == 200


def test_the_new_cookie_is_set_as_for_any_login(make_resident: Callable[..., Resident]) -> None:
    make_resident(email="anden@gahk.dk", password="hemmelig")
    login = Client()
    login.get(reverse("login"))
    logged_in = login.post(reverse("login"), {"username": "anden@gahk.dk", "password": "hemmelig"})
    expected = logged_in.cookies[settings.SESSION_COOKIE_NAME]

    carried = _intern_with_old_cookie(_old_session(make_resident)).get(reverse("dashboard"))
    cookie = carried.cookies[settings.SESSION_COOKIE_NAME]

    for attribute in ("domain", "path", "secure", "httponly", "samesite", "max-age"):
        assert cookie[attribute] == expected[attribute], attribute
    assert cookie["max-age"] == settings.SESSION_COOKIE_AGE and cookie["path"] == "/"
    assert cookie["httponly"] and not cookie["domain"]


def test_an_old_key_is_accepted_once(make_resident: Callable[..., Resident]) -> None:
    old = _old_session(make_resident)
    assert _intern_with_old_cookie(old).get(reverse("dashboard")).status_code == 200

    replayed = _intern_with_old_cookie(old).get(reverse("dashboard"))
    assert replayed.status_code == 302 and replayed["Location"].startswith(reverse("login"))
    assert settings.SESSION_COOKIE_NAME not in replayed.cookies


def test_an_expired_or_unknown_old_key_is_anonymous(make_resident: Callable[..., Resident]) -> None:
    old = _old_session(make_resident)
    Session.objects.filter(session_key=old).update(expire_date=timezone.now() - datetime.timedelta(seconds=1))

    for key in (old, "x" * 32):
        response = _intern_with_old_cookie(key).get(reverse("dashboard"))
        assert response.status_code == 302 and response["Location"].startswith(reverse("login"))
        assert settings.SESSION_COOKIE_NAME not in response.cookies
    assert not Session.objects.filter(session_key=old).exists()


def test_a_new_session_is_never_accepted_under_the_old_name(make_resident: Callable[..., Resident]) -> None:
    """Once gahk.dk is the webhost it receives ".gahk.dk" cookies and could set one: a browser handed
    somebody's NEW session key as `sessionid` must not be logged in with it, nor that session ended."""
    make_resident(email="anden@gahk.dk", password="hemmelig")
    login = Client()
    login.post(reverse("login"), {"username": "anden@gahk.dk", "password": "hemmelig"})
    new = login.cookies[settings.SESSION_COOKIE_NAME].value

    response = _intern_with_old_cookie(new).get(reverse("dashboard"))

    assert response.status_code == 302 and response["Location"].startswith(reverse("login"))
    assert Session.objects.filter(session_key=new).exists()


def test_with_our_own_cookie_the_old_one_is_not_looked_at(make_resident: Callable[..., Resident]) -> None:
    old = _old_session(make_resident)
    c = _intern_with_old_cookie(old)
    c.cookies[settings.SESSION_COOKIE_NAME] = "y" * 32
    response = c.get(reverse("dashboard"))
    assert response.status_code == 302 and response["Location"].startswith(reverse("login"))
    assert Session.objects.filter(session_key=old).exists()


# --- the mark -------------------------------------------------------------------------------------


def test_a_login_is_marked(make_resident: Callable[..., Resident]) -> None:
    make_resident(email="beboer@gahk.dk", password="hemmelig")
    c = Client()
    c.post(reverse("login"), {"username": "beboer@gahk.dk", "password": "hemmelig"})
    key = c.cookies[settings.SESSION_COOKIE_NAME].value
    assert Session.objects.get(session_key=key).get_decoded()[MARKER] == 1


def _through_the_middleware(
    view: Callable[[HttpRequest], HttpResponse], cookies: dict | None = None
) -> object:
    request = RequestFactory().get("/")
    request.COOKIES.update(cookies or {})
    return SessionMiddleware(CarryOverLegacySession(view))(request)  # type: ignore[arg-type]


def test_an_anonymous_session_is_marked() -> None:
    def view(request: HttpRequest) -> HttpResponse:
        request.session["noget"] = "x"
        return HttpResponse()

    response = _through_the_middleware(view)
    key = response.cookies[settings.SESSION_COOKIE_NAME].value  # type: ignore[attr-defined]
    assert Session.objects.get(session_key=key).get_decoded() == {"noget": "x", MARKER: 1}


def test_a_session_that_is_only_read_is_not_written() -> None:
    def view(request: HttpRequest) -> HttpResponse:
        request.session.get("noget")
        return HttpResponse()

    response = _through_the_middleware(view)
    assert not response.cookies and not Session.objects.exists()  # type: ignore[attr-defined]


def test_a_logout_still_empties_the_session(make_resident: Callable[..., Resident]) -> None:
    c = Client()
    c.force_login(make_resident(email="beboer@gahk.dk"))
    key = c.cookies[settings.SESSION_COOKIE_NAME].value

    response = c.post(reverse("logout"))

    assert response.cookies[settings.SESSION_COOKIE_NAME]["max-age"] == 0
    assert not Session.objects.filter(session_key=key).exists()
    assert not Session.objects.exists()


# --- end_legacy_sessions --------------------------------------------------------------------------


def _sessions(make_resident: Callable[..., Resident]) -> tuple[list[str], str]:
    """Two old sessions and one that does not decode (all legacy), and one new marked one."""
    legacy = [_old_session(make_resident), _old_session(make_resident, email="to@gahk.dk")]
    store = import_module(settings.SESSION_ENGINE).SessionStore
    broken = store()
    broken.create()
    Session.objects.filter(session_key=broken.session_key).update(session_data="not-a-session")
    legacy.append(broken.session_key)
    new = store()
    new[MARKER] = 1
    new.create()
    return legacy, new.session_key


def test_end_legacy_sessions_ends_exactly_the_unmarked_ones(make_resident: Callable[..., Resident]) -> None:
    _legacy, new = _sessions(make_resident)

    out = StringIO()
    call_command("end_legacy_sessions", stdout=out)

    assert "Ended 3 old session(s); kept 1." in out.getvalue()
    assert list(Session.objects.values_list("session_key", flat=True)) == [new]
    again = StringIO()
    call_command("end_legacy_sessions", stdout=again)
    assert "Ended 0 old session(s); kept 1." in again.getvalue()


def test_end_legacy_sessions_dry_run_deletes_nothing(make_resident: Callable[..., Resident]) -> None:
    legacy, new = _sessions(make_resident)

    out = StringIO()
    call_command("end_legacy_sessions", "--dry-run", stdout=out)

    assert "would end 3 old session(s) and keep 1" in out.getvalue()
    assert set(Session.objects.values_list("session_key", flat=True)) == {*legacy, new}
