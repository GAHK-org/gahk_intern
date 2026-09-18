"""CMS page addresses, redirects, version history and the overview.

All of this exists because of one incident: an editor opened /faciliteter/kokken, changed the
address to `faciliteter-kokken`, saved — and the page disappeared from the site with no way back,
because "/" was rejected as an illegal character. Several tests below are that incident, replayed.
"""

from collections.abc import Callable

import pytest
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.test import Client

from cms.models import Page, PageRedirect, PageVersion
from cms.nav import is_reachable
from cms.paths import validate_page_path
from residents.models import Resident, Role

pytestmark = pytest.mark.django_db


@pytest.fixture
def editor(make_resident: Callable[..., Resident]) -> Resident:
    """An inspektion member — the role that actually hit this, and not an administrator."""
    return make_resident(email="inspektion@gahk.dk", roles=[Role.INSPEKTION])


@pytest.fixture
def editor_client(editor: Resident) -> Client:
    client = Client()
    client.force_login(editor)
    return client


def _faciliteter() -> Page:
    """The Faciliteter section as it exists in production (it is in NAV_PUBLIC)."""
    return Page.objects.create(header="Faciliteter", slug="faciliteter", body="<p>Faciliteter</p>")


def _post_page(client: Client, page: Page, *, follow: bool = False, **overrides: str) -> HttpResponse:
    """POST the admin change form for `page`, defaulting every field to its current value."""
    parent, _, segment = (page.slug or "").rpartition("/")
    data = {
        "path_parent": parent,
        "path_segment": segment,
        "slug": page.slug or "",
        "header": page.header,
        "body": page.body,
        "background_image": "",
    }
    data.update(overrides)
    return client.post(f"/django-admin/cms/page/{page.id}/change/", data, follow=follow)


# ------------------------------------------------------------------ the address itself


def test_a_multi_segment_address_round_trips_through_the_admin(editor_client: Client) -> None:
    """The reported bug, directly: re-saving /faciliteter/kokken must not mangle its address.

    Before this work the form rejected the slash outright, so an editor could save a broken address
    but never type the correct one back.
    """
    _faciliteter()
    page = Page.objects.create(header="Køkkenet", slug="faciliteter/kokken", body="<p>Hej</p>")

    response = _post_page(editor_client, page)

    assert response.status_code == 302, "the form should have accepted the address unchanged"
    page.refresh_from_db()
    assert page.slug == "faciliteter/kokken"


def test_an_editor_can_create_a_sub_page(editor_client: Client) -> None:
    """Picking a section is how a sub-page is made — previously impossible through the admin."""
    _faciliteter()

    response = editor_client.post(
        "/django-admin/cms/page/add/",
        {
            "path_parent": "faciliteter",
            "path_segment": "kokken",
            "slug": "",
            "header": "Køkkenet",
            "body": "<p>Fælleskøkkenet.</p>",
            "background_image": "",
        },
    )

    assert response.status_code == 302
    assert Page.objects.get(header="Køkkenet").slug == "faciliteter/kokken"


@pytest.mark.parametrize(
    "bad",
    [
        "/faciliteter",
        "faciliteter/",
        "faciliteter//kokken",
        "Faciliteter/Kokken",
        "faciliteter kokken",
        "../etc",
        "køkken",
        "a/b/c",
    ],
)
def test_the_validator_rejects_a_malformed_address(bad: str) -> None:
    with pytest.raises(ValidationError):
        validate_page_path(bad)


def test_a_malformed_address_is_reported_on_the_form_not_saved(editor_client: Client) -> None:
    """An error must come back as a field error on a *visible* field, leaving the page untouched.

    The address input is composed from two visible controls while `slug` itself is hidden; admin
    renders hidden-field errors in a detached list with nothing to point at, so errors are attached
    to the segment field on purpose.
    """
    page = Page.objects.create(header="Vision", slug="vision", body="")

    response = _post_page(editor_client, page, path_segment="a/b/c")

    assert response.status_code == 200  # re-rendered with errors, not redirected
    assert "path_segment" in response.context["adminform"].form.errors
    page.refresh_from_db()
    assert page.slug == "vision", "a rejected address must not have been written"


