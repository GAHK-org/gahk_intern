"""The audit trail of admission applications (admissions.auditing): what Indstillingen did, and none
of what the applicant wrote, which must not outlive the application."""

import uuid
from collections.abc import Callable
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from admissions.models import Application
from admissions.services import ingest_outbox_applications
from audit.models import AuditChange, AuditEntity
from residents.models import Resident, Role

from .audit_support import actions, changes, events

pytestmark = pytest.mark.django_db

PERSONAL = ["Ada Lovelace", "ada@example.com", "DTU", "Jeg vil gerne bo her."]


def _item() -> dict[str, object]:
    return {
        "id": str(uuid.uuid4()),
        "kind": "tour",
        "submitted_at": "2026-11-10T09:30:00+01:00",
        "fields": {
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "gender": "female",
            "age": "21",
            "heard_about_us": "poster",
            "motivation": "Jeg vil gerne bo her.",
            "study_year": "2",
            "year_left": "3",
            "university": "DTU",
            "field_of_study": "Fysik",
        },
    }


def _nothing_personal_in_the_trail() -> None:
    texts = [str(v) for v in AuditChange.objects.values_list("before", "after") for v in v if v is not None]
    texts += list(AuditEntity.objects.values_list("name", flat=True))
    for personal in PERSONAL:
        assert not any(personal in text for text in texts), personal


def test_an_application_is_received_marked_and_discarded_without_the_applicants_data(
    make_resident: Callable[..., Resident],
) -> None:
    ind = make_resident(email="ind@gahk.dk", roles=[Role.INDSTILLING])
    ingest_outbox_applications([_item()])
    app = Application.objects.get()
    client = Client()
    client.force_login(ind)
    client.post(f"/optagelse/setasreceived/{app.pk}")
    client.post(f"/optagelse/kasser/{app.pk}")
    client.post(f"/optagelse/kasser/{app.pk}")

    assert actions("admissions.") == [
        "admissions.application.submitted",
        "admissions.application.marked_received",
        "admissions.application.discarded",
        "admissions.application.undiscarded",
    ]
    submitted = events("admissions.application.submitted").get()
    assert submitted.actor_id is None
    subject = submitted.entities.get(role="subject")
    assert subject.name == f"Rundvisning #{app.pk}"
    assert set(changes(submitted)) == {"type", "submitted_at"}
    received = events("admissions.application.marked_received").get()
    assert received.actor_id == ind.pk
    assert changes(received)["received_by"] == [None, ind.pk]
    assert changes(events("admissions.application.undiscarded").get())["discarded_by"] == [ind.pk, None]
    _nothing_personal_in_the_trail()


def test_the_yearly_purge_is_one_event_with_a_count() -> None:
    old = timezone.now() - timedelta(days=400)
    for _ in range(2):
        Application.objects.create(
            type="rundvisning", full_name="Ada Lovelace", email="ada@example.com", submitted_at=old
        )
    Application.objects.create(
        type="fremleje", full_name="Ny", email="ny@example.com", submitted_at=timezone.now()
    )

    call_command("purge_applications")

    purged = events("admissions.application.purged").get()
    assert purged.actor_id is None
    assert purged.details["count"] == 2
    assert purged.entities.filter(kind="admissions.application", operation="deleted").count() == 2
    assert Application.objects.count() == 1
    _nothing_personal_in_the_trail()
