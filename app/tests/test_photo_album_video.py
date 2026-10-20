"""Video conversion: the viewer copy's size, its streams, and the commands that repair old copies."""

from collections.abc import Callable
from io import StringIO

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from photo_album.models import Album, DerivativeState, Media
from photo_album.services import (
    _ffmpeg,
    build_derivatives,
    probe_video,
    read_head,
    stored_video_dimensions,
    upload_media,
)
from photo_album.storage import get_photo_album_storage
from residents.models import Resident

from .media_samples import broken_audio_mpg_bytes, sized_mp4_bytes, subtitled_mkv_bytes


def _video(
    make_resident: Callable[..., Resident], name: str, content: bytes, *, folder: str = "2004"
) -> Media:
    album, _ = Album.objects.get_or_create(folder=folder, name="Fremkaldning")
    resident = make_resident(email=f"uploader-{Media.objects.count()}@gahk.dk")
    return upload_media(album=album, uploaded_file=SimpleUploadedFile(name, content), resident=resident)


def _streams(media: Media) -> str:
    """`ffmpeg -i` on the stored viewer copy."""
    return probe_video(_ffmpeg(), "", read_head(media.high_definition, 1024 * 1024)).text


@pytest.mark.django_db
def test_a_small_video_keeps_its_own_size(make_resident: Callable[..., Resident]) -> None:
    """A 1600 px box with nothing to stop it ENLARGED every smaller video: a 320x240 camera clip came
    out 1600x1200, at twenty times the CPU and six times the original's size."""
    media = _video(make_resident, "small.mp4", sized_mp4_bytes(320, 240))

    assert build_derivatives(media)

    assert stored_video_dimensions(media.high_definition) == (320, 240)


@pytest.mark.django_db
def test_a_large_video_is_scaled_down_to_the_viewer_size(make_resident: Callable[..., Resident]) -> None:
    media = _video(make_resident, "large.mp4", sized_mp4_bytes(1920, 1080))

    assert build_derivatives(media)

    assert stored_video_dimensions(media.high_definition) == (1600, 900)


@pytest.mark.django_db
def test_a_video_with_a_broken_audio_track_is_converted_with_its_good_one(
    make_resident: Callable[..., Resident],
) -> None:
    """An old camera's MPEG carried an empty AC3 track ("0 channels") beside its real sound. Mapping
    every audio stream took the broken one in, and the whole conversion failed on it."""
    media = _video(make_resident, "Søby 027.mpg", broken_audio_mpg_bytes())

    assert build_derivatives(media)

    streams = _streams(media)
    assert streams.count("Audio: aac") == 1
    assert "mono" in streams


@pytest.mark.django_db
def test_text_subtitles_are_kept_in_the_viewer_copy(make_resident: Callable[..., Resident]) -> None:
    media = _video(make_resident, "film.mkv", subtitled_mkv_bytes())

    assert build_derivatives(media)

    subtitles = [line for line in _streams(media).splitlines() if "Subtitle:" in line]
    assert len(subtitles) == 2
    assert all("mov_text" in line for line in subtitles)
    assert "(dan)" in subtitles[0]
    assert "(default)" in subtitles[0]
    assert "(eng)" in subtitles[1]


def _enlarged(
    make_resident: Callable[..., Resident], name: str, original: bytes, copy: bytes, folder: str
) -> Media:
    """A video as the old conversion left it: READY, with `copy` as its viewer copy."""
    media = _video(make_resident, name, original, folder=folder)
    media.high_definition.save("old.mp4", ContentFile(copy), save=False)
    media.thumbnail.save("old.jpg", ContentFile(b"jpeg"), save=False)
    media.derivative_state = DerivativeState.READY
    media.save()
    return media