def test_an_address_cannot_shadow_a_real_app_url(editor_client: Client) -> None:
    """A fixed URL pattern wins over the CMS catch-all, so such a page would never open at all."""
    page = Page.objects.create(header="Vision", slug="vision", body="")

    for reserved in ("intern", "optagelse", "django-admin"):
        response = _post_page(editor_client, page, path_segment=reserved)
        assert response.status_code == 200, f"{reserved} should have been refused"
        page.refresh_from_db()
        assert page.slug == "vision"

    assert Client().get("/intern/").status_code in {200, 302}, "the real app must still answer"


def test_an_address_already_in_use_is_refused_by_name(editor_client: Client) -> None:
    """A duplicate must be a readable field error, never a 500 from the unique index."""
    Page.objects.create(header="Vision", slug="vision", body="")
    other = Page.objects.create(header="Legater", slug="legater", body="")

    response = _post_page(editor_client, other, path_segment="vision")

    assert response.status_code == 200
    assert "Vision" in str(response.context["adminform"].form.errors)
    other.refresh_from_db()
    assert other.slug == "legater"


def test_pages_without_an_address_do_not_collide(editor_client: Client) -> None:
    """`unique` permits many NULLs but only one "", so a blank address must store None."""
    first = Page.objects.create(header="Optagelse-tekst", slug=None, body="")
    second = Page.objects.create(header="Fremleje-tekst", slug=None, body="")

    assert _post_page(editor_client, first, path_segment="").status_code == 302
    assert _post_page(editor_client, second, path_segment="").status_code == 302

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.slug is None and second.slug is None


# ------------------------------------------------------------------ redirects


def test_renaming_a_page_keeps_the_old_address_alive(editor_client: Client) -> None:
    """The incident, end to end — except that now the old URL leads somewhere."""
    _faciliteter()
    page = Page.objects.create(header="Køkkenet", slug="faciliteter/kokken", body="<p>Hej</p>")

    assert _post_page(editor_client, page, path_segment="koekken").status_code == 302

    public = Client()
    moved = public.get("/faciliteter/kokken")
    assert moved.status_code == 301
    assert moved.headers["Location"] == "/faciliteter/koekken"
    assert public.get("/faciliteter/koekken").status_code == 200


def test_a_redirect_keeps_the_query_string(editor_client: Client) -> None:
    _faciliteter()
    page = Page.objects.create(header="Køkkenet", slug="faciliteter/kokken", body="")

    _post_page(editor_client, page, path_segment="koekken")

    response = Client().get("/faciliteter/kokken?fra=nyhedsbrev")
    assert response.headers["Location"] == "/faciliteter/koekken?fra=nyhedsbrev"


def test_renaming_twice_redirects_in_one_hop(editor_client: Client) -> None:
    """Redirects point at the page, not at a path, so there is no chain to follow."""
    page = Page.objects.create(header="Side", slug="side-a", body="")

    _post_page(editor_client, page, path_segment="side-b")
    page.refresh_from_db()
    _post_page(editor_client, page, path_segment="side-c")

    public = Client()
    for old in ("/side-a", "/side-b"):
        response = public.get(old)
        assert response.status_code == 301
        assert response.headers["Location"] == "/side-c", f"{old} should reach the page directly"


def test_renaming_back_leaves_no_self_redirect(editor_client: Client) -> None:
    """Renaming a → b → a must not leave `a` redirecting to itself."""
    page = Page.objects.create(header="Side", slug="side-a", body="")

    _post_page(editor_client, page, path_segment="side-b")
    page.refresh_from_db()
    _post_page(editor_client, page, path_segment="side-a")

    public = Client()
    assert public.get("/side-a").status_code == 200
    assert public.get("/side-b").status_code == 301


def test_a_live_page_wins_over_an_old_redirect(editor_client: Client) -> None:
    """A new page may reclaim an abandoned address; real content always beats a redirect."""
    page = Page.objects.create(header="Gammel", slug="side-a", body="<p>gammel</p>")
    _post_page(editor_client, page, path_segment="side-b")
    assert PageRedirect.objects.filter(old_path="side-a").exists()

    Page.objects.create(header="Ny", slug="side-a", body="<p>ny</p>")

    response = Client().get("/side-a")
    assert response.status_code == 200
    assert "ny" in response.content.decode()


