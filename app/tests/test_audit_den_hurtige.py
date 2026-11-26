"""What Den Hurtige leaves in the audit trail (den_hurtige.auditing)."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from den_hurtige import access
from den_hurtige.models import QuickPost
from residents.models import Resident, Role

from .audit_support import actions, changes, events

pytestmark = pytest.mark.django_db

FEED_URL = "/den-hurtige/"


@pytest.fixture(autouse=True)
def rollout_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access, "ACCESS_ROLES", None)


def test_a_message_and_a_reply_are_recorded_with_what_they_said(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    anna = make_resident(email="anna@gahk.dk", first_name="Anna", last_name="Berg")
    client.force_login(anna)
    client.post(FEED_URL + "opret", {"content": "Hvem har lånt boremaskinen?", "duration": "120"})
    post = QuickPost.objects.get()
    client.post(f"{FEED_URL}{post.pk}/kommentar", {"content": "Det har Bo"})

    assert actions("den_hurtige.") == ["den_hurtige.message.posted", "den_hurtige.reply.posted"]
    posted = events("den_hurtige.message.posted").get()
    assert posted.actor_id == anna.pk
    subject = posted.entities.get(role="subject")
    assert (subject.kind, subject.entity_id, subject.operation) == ("den_hurtige.message", post.pk, "created")
    assert changes(posted)["content"] == [None, "Hvem har lånt boremaskinen?"]
    assert changes(posted)["channel"] == [None, "generelt"]
    assert "created_at" not in changes(posted)

    replied = events("den_hurtige.reply.posted").get()
    assert changes(replied)["content"] == [None, "Det har Bo"]
    assert replied.entities.get(role="message").entity_id == post.pk


def test_a_deleted_message_is_recorded_whole_and_a_blanked_one_with_its_words(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    anna = make_resident(email="anna@gahk.dk")
    client.force_login(anna)
    fresh = QuickPost.objects.create(author=anna, content="Ups")
    old = QuickPost.objects.create(author=anna, content="Pakke i porten")
    QuickPost.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(minutes=30))

    client.post(f"{FEED_URL}{fresh.pk}/slet")
    client.post(f"{FEED_URL}{old.pk}/slet")

    deleted = events("den_hurtige.message.deleted").get()
    assert deleted.actor_id == anna.pk
    assert deleted.entities.get(role="subject").operation == "deleted"
    assert changes(deleted)["content"] == ["Ups", None]

    # Within the trail the words outlive the tombstone: the UPDATE that wiped them is recorded.
    blanked = events("den_hurtige.message.blanked").get()
    assert blanked.entities.get(role="subject").operation == "updated"
    assert changes(blanked)["content"] == ["Pakke i porten", ""]
    assert changes(blanked)["deleted_at"][0] is None
    assert changes(blanked)["deleted_at"][1]
    assert "image" not in changes(blanked), "no picture, so nothing about one"


def test_a_moderator_deleting_someone_elses_message_is_the_actor(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    anna = make_resident(email="anna@gahk.dk")
    admin = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    post = QuickPost.objects.create(author=anna, content="Noget")
    client.force_login(admin)

    client.post(f"{FEED_URL}{post.pk}/slet")

    assert events("den_hurtige.message.deleted", actor=admin).count() == 1


def test_reactions_leave_no_trail(client: Client, make_resident: Callable[..., Resident]) -> None:
    anna = make_resident(email="anna@gahk.dk")
    post = QuickPost.objects.create(author=anna, content="Kaffe")
    client.force_login(anna)

    client.post(f"{FEED_URL}{post.pk}/reaktion", {"emoji": "👍"})
    client.post(f"{FEED_URL}{post.pk}/reaktion", {"emoji": "👍"})

    assert actions("den_hurtige.") == []
    assert not events().filter(unclaimed=True).exists()
