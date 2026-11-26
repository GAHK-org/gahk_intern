"""What notifications leave in the audit trail: a resident's settings, and an administrator's message
to everyone. The notifications themselves, and reading them, leave nothing."""

import datetime
from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEntity
from notifications import categories, services, tasks
from notifications.categories import Importance
from notifications.models import Notification
from residents.models import Resident, Role
from tests.audit_support import actions, changes, events

pytestmark = pytest.mark.django_db


def _all_on() -> dict[str, str]:
    data = {"vibrate": "on", "vibrate_min_importance": str(int(Importance.LAV))}
    for category in categories.CATEGORIES:
        if category.in_app and category.in_app_optional:
            data[f"{category.key}__in_app"] = "on"
        data[f"{category.key}__push"] = "on"
        data[f"{category.key}__vibrate"] = "on"
    return data


def test_changing_ones_settings_is_recorded(client: Client, make_resident: Callable[..., Resident]) -> None:
    anna = make_resident(email="anna@gahk.dk", first_name="Anna", last_name="Berg")
    client.force_login(anna)
    url = reverse("notifications:preferences")
    muted = {k: v for k, v in _all_on().items() if k != f"{categories.POSTS_NEW}__push"}

    client.post(url, _all_on())  # the defaults: makes the settings row, nothing else
    client.post(url, _all_on())  # again, unchanged: nothing
    client.post(url, muted)
    client.post(url, _all_on())  # back to the default: the exception is deleted

    first, muting, unmuting = events("notifications.settings.updated")
    assert {first.actor_id, muting.actor_id, unmuting.actor_id} == {anna.pk}
    assert changes(first, "notifications.settings")["vibrate"] == [None, True]
    assert AuditEntity.objects.get(event=muting, role="subject").kind == "residents.resident"
    assert changes(muting, "notifications.preference")["push"] == [None, False]
    removed = unmuting.entities.get(kind="notifications.preference")
    assert (removed.operation, removed.name) == ("deleted", f"Anna Berg: {categories.POSTS_NEW}")


def test_a_message_to_everyone_is_recorded_with_what_it_said(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    admin = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    make_resident(email="anna@gahk.dk")
    client.force_login(admin)

    client.post(
        reverse("siteadmin:notifications"),
        {
            "category": categories.GENERAL,
            "importance": str(int(Importance.HOEJ)),
            "title": "Vandet lukkes kl. 10",
            "body": "I hele huset.",
            "url": "/",
        },
    )

    [sent] = events("notifications.broadcast.sent")
    assert sent.actor_id == admin.pk
    assert sent.details == {
        "category": categories.GENERAL,
        "importance": int(Importance.HOEJ),
        "title": "Vandet lukkes kl. 10",
        "body": "I hele huset.",
        "url": "/",
        "reached": Notification.objects.count(),
    }
    assert not sent.entities.exists()  # the notifications themselves are not in the trail


def test_notifying_reading_and_purging_leave_no_trail(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    anna = make_resident(email="anna@gahk.dk")
    services.notify([anna.pk], category=categories.GENERAL, title="Hej", url="/")
    row = Notification.objects.get()
    client.force_login(anna)

    client.get(reverse("notifications:open", args=[row.pk]))
    client.post(reverse("notifications:mark_all_seen"))
    Notification.objects.update(expires_at=timezone.now() - datetime.timedelta(days=60))
    assert tasks.purge_expired_notifications.apply().get() == 1

    assert set(actions()) == {"auth.login"}