def test_redirects_cannot_swallow_a_slashless_app_url(editor_client: Client) -> None:
    """Guards the lookup order in cms.views.page.

    The redirect lookup sits above the APPEND_SLASH restoration, and must never intercept a path
    owned by a real app. It cannot: PageRedirect.old_path is validated by cms.paths, whose reserved
    list rejects exactly these names — and cms.checks fails the build if urls.py grows another.
    """
    page = Page.objects.create(header="Side", slug="side-a", body="")
    _post_page(editor_client, page, path_segment="side-b")  # a PageRedirect row now exists

    public = Client()
    for path in ("/optagelse", "/nyintern", "/begivenheder"):
        response = public.get(path)
        assert response.status_code == 301, f"{path} should still redirect, got {response.status_code}"
        assert response.headers["Location"] == f"{path}/"


# ------------------------------------------------------------------ version history


def test_editing_a_page_keeps_the_previous_body(editor_client: Client, editor: Resident) -> None:
    """The gap that made the incident unrecoverable: what the page said before is now stored."""
    page = Page.objects.create(header="Vision", slug="vision", body="<p>Oprindelig tekst</p>")

    _post_page(editor_client, page, body="<p>Ny tekst</p>")

    bodies = list(PageVersion.objects.filter(page=page).values_list("body", flat=True))
    assert "<p>Oprindelig tekst</p>" in bodies, "the pre-edit content must be recoverable"
    assert "<p>Ny tekst</p>" in bodies
    assert PageVersion.objects.filter(page=page, created_by=editor).exists()


def test_restoring_a_version_brings_the_body_back_without_losing_history(
    editor_client: Client,
) -> None:
    page = Page.objects.create(header="Vision", slug="vision", body="<p>Den gode tekst</p>")
    _post_page(editor_client, page, body="<p>Ups</p>")
    good = PageVersion.objects.filter(page=page, body="<p>Den gode tekst</p>").get()
    before = PageVersion.objects.filter(page=page).count()

    response = editor_client.post(f"/django-admin/cms/page/{page.id}/versioner/{good.id}/gendan")

    assert response.status_code == 302
    page.refresh_from_db()
    assert page.body == "<p>Den gode tekst</p>"
    assert PageVersion.objects.filter(page=page).count() > before, "history must only ever grow"
    assert PageVersion.objects.filter(pk=good.pk).exists()


def test_restore_requires_post_and_an_editor_role(
    editor_client: Client, make_resident: Callable[..., Resident]
) -> None:
    page = Page.objects.create(header="Vision", slug="vision", body="<p>a</p>")
    _post_page(editor_client, page, body="<p>b</p>")
    version = PageVersion.objects.filter(page=page).order_by("created_at", "id").first()
    assert version is not None
    url = f"/django-admin/cms/page/{page.id}/versioner/{version.id}/gendan"

    assert editor_client.get(url).status_code == 405, "restoring is a write; GET must not do it"

    outsider = Client()
    outsider.force_login(make_resident(email="ak@gahk.dk", roles=[Role.AK]))
    assert outsider.post(url).status_code in {403, 302}

    assert editor_client.post(url).status_code == 302


def test_restore_keeps_the_current_address_when_the_old_one_is_taken(
    editor_client: Client,
) -> None:
    """The content is what people come here for; a clashing address must not block the restore."""
    page = Page.objects.create(header="Side", slug="side-a", body="<p>original</p>")
    _post_page(editor_client, page, path_segment="side-b", body="<p>ændret</p>")
    original = PageVersion.objects.filter(page=page, slug="side-a").first()
    assert original is not None
    Page.objects.create(header="Anden side", slug="side-a", body="")  # address reclaimed

    response = editor_client.post(
        f"/django-admin/cms/page/{page.id}/versioner/{original.id}/gendan", follow=True
    )

    page.refresh_from_db()
    assert page.body == "<p>original</p>", "the body should still have been restored"
    assert page.slug == "side-b", "the taken address must not have been forced back"
    assert any("bruges nu af en anden side" in str(m) for m in response.context["messages"])


