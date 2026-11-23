"""The Hjemmeside's bin (cms.bin): nothing an editor made is deleted outright. "Slet" bins it, the bin
restores it, and only after KEEP_FOR may one item at a time be deleted for good."""

from collections.abc import Callable
from datetime import timedelta
from io import BytesIO
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from cms import bin as cms_bin
from cms import services
from cms.models import CmsImage, Event, MenuItem, NewsItem, Page, PageRedirect, PageVersion
from cms.snapshot import build_snapshot
from residents.models import Resident

from .audit_support import actions, events


@pytest.fixture
def editor(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="pr@gahk.dk", first_name="Rita", last_name="Redaktør", roles=("pr",))


@pytest.fixture
def admin(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="admin@gahk.dk", first_name="Ada", last_name="Admin", roles=("administrator",))


@pytest.fixture
def front(db: None) -> Page:
    return Page.put_front_page(slug="velkommen", header="Velkommen", body="<p>Hej</p>")


def client_for(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def _image(by: Resident, caption: str = "Gangen") -> CmsImage:
    buffer = BytesIO()
    Image.new("RGB", (40, 30), "green").save(buffer, format="PNG")
    return services.upload_image(
        SimpleUploadedFile("g.png", buffer.getvalue(), content_type="image/png"), caption, by
    )


def _age(obj: Any, days: float) -> None:  # noqa: ANN401 - a Binnable
    """Pretend `obj` (and what was binned with it) went in the bin `days` ago."""
    model = type(obj)
    binned_at = model.all_objects.get(pk=obj.pk).binned_at
    then = timezone.now() - timedelta(days=days)
    model.all_objects.filter(pk=obj.pk).update(binned_at=then)
    if isinstance(obj, Page):
        PageRedirect.all_objects.filter(page=obj, binned_at=binned_at).update(binned_at=then)


# ----------------------------------------------------------------------------------- every kind
@pytest.mark.django_db
def test_every_kind_leaves_gahk_dk_for_the_bin_and_comes_back(editor: Resident, front: Page) -> None:
    page = services.save_page(None, {"header": "Vision", "slug": "vision", "body": "<p>Mål</p>"}, editor)
    news = services.save_news(None, {"title": "Nyt", "body": "<p>x</p>"}, editor)
    event = services.save_event(None, {"title": "Fest", "starts_on": "2030-01-01"}, editor)
    redirect = services.save_redirect(None, {"old_path": "maal", "page": page}, editor)
    image = _image(editor)
    menu_item = MenuItem.objects.create(label="Vores vision", url="/vores-vision", order=999)
    things = [page, news, event, redirect, image, menu_item]
    before = build_snapshot().data

    # The address on its own first: a page binned first would take it along (and list it with itself).
    for thing in [redirect, page, news, event, image, menu_item]:
        cms_bin.put_in_bin(thing, editor)

    snapshot = build_snapshot().data
    assert [p["path"] for p in snapshot["pages"]] == [""]
    assert snapshot["news"] == [] and snapshot["events"] == [] and snapshot["redirects"] == {"velkommen": ""}
    assert "/vores-vision" not in [m["url"] for m in snapshot["menu"]]
    assert not CmsImage.objects.exists() and services.library_urls() == set()
    assert {entry.obj for entry in cms_bin.entries()} == set(things) - {redirect}  # it waits with its page
    assert cms_bin.count() == len(things) - 1

    for thing in things:
        assert cms_bin.restore(thing, editor) == []

    restored = build_snapshot().data
    for key in ("pages", "news", "events", "redirects"):
        assert restored[key] == before[key], key
    assert restored["menu"][-1] == {"label": "Vores vision", "url": "/vores-vision"}
    assert cms_bin.entries() == []
    kinds = ("page", "news", "event", "redirect", "image", "menu_item")
    for kind in kinds:
        assert events(f"cms.{kind}.binned").count() == 1, kind
        assert events(f"cms.{kind}.restored_from_bin").count() == 1, kind


@pytest.mark.django_db
def test_nothing_in_the_editor_deletes_a_row(editor: Resident, admin: Resident, front: Page) -> None:
    """Every "Slet" the editor offers, pressed: each thing is still in the database afterwards."""
    page = services.save_page(None, {"header": "Vision", "slug": "vision"}, editor)
    services.save_page(page, {"slug": "vores-vision"}, editor)
    news = services.save_news(None, {"title": "Nyt"}, editor)
    event = services.save_event(None, {"title": "Fest", "starts_on": "2030-01-01"}, editor)
    image = _image(editor)
    redirect = services.save_redirect(None, {"old_path": "folder", "page": page}, editor)
    menu = list(MenuItem.objects.values_list("label", "url"))
    counts = {
        model: model.all_objects.count()
        for model in (Page, NewsItem, Event, CmsImage, PageRedirect, MenuItem)
    }

    client = client_for(admin)
    client.post(reverse("cms:page_delete", args=[page.pk]))
    client.post(reverse("cms:news_delete", args=[news.pk]))
    client.post(reverse("cms:event_delete", args=[event.pk]))
    client.post(reverse("cms:image_delete", args=[image.pk]))
    client.post(reverse("cms:redirect_delete", args=[redirect.pk]))
    data = {"rows": str(len(menu)), "action": "remove-0"}
    for index, (label, url) in enumerate(menu):
        data |= {f"label_{index}": label, f"url_{index}": url}
    client.post(reverse("cms:menu"), data)

    assert {model: model.all_objects.count() for model in counts} == counts
    assert not Page.objects.exclude(pk=front.pk).exists()
    assert MenuItem.objects.count() == len(menu) - 1


# ------------------------------------------------------------------------------------- the 30 days
@pytest.mark.django_db
def test_an_item_can_be_deleted_for_good_only_after_thirty_days(editor: Resident) -> None:
    news = services.save_news(None, {"title": "Nyt"}, editor)
    with pytest.raises(ValidationError, match="papirkurven"):
        cms_bin.delete_for_good(news, editor)  # not in the bin

    cms_bin.put_in_bin(news, editor)
    binned_at = NewsItem.all_objects.get().binned_at
    assert cms_bin.deletable_from(NewsItem.all_objects.get()) == binned_at + timedelta(days=30)
    with pytest.raises(ValidationError, match="først"):
        cms_bin.delete_for_good(news, editor, now=binned_at + timedelta(days=30, seconds=-1))
    assert NewsItem.all_objects.exists()

    cms_bin.delete_for_good(news, editor, now=binned_at + timedelta(days=30))

    assert not NewsItem.all_objects.exists()
    assert actions("cms.news.") == ["cms.news.created", "cms.news.binned", "cms.news.deleted"]


@pytest.mark.django_db
def test_the_bin_never_empties_itself(editor: Resident) -> None:
    from cms import tasks

    news = services.save_news(None, {"title": "Gammel"}, editor)
    cms_bin.put_in_bin(news, editor)
    _age(news, 400)
    for task in (tasks.sync_gahk_web, tasks.publish_gahk_web):
        task.apply()  # gahk.dk is not there in the tests, which apply() records; only the bin matters
    assert NewsItem.all_objects.filter(pk=news.pk).exists()


# ------------------------------------------------------------------------------------------- pages
@pytest.mark.django_db
def test_a_page_takes_its_old_addresses_along_and_gives_its_own_up(editor: Resident, front: Page) -> None:
    page = services.save_page(None, {"header": "Vision", "slug": "vision", "body": "<p>Mål</p>"}, editor)
    services.save_page(page, {"slug": "vores-vision"}, editor)
    cms_bin.put_in_bin(page, editor)

    assert not PageRedirect.objects.exists()
    [entry] = cms_bin.entries()
    assert (entry.name, entry.detail, entry.redirects) == ("Vision", "/vores-vision", ["/vision"])
    # Its address and old address are free for a new page while it waits.
    newcomer = services.save_page(None, {"header": "Ny vision", "slug": "vores-vision"}, editor)
    services.save_redirect(None, {"old_path": "vision", "page": newcomer}, editor)

    warnings = cms_bin.restore(page, editor)

    page = Page.objects.get(pk=page.pk)
    assert page.slug is None and page.body == "<p>Mål</p>"
    assert "/vores-vision" in warnings[0] and "/vision" in warnings[1]
    assert PageRedirect.objects.get(old_path="vision").page == newcomer
    assert PageRedirect.all_objects.get(old_path="vision", page=page).binned_at is not None
    # Left in the bin on its own now that its page is back: restorable once the address is free.
    assert [e.name for e in cms_bin.entries()] == ["/vision"]


@pytest.mark.django_db
def test_a_page_comes_back_with_its_address_and_old_addresses(editor: Resident, front: Page) -> None:
    page = services.save_page(None, {"header": "Vision", "slug": "vision"}, editor)
    services.save_page(page, {"slug": "vores-vision"}, editor)
    cms_bin.put_in_bin(page, editor)

    assert cms_bin.restore(page, editor) == []

    assert Page.objects.get(pk=page.pk).slug == "vores-vision"
    assert PageRedirect.objects.get(old_path="vision").page_id == page.pk
    assert build_snapshot().data["redirects"]["vision"] == "vores-vision"


@pytest.mark.django_db
def test_a_page_deleted_for_good_keeps_its_versions(editor: Resident, front: Page) -> None:
    page = services.save_page(None, {"header": "Væk", "slug": "vaek", "body": "<p>tekst</p>"}, editor)
    services.save_page(page, {"slug": "helt-vaek"}, editor)
    cms_bin.put_in_bin(page, editor)
    _age(page, 31)

    cms_bin.delete_for_good(page, editor)

    assert not Page.all_objects.filter(pk=page.pk).exists()
    assert not PageRedirect.all_objects.exists()  # its old address went with it
    assert PageVersion.objects.filter(header="Væk", page__isnull=True).count() >= 2
    deleted = events("cms.page.deleted").get()
    assert {e.kind for e in deleted.entities.all()} == {"cms.page", "cms.redirect"}


@pytest.mark.django_db
def test_renaming_a_page_back_bins_the_redirect_it_no_longer_needs(editor: Resident, front: Page) -> None:
    page = services.save_page(None, {"header": "Vision", "slug": "vision"}, editor)
    services.save_page(page, {"slug": "vores-vision"}, editor)
    services.save_page(page, {"slug": "vision"}, editor)

    stale = PageRedirect.all_objects.get(old_path="vision")
    assert stale.binned_at is not None and stale.binned_by == editor
    assert PageRedirect.objects.get().old_path == "vores-vision"
    with pytest.raises(ValidationError, match="adressen på siden"):
        cms_bin.restore(stale, editor)


# ------------------------------------------------------------------------------------------ images
@pytest.mark.django_db
def test_an_image_binned_content_shows_is_kept_until_that_content_goes(
    editor: Resident, front: Page, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    image = _image(editor)
    news = services.save_news(None, {"title": "Med billede", "body": f'<img src="{image.url}">'}, editor)
    cms_bin.put_in_bin(news, editor)
    cms_bin.put_in_bin(image, editor)  # nothing on gahk.dk shows it now
    _age(image, 31)
    _age(news, 31)

    [image_entry] = [e for e in cms_bin.entries() if e.kind.key == "image"]
    assert image_entry.usage == ["Nyhed: Med billede (i papirkurven)"]
    with pytest.raises(ValidationError, match="bruges stadig"):
        cms_bin.delete_for_good(image, editor)
    warnings = cms_bin.restore(news, editor)
    assert "Gangen" in warnings[0]  # the news is back, its picture is still in the bin

    cms_bin.put_in_bin(NewsItem.objects.get(), editor)
    _age(news, 31)
    cms_bin.delete_for_good(NewsItem.all_objects.get(), editor)
    name = image.file.name
    with django_capture_on_commit_callbacks(execute=True):
        cms_bin.delete_for_good(image, editor)

    assert not CmsImage.all_objects.exists()
    assert not CmsImage._meta.get_field("file").storage.exists(name)  # the file went with it


# -------------------------------------------------------------------------------------------- menu
@pytest.mark.django_db
def test_a_menu_entry_comes_back_at_the_end_while_there_is_room(editor: Resident) -> None:
    first = MenuItem.objects.first()
    assert first is not None
    cms_bin.put_in_bin(first, editor)
    assert first.label not in [m["label"] for m in services.menu_state()]
    assert events("cms.menu_item.binned").get().entities.filter(kind="cms.menu").exists()

    cms_bin.restore(first, editor)
    assert services.menu_state()[-1]["label"] == first.label

    cms_bin.put_in_bin(first, editor)
    for index in range(services.MAX_MENU_ITEMS - MenuItem.objects.count()):
        MenuItem.objects.create(label=f"Punkt {index}", url="/", order=1000 + index)
    with pytest.raises(ValidationError, match="plads"):
        cms_bin.restore(first, editor)


@pytest.mark.django_db
def test_the_menu_editor_keeps_rows_by_id(editor: Resident) -> None:
    rows = [{"label": i.label, "url": i.url, "id": str(i.pk)} for i in MenuItem.objects.all()]
    pks = [int(row["id"]) for row in rows]
    rows[0]["label"] = "Forside"  # renamed, not replaced
    services.save_menu([*rows[1:], rows[0], {"label": "Ny", "url": "/ny"}], editor)

    assert list(MenuItem.objects.values_list("pk", flat=True))[:-1] == [*pks[1:], pks[0]]
    assert MenuItem.objects.get(pk=pks[0]).label == "Forside"
    assert not MenuItem.all_objects.filter(binned_at__isnull=False).exists()


# ------------------------------------------------------------------------------------------- views
@pytest.mark.django_db
def test_the_bin_page_offers_restore_always_and_delete_after_thirty_days(
    editor: Resident, admin: Resident, front: Page
) -> None:
    page = services.save_page(None, {"header": "Gammel side", "slug": "gammel"}, editor)
    news = services.save_news(None, {"title": "Gammel nyhed"}, editor)
    cms_bin.put_in_bin(page, admin)
    cms_bin.put_in_bin(news, editor)
    delete_news = reverse("cms:bin_delete", args=["news", news.pk])
    delete_page = reverse("cms:bin_delete", args=["page", page.pk])

    html = client_for(editor).get(reverse("cms:bin")).content.decode()
    assert "Gammel side" in html and "Gammel nyhed" in html and "Kan slettes permanent fra" in html
    assert delete_news not in html
    client_for(editor).post(delete_news)  # too early, even when posted by hand
    assert NewsItem.all_objects.filter(pk=news.pk).exists()

    _age(news, 31)
    _age(page, 31)
    html = client_for(editor).get(reverse("cms:bin")).content.decode()
    assert delete_news in html and delete_page not in html  # a page is for administrators
    assert client_for(editor).post(delete_page).status_code == 403
    assert delete_page in client_for(admin).get(reverse("cms:bin")).content.decode()

    client_for(editor).post(delete_news)
    client_for(admin).post(delete_page)
    assert not NewsItem.all_objects.exists() and not Page.all_objects.filter(pk=page.pk).exists()


@pytest.mark.django_db
def test_any_editor_restores_and_lands_on_the_restored_thing(
    editor: Resident, admin: Resident, front: Page
) -> None:
    page = services.save_page(None, {"header": "Vision", "slug": "vision"}, editor)
    cms_bin.put_in_bin(page, admin)

    response = client_for(editor).post(reverse("cms:bin_restore", args=["page", page.pk]))

    assert response["Location"] == reverse("cms:page_edit", args=[page.pk])
    assert Page.objects.filter(pk=page.pk).exists()
    # Not in the bin any more: nothing to restore or delete there.
    assert client_for(editor).post(reverse("cms:bin_restore", args=["page", page.pk])).status_code == 404
    assert client_for(editor).post(reverse("cms:bin_delete", args=["nonsense", page.pk])).status_code == 404


@pytest.mark.django_db
def test_a_binned_item_s_editor_is_gone_and_the_log_points_into_the_bin(
    editor: Resident, front: Page
) -> None:
    news = services.save_news(None, {"title": "Nyt"}, editor)
    cms_bin.put_in_bin(news, editor)

    client = client_for(editor)
    assert client.get(reverse("cms:news_edit", args=[news.pk])).status_code == 404
    html = client.get(reverse("cms:change_log")).content.decode()
    assert "Lagde nyheden i papirkurven" in html
    assert f"{reverse('cms:bin')}#news-{news.pk}" in html
    assert client.get(reverse("cms:bin")).content.decode().count(f'id="news-{news.pk}"') == 1


@pytest.mark.django_db
def test_a_page_s_redirect_cannot_come_back_without_its_page(
    editor: Resident, admin: Resident, front: Page
) -> None:
    page = services.save_page(None, {"header": "Vision", "slug": "vision"}, editor)
    redirect = services.save_redirect(None, {"old_path": "maal", "page": page}, editor)
    cms_bin.put_in_bin(redirect, editor)  # on its own first
    cms_bin.put_in_bin(page, admin)

    assert [e.name for e in cms_bin.entries()] == ["Vision"]  # the address waits with its page
    with pytest.raises(ValidationError, match="Gendan siden først"):
        cms_bin.restore(PageRedirect.all_objects.get(pk=redirect.pk), editor)
    cms_bin.restore(page, editor)
    # Binned before its page was, so it stays: the page's restore brings back only what went with it.
    assert [e.name for e in cms_bin.entries()] == ["/maal"]
