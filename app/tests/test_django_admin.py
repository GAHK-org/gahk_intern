"""The Django admin is read-only except where nothing else on the site can make the change.

The table below is the whole list of exceptions. A model registered later is held to read-only
until it is added here, on purpose: the feature's own pages are where rows are written.
"""

from collections.abc import Callable

import pytest
from django.contrib import admin
from django.test import Client, RequestFactory

from residents.models import AdminAccessGrant, Resident, Role

# (add, change, delete) for a superuser; every registered model not listed is (False, False, False).
EXCEPTIONS = {
    "core.room": (True, True, True),
    "core.workgroup": (True, True, False),
    "core.cleaning": (True, True, False),
    "photo_album.tag": (True, True, True),
    "gahkflix.tag": (True, True, True),
    "residents.resident": (False, True, False),  # is_active and is_kitchen only
    "events.rsvp": (False, True, False),  # answered_at only
    "opslagstavle.noticeimage": (False, True, False),  # notice only
    "admissions.application": (False, False, True),
    "den_hurtige.quickpost": (False, False, True),
    "den_hurtige.quickcomment": (False, False, True),
    "events.event": (False, False, True),
    "events.eventcomment": (False, False, True),
    "events.calendarfeedtoken": (False, False, True),
    "django_celery_beat.periodictask": (False, True, False),  # enabled only
}
# Admin actions are writes too. Every one besides Django's own delete_selected (which follows the
# delete right above) is listed; any other model offers none.
ACTIONS = {
    "core.outgoingmail": {"send_again"},  # until /admin/mails can send a failed mail again
}


@pytest.fixture
def superuser(make_resident: Callable) -> Resident:
    return make_resident(email="super@gahk.dk", is_superuser=True, is_staff=True)


@pytest.fixture
def administrator(make_resident: Callable) -> Resident:
    admin_ = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    AdminAccessGrant.objects.filter(resident=admin_).update(status=AdminAccessGrant.Status.APPROVED)
    return admin_


@pytest.mark.django_db
def test_only_the_listed_models_can_be_changed(superuser: Resident) -> None:
    request = RequestFactory().get("/django-admin/")
    request.user = superuser
    seen = set()
    for model, model_admin in admin.site._registry.items():
        key = model._meta.label_lower
        seen.add(key)
        rights = (
            model_admin.has_add_permission(request),
            model_admin.has_change_permission(request),
            model_admin.has_delete_permission(request),
        )
        assert rights == EXCEPTIONS.get(key, (False, False, False)), key
        assert model_admin.has_view_permission(request), key
        actions = set(model_admin.get_actions(request)) - {"delete_selected"}
        assert actions == ACTIONS.get(key, set()), key
    assert not set(EXCEPTIONS) - seen, "an exception names a model that is not registered"
    assert not set(ACTIONS) - seen, "an action exception names a model that is not registered"


@pytest.mark.django_db
def test_every_inline_is_read_only(superuser: Resident) -> None:
    request = RequestFactory().get("/django-admin/")
    request.user = superuser
    for model, model_admin in admin.site._registry.items():
        for inline in model_admin.get_inline_instances(request):
            assert not inline.has_add_permission(request, None), inline
            assert not inline.has_change_permission(request, None), inline
            assert not inline.has_delete_permission(request, None), inline


@pytest.mark.django_db
def test_every_admin_page_opens(superuser: Resident, administrator: Resident) -> None:
    """Changelists and the add page (where there is one) render for both kinds of admin."""
    for user in (superuser, administrator):
        c = Client()
        c.force_login(user)
        for model in admin.site._registry:
            opts = model._meta
            base = f"/django-admin/{opts.app_label}/{opts.model_name}/"
            assert c.get(base).status_code == 200, base
            expected_add = EXCEPTIONS.get(opts.label_lower, (False,))[0]
            assert c.get(f"{base}add/").status_code == (200 if expected_add else 403), base


@pytest.mark.django_db
def test_a_resident_change_only_touches_the_two_switches(
    administrator: Resident, make_resident: Callable
) -> None:
    target = make_resident(email="target@gahk.dk", first_name="Tove")
    c = Client()
    c.force_login(administrator)
    url = f"/django-admin/residents/resident/{target.pk}/change/"

    page = c.get(url).content.decode()
    assert target.password not in page
    assert 'name="is_active"' in page and 'name="is_kitchen"' in page
    assert 'name="is_superuser"' not in page and 'name="first_name"' not in page

    response = c.post(
        url,
        {"is_kitchen": "on", "is_superuser": "on", "is_staff": "on", "first_name": "Ændret"},
    )
    assert response.status_code == 302
    target.refresh_from_db()
    assert not target.is_active and target.is_kitchen
    assert not target.is_superuser and not target.is_staff and target.first_name == "Tove"


@pytest.mark.django_db
def test_an_administrator_cannot_make_themselves_superuser(administrator: Resident) -> None:
    c = Client()
    c.force_login(administrator)
    c.post(
        f"/django-admin/residents/resident/{administrator.pk}/change/",
        {"is_active": "on", "is_superuser": "on"},
    )
    administrator.refresh_from_db()
    assert not administrator.is_superuser


@pytest.mark.django_db
def test_a_room_keeps_its_numbers_once_it_exists(superuser: Resident) -> None:
    from core.models import Room

    room = Room.objects.create(number=999, legacy_index=999, floor="stuen", side="mod gaden")
    c = Client()
    c.force_login(superuser)
    response = c.post(
        f"/django-admin/core/room/{room.pk}/change/",
        {"number": 998, "legacy_index": 998, "floor": "1. sal", "side": "mod gaden", "note": ""},
    )
    assert response.status_code == 302
    room.refresh_from_db()
    assert (room.number, room.legacy_index, room.floor) == (999, 999, "1. sal")