def test_restore_re_sanitizes_an_old_snapshot(editor_client: Client) -> None:
    """A snapshot taken before the allowlist tightened must not be a way back in for bad markup."""
    page = Page.objects.create(header="Vision", slug="vision", body="<p>ok</p>")
    _post_page(editor_client, page, body="<p>nyere</p>")
    version = PageVersion.objects.filter(page=page).order_by("created_at", "id").first()
    assert version is not None
    PageVersion.objects.filter(pk=version.pk).update(body="<p>hej</p><script>alert(1)</script>")

    editor_client.post(f"/django-admin/cms/page/{page.id}/versioner/{version.id}/gendan")

    page.refresh_from_db()
    assert "<p>hej</p>" in page.body
    assert "<script" not in page.body


def test_history_is_capped(editor_client: Client) -> None:
    """Bounded by construction, so there is no pruning command to forget to run."""
    from cms.services import VERSIONS_PER_PAGE_CAP, snapshot_page

    page = Page.objects.create(header="Vision", slug="vision", body="")
    for index in range(VERSIONS_PER_PAGE_CAP + 5):
        page.body = f"<p>{index}</p>"
        snapshot_page(page, None, note=str(index))

    assert PageVersion.objects.filter(page=page).count() == VERSIONS_PER_PAGE_CAP
    newest = PageVersion.objects.filter(page=page).first()
    assert newest is not None
    assert newest.note == str(VERSIONS_PER_PAGE_CAP + 4), "the newest snapshots must be the survivors"


def test_deleting_a_page_keeps_its_history(editor_client: Client) -> None:
    """SET_NULL, not CASCADE: the snapshots *are* the recovery story for a deleted page."""
    page = Page.objects.create(header="Vision", slug="vision", body="<p>indhold</p>")
    _post_page(editor_client, page, body="<p>nyt</p>")
    assert PageVersion.objects.filter(page=page).exists()

    page.delete()

    orphaned = PageVersion.objects.filter(page__isnull=True)
    assert orphaned.exists()
    assert orphaned.filter(header="Vision").exists(), "an orphaned snapshot stays identifiable"


def test_the_history_page_shows_what_changed(editor_client: Client) -> None:
    page = Page.objects.create(header="Vision", slug="vision", body="<p>gammel</p>")
    _post_page(editor_client, page, header="Ny overskrift", body="<p>ny</p>")

    html = editor_client.get(f"/django-admin/cms/page/{page.id}/versioner/").content.decode()

    assert "Historik" in html
    assert "Gendan denne version" in html
    assert "Overskrift" in html, "the changed short field should be listed"


def test_the_history_page_escapes_the_body(editor_client: Client) -> None:
    """The diff shows body HTML *as text*; rendering it would execute stored markup in the admin."""
    page = Page.objects.create(header="Vision", slug="vision", body="<p>a</p>")
    _post_page(editor_client, page, body="<p>en <em>fed</em> tekst</p>")

    html = editor_client.get(f"/django-admin/cms/page/{page.id}/versioner/").content.decode()

    assert "&lt;em&gt;fed&lt;/em&gt;" in html
    assert "<p>en <em>fed</em> tekst</p>" not in html


# ------------------------------------------------------------------ reachability


def test_a_page_in_no_menu_is_detected() -> None:
    """The incident's real symptom, as a predicate: renaming drops a page out of its section."""
    _faciliteter()
    page = Page.objects.create(header="Køkkenet", slug="faciliteter/kokken", body="")
    assert is_reachable(page), "a sub-page of a menu section is in that section's sidebar"

    page.slug = "faciliteter-kokken"
    page.save()

    assert not is_reachable(page), "this is exactly the state nobody could see before"


def test_every_production_address_is_reachable() -> None:
    """Guards the whole legacy route map, so this cannot regress silently for the real site."""
    from cms.management.commands.etl_cms import SLUG_BY_PAGE_ID

    for page_id, slug in SLUG_BY_PAGE_ID.items():
        Page.objects.create(id=page_id, header=slug, body="")
        Page.objects.filter(id=page_id).update(slug=slug)  # bypass validators, as the ETL does

    for page in Page.objects.all():
        assert is_reachable(page), f"/{page.slug} is not linked from anywhere"


