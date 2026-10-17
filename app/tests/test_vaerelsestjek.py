"""Værelsestjek (F-005): resubmitting a room report."""

from collections.abc import Callable
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from core.models import Room
from rooms.models import RoomCondition, RoomConditionScore, RoomCriterion

# Smallest valid PNG, so core.uploads' image check accepts it.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


@pytest.mark.django_db
def test_resubmit_carries_photos_forward_unless_replaced(make_resident: Callable, tmp_path: Path) -> None:
    """A browser never re-posts a file input and the form has no remove control, so correcting one
    score used to drop every photo from the current report. Untouched criteria keep their photo, a new
    upload replaces it, and the superseded report keeps its own."""
    rm = Room.objects.create(legacy_index=94, number=94, floor="stuen", side="mod gaden")
    RoomCriterion.objects.create(code="floor", name="Gulv", options=5)
    RoomCriterion.objects.create(code="walls", name="Vægge", options=5)
    c = Client()
    c.force_login(make_resident(email="tjek-foto@gahk.dk"))
    url = f"/intern/vaerelsestjek/besvar/{rm.number}"

    with override_settings(MEDIA_ROOT=tmp_path):
        c.post(
            url,
            {
                "score_floor": "3",
                "image_floor": SimpleUploadedFile("gulv.png", PNG, "image/png"),
                "image_walls": SimpleUploadedFile("vaeg.png", PNG, "image/png"),
            },
        )
        first = RoomCondition.objects.get(room=rm, is_current=True)
        first_photos = {s.criterion.code: s.photo.name for s in first.scores.all()}
        assert set(first_photos) == {"floor", "walls"}

        # Update one score and replace only the walls photo.
        c.post(
            url,
            {"score_floor": "4", "image_walls": SimpleUploadedFile("ny.png", PNG, "image/png")},
        )

    current = RoomCondition.objects.get(room=rm, is_current=True)
    assert current.pk != first.pk
    photos = {s.criterion.code: s.photo.name for s in current.scores.all()}
    assert photos["floor"] == first_photos["floor"]  # carried forward
    assert photos["walls"] and photos["walls"] != first_photos["walls"]  # replaced
    assert current.scores.get(criterion__code="floor").score == 4
    # The old report is untouched.
    assert {s.criterion.code: s.photo.name for s in RoomConditionScore.objects.filter(condition=first)} == (
        first_photos
    )


@pytest.mark.django_db
def test_resubmit_carries_legacy_image_paths_forward(make_resident: Callable) -> None:
    rm = Room.objects.create(legacy_index=95, number=95, floor="stuen", side="mod gaden")
    crit = RoomCriterion.objects.create(code="floor", name="Gulv", options=5)
    who = make_resident(email="tjek-legacy@gahk.dk")
    old = RoomCondition.objects.create(
        room=rm, resident=who, recorded_at="2020-01-01T00:00Z", is_current=True
    )
    RoomConditionScore.objects.create(condition=old, criterion=crit, image="public/image/a.jpg")
    c = Client()
    c.force_login(who)

    c.post(f"/intern/vaerelsestjek/besvar/{rm.number}", {"score_floor": "2"})

    s = RoomConditionScore.objects.get(condition__room=rm, condition__is_current=True)
    assert s.score == 2
    assert s.image == "public/image/a.jpg"