@pytest.mark.django_db
def test_enlarged_copies_are_re_encoded_at_the_videos_own_size(
    make_resident: Callable[..., Resident],
) -> None:
    small = _enlarged(
        make_resident, "small.mpg", sized_mp4_bytes(320, 240), sized_mp4_bytes(1600, 1200), "2004"
    )
    large = _enlarged(
        make_resident, "large.mp4", sized_mp4_bytes(1920, 1080), sized_mp4_bytes(1600, 900), "2004"
    )
    other_year = _enlarged(
        make_resident, "other.mpg", sized_mp4_bytes(320, 240), sized_mp4_bytes(1600, 1200), "2005"
    )
    old_copy = small.high_definition.name

    listed = StringIO()
    call_command("reencode_enlarged_photo_album_videos", "--year", "2004", "--dry-run", stdout=listed)
    assert "small.mpg" in listed.getvalue()
    assert "original 320x240 -> copy 1600x1200" in listed.getvalue()
    assert "large.mp4" not in listed.getvalue()  # its original is larger than the copy
    assert "1 of 2 video(s) to re-encode" in listed.getvalue()
    small.refresh_from_db()
    assert small.high_definition.name == old_copy

    call_command("reencode_enlarged_photo_album_videos", "--year", "2004", stdout=StringIO())

    small.refresh_from_db()
    assert stored_video_dimensions(small.high_definition) == (320, 240)
    assert small.derivative_state == DerivativeState.READY
    assert not get_photo_album_storage().exists(old_copy)
    other_year.refresh_from_db()
    assert stored_video_dimensions(other_year.high_definition) == (1600, 1200)
    assert stored_video_dimensions(Media.objects.get(pk=large.pk).high_definition) == (1600, 900)

    again = StringIO()
    call_command("reencode_enlarged_photo_album_videos", "--year", "2004", stdout=again)
    assert "Queued 0 of 2" in again.getvalue()


@pytest.mark.django_db
def test_a_failed_re_encode_leaves_the_old_copy_in_place(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from photo_album import services
    from photo_album.tasks import DerivativesNotBuilt, reencode_enlarged_video

    media = _enlarged(
        make_resident, "small.mpg", sized_mp4_bytes(320, 240), sized_mp4_bytes(1600, 1200), "2004"
    )
    old_copy = media.high_definition.name

    def fails(*_: object) -> None:
        raise subprocess.CalledProcessError(1, ["ffmpeg"], stderr="Invalid data found when processing input")

    monkeypatch.setattr(services, "_encode_video", fails)
    with pytest.raises(DerivativesNotBuilt, match="old copy is kept"):
        reencode_enlarged_video.apply(args=(media.pk,))

    media.refresh_from_db()
    assert media.derivative_state == DerivativeState.READY
    assert media.high_definition.name == old_copy
    assert get_photo_album_storage().exists(old_copy)


@pytest.mark.django_db
def test_failed_media_can_be_given_fresh_attempts(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    from photo_album.management.commands import retry_failed_photo_album_media

    failed = _video(make_resident, "broken.mpg", b"not a video")
    elsewhere = _video(make_resident, "broken.avi", b"not a video", folder="2005")
    Media.objects.filter(pk__in=[failed.pk, elsewhere.pk]).update(
        derivative_state=DerivativeState.FAILED, derivative_attempts=3
    )
    queued: list[int] = []
    monkeypatch.setattr(
        retry_failed_photo_album_media, "queue_derivatives", lambda media: queued.append(media.pk)
    )

    call_command("retry_failed_photo_album_media", "--year", "2004", "--dry-run", stdout=StringIO())
    assert Media.objects.get(pk=failed.pk).derivative_state == DerivativeState.FAILED
    assert queued == []

    call_command("retry_failed_photo_album_media", "--year", "2004", stdout=StringIO())

    failed.refresh_from_db()
    assert (failed.derivative_state, failed.derivative_attempts) == (DerivativeState.PENDING, 0)
    assert queued == [failed.pk]
    assert Media.objects.get(pk=elsewhere.pk).derivative_state == DerivativeState.FAILED