def test_the_front_page_is_not_reported_as_orphaned() -> None:
    """cms.views.home serves page id=1 at `/`, and its slug (`velkommen`) is in no menu list.

    The obvious reachability check calls that page orphaned. It is the front page.
    """
    front = Page.objects.create(id=1, header="Velkommen", slug="velkommen", body="")
    assert is_reachable(front)


def test_a_page_without_an_address_is_not_reported_as_broken() -> None:
    """The optagelse bodies have no public URL on purpose; that is not a fault to fix."""
    assert is_reachable(Page.objects.create(header="Optagelse-tekst", slug=None, body=""))


def test_get_absolute_url_matches_the_catch_all_route() -> None:
    _faciliteter()
    page = Page.objects.create(header="Køkkenet", slug="faciliteter/kokken", body="<p>Hej</p>")

    assert page.get_absolute_url() == "/faciliteter/kokken"
    assert Client().get(page.get_absolute_url()).status_code == 200
    assert Page.objects.create(header="Uden", slug=None, body="").get_absolute_url() == ""


def test_reserved_segments_cover_the_urlconf() -> None:
    """The reserved list is a hand-kept copy of urls.py; this is what stops the two drifting."""
    from cms.checks import check_reserved_top_segments

    assert check_reserved_top_segments(None) == []


def test_the_reserved_check_actually_detects_a_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the check can fail — a check that only ever passes guards nothing."""
    from cms import checks

    monkeypatch.setattr(checks, "RESERVED_TOP_SEGMENTS", frozenset())
    errors = checks.check_reserved_top_segments(None)

    assert errors, "with nothing reserved, every mounted prefix should be reported"
    assert all(error.id == "cms.E001" for error in errors)


# ------------------------------------------------------------------ the overview


def test_the_overview_flags_the_page_in_no_menu(editor_client: Client) -> None:
    _faciliteter()
    Page.objects.create(header="Køkkenet", slug="faciliteter/kokken", body="")
    orphan = Page.objects.create(header="Glemt side", slug="glemt-side", body="")

    html = editor_client.get("/django-admin/cms/page/").content.decode()

    assert "Ikke i nogen menu" in html
    # And on the right row: the orphan's own address must be the one carrying the warning.
    row = html.split(orphan.header)[1]
    assert "Ikke i nogen menu" in row.split("</tr>")[0]


def test_the_overview_links_to_the_live_page(editor_client: Client) -> None:
    _faciliteter()
    Page.objects.create(header="Køkkenet", slug="faciliteter/kokken", body="")

    html = editor_client.get("/django-admin/cms/page/").content.decode()

    assert 'href="/faciliteter/kokken"' in html


def test_the_overview_costs_the_same_whatever_the_page_count(
    editor_client: Client, django_assert_num_queries: Callable
) -> None:
    """A reachability column computed per row would be a query per page — the trap
    CmsImageAdmin.usage documents. One scan answers it for the whole list."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _faciliteter()
    for index in range(2):
        Page.objects.create(header=f"Side {index}", slug=f"side-{index}", body="")

    with CaptureQueriesContext(connection) as captured:
        editor_client.get("/django-admin/cms/page/")
    baseline = len(captured.captured_queries)

    for index in range(12):
        Page.objects.create(header=f"Mere {index}", slug=f"mere-{index}", body="")

    # Exactly the same count with six times the rows: nothing here is per-row.
    with django_assert_num_queries(baseline):
        editor_client.get("/django-admin/cms/page/")


def test_the_visibility_filter_narrows_the_list(editor_client: Client) -> None:
    _faciliteter()
    Page.objects.create(header="Køkkenet", slug="faciliteter/kokken", body="")
    Page.objects.create(header="Glemt side", slug="glemt-side", body="")

    html = editor_client.get("/django-admin/cms/page/?synlighed=orphan").content.decode()

    assert "Glemt side" in html
    assert "Køkkenet" not in html


