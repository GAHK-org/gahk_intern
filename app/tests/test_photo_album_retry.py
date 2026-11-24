"""The admin's "Prøv igen nu": convert failed items, and items waiting for the nightly retry, now."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from photo_album import tasks
from photo_album.models import DERIVATIVE_CLAIM_TTL, Album, DerivativeState, Media, MediaStatus
from residents.models import Account, Role


def _retry(kind: str) -> str:
    return reverse("siteadmin:photo_album_retry", args=[kind])


def _list(kind: str) -> str:
    return reverse("siteadmin:photo_album_list", args=[kind])


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """What was queued, instead of converting it (the files are not real)."""
    calls: list[int] = []
    monkeypatch.setattr(tasks, "queue_derivatives", lambda media: calls.append(media.pk))
    return calls


@pytest.fixture
def items(make_resident: Callable[..., Account]) -> dict[str, Media]:
    resident = make_resident(email="foto@gahk.dk")
    album = Album.objects.create(folder="2004", name="Fest")

    def item(name: str, **fields: object) -> Media:
        return Media.objects.create(
            album=album,
            requested_by=resident,
            title=name,
            filename=name,
            original=f"photo-album/{album.pk}/x/original/{name}",
            status=MediaStatus.APPROVED,
            **fields,
        )

    now = timezone.now()
    return {
        "gave_up": item("gave-up.mp4", derivative_state=DerivativeState.FAILED, derivative_attempts=3),
        # Failed a try; waits for the nightly pass.
        "waiting": item(
            "waiting.mp4",
            derivative_state=DerivativeState.PENDING,
            derivative_attempts=1,
            derivative_task_id="t1",
        ),
        # Claimed by a worker that never came back.
        "lost": item(
            "lost.mp4",
            derivative_state=DerivativeState.PENDING,
            derivative_attempts=1,
            derivative_task_id="t2",
            derivative_started_at=now - DERIVATIVE_CLAIM_TTL - timedelta(minutes=1),
        ),
        # Being converted right now.
        "converting": item(
            "converting.mp4",
            derivative_state=DerivativeState.PENDING,
            derivative_attempts=1,
            derivative_task_id="t3",
            derivative_started_at=now,
        ),
        # Just uploaded; its job is in the queue already.
        "new": item("new.mp4", derivative_state=DerivativeState.PENDING),
    }


@pytest.fixture
def admin_client(client: Client, make_resident: Callable[..., Account]) -> Client:
    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))
    return client


@pytest.mark.django_db
def test_only_administrators_retry(client: Client, make_resident: Callable[..., Account]) -> None:
    assert client.post(_retry("fejlede-versioner")).status_code == 302  # to the login
    client.force_login(make_resident(roles=(Role.FOTO,)))
    assert client.post(_retry("fejlede-versioner")).status_code == 403


@pytest.mark.django_db
def test_retry_is_only_for_the_conversion_lists(admin_client: Client) -> None:
    assert admin_client.post(_retry("papirkurv")).status_code == 404
    assert admin_client.get(_retry("fejlede-versioner")).status_code == 405


@pytest.mark.django_db
def test_retrying_all_failed_items_gives_them_three_fresh_attempts_now(
    admin_client: Client, items: dict[str, Media], queued: list[int]
) -> None:
    response = admin_client.post(_retry("fejlede-versioner"))

    assert response.status_code == 302 and response["Location"] == _list("fejlede-versioner")
    assert queued == [items["gave_up"].pk]
    items["gave_up"].refresh_from_db()
    assert (items["gave_up"].derivative_state, items["gave_up"].derivative_attempts) == (
        DerivativeState.PENDING,
        0,
    )


@pytest.mark.django_db
def test_retrying_waiting_items_skips_those_queued_or_being_converted(
    admin_client: Client, items: dict[str, Media], queued: list[int]
) -> None:
    admin_client.post(_retry("venter-versioner"))

    assert sorted(queued) == sorted([items["waiting"].pk, items["lost"].pk])
    converting = Media.objects.get(pk=items["converting"].pk)
    assert (converting.derivative_task_id, converting.derivative_attempts) == ("t3", 1)  # left to its worker


@pytest.mark.django_db
def test_retrying_one_item(admin_client: Client, items: dict[str, Media], queued: list[int]) -> None:
    admin_client.post(_retry("venter-versioner"), {"media": items["waiting"].pk})
    # One the list does not hold, or one being converted, is not retried.
    admin_client.post(_retry("fejlede-versioner"), {"media": items["waiting"].pk})
    admin_client.post(_retry("venter-versioner"), {"media": items["converting"].pk})

    assert queued == [items["waiting"].pk]


@pytest.mark.django_db
def test_a_second_press_queues_nothing_again(
    admin_client: Client, items: dict[str, Media], queued: list[int]
) -> None:
    admin_client.post(_retry("fejlede-versioner"))
    response = admin_client.post(_retry("fejlede-versioner"), follow=True)

    assert queued == [items["gave_up"].pk]
    assert "Intet at prøve igen" in response.content.decode()


@pytest.mark.django_db
def test_the_list_offers_retry_for_the_retryable_items_alone(
    admin_client: Client, items: dict[str, Media]
) -> None:
    page = admin_client.get(_list("venter-versioner")).content.decode()

    assert "Prøv alle 2 igen nu" in page
    assert page.count('name="media"') == 2
    assert f'value="{items["waiting"].pk}"' in page and f'value="{items["converting"].pk}"' not in page
    assert "Prøv igen nu" not in admin_client.get(_list("papirkurv")).content.decode()


@pytest.mark.django_db
def test_the_command_retries_failed_items_the_same_way(items: dict[str, Media], queued: list[int]) -> None:
    from django.core.management import call_command

    call_command("retry_failed_photo_album_media")

    assert queued == [items["gave_up"].pk]