def test_the_section_filter_narrows_the_list(editor_client: Client) -> None:
    _faciliteter()
    Page.objects.create(header="Køkkenet", slug="faciliteter/kokken", body="")
    Page.objects.create(header="Vision", slug="vision", body="")

    html = editor_client.get("/django-admin/cms/page/?sektion=faciliteter").content.decode()

    assert "Køkkenet" in html
    assert "Vision" not in html


def test_menu_category_is_not_editable_but_is_preserved(editor_client: Client) -> None:
    """Hidden from editors (it drives nothing), kept in the database (the ETL writes it)."""
    from cms.admin import PageAdminForm

    page = Page.objects.create(header="Vision", slug="vision", body="", menu_category=7)

    assert "menu_category" not in PageAdminForm().fields
    assert _post_page(editor_client, page, header="Ny titel").status_code == 302

    page.refresh_from_db()
    assert page.header == "Ny titel"
    assert page.menu_category == 7, "hiding the field must not silently reset the legacy value"


def test_only_an_administrator_may_delete_a_page(
    editor_client: Client, make_resident: Callable[..., Resident]
) -> None:
    """A deleted page has no undo, which is worse than the accident this screen was rebuilt for."""
    page = Page.objects.create(header="Vision", slug="vision", body="")

    assert editor_client.post(f"/django-admin/cms/page/{page.id}/delete/").status_code == 403
    assert Page.objects.filter(pk=page.pk).exists()
    assert "delete_selected" not in editor_client.get("/django-admin/cms/page/").content.decode()

    boss = Client()
    boss.force_login(make_resident(email="boss@gahk.dk", roles=[Role.ADMINISTRATOR]))
    assert boss.post(f"/django-admin/cms/page/{page.id}/delete/", {"post": "yes"}).status_code == 302
    assert not Page.objects.filter(pk=page.pk).exists()


def test_the_page_form_still_loads_the_image_toolbar(editor_client: Client) -> None:
    """Declaring a Media on PageAdminForm shadows BodyEditorMixin's — a silent loss of a feature.

    `media_property` treats `getattr(cls, "Media")` as *the* definition, inherited or not, so the
    new script has to re-list the old one. Nothing else would have noticed.
    """
    page = Page.objects.create(header="Vision", slug="vision", body="")

    html = editor_client.get(f"/django-admin/cms/page/{page.id}/change/").content.decode()

    assert "cms/insert_image.js" in html
    assert "cms/page_path.js" in html


def test_the_cms_index_explains_how_to_edit(editor_client: Client) -> None:
    """The first templates/admin/ override in this project; a wrong path would fail silently."""
    html = editor_client.get("/django-admin/cms/").content.decode()

    assert "underside" in html
    assert "Skift en adresse" in html


def test_the_page_list_warns_about_changing_an_address(editor_client: Client) -> None:
    Page.objects.create(header="Vision", slug="vision", body="")

    html = editor_client.get("/django-admin/cms/page/").content.decode()

    assert "sender" in html and "automatisk videre" in html


def test_saving_an_unreachable_page_warns_the_editor(editor_client: Client) -> None:
    """The message that would have caught the original incident as it happened."""
    _faciliteter()
    page = Page.objects.create(header="Køkkenet", slug="faciliteter/kokken", body="")

    response = _post_page(editor_client, page, path_parent="", path_segment="faciliteter-kokken", follow=True)

    assert any("ikke i nogen menu" in str(m).lower() for m in response.context["messages"])


def test_the_change_log_is_scoped_to_cms(editor_client: Client) -> None:
    """The site-wide "who changed what" list must not become a cross-app disclosure.

    Django's own LogEntry records every model touched through the admin, and its object_repr is a
    __str__ — applicant names, personnel data. `pr` holds CMS rights and nothing else, so the change
    log is PageVersion, which cannot hold anything but CMS pages.
    """
    page = Page.objects.create(header="Vision", slug="vision", body="")
    _post_page(editor_client, page, body="<p>ny</p>")

    html = editor_client.get("/django-admin/cms/pageversion/").content.decode()

    assert "Vision" in html
    for model in PageVersion.objects.values_list("page__header", flat=True):
        assert model is None or model == "Vision"
