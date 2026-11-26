"""Real, lightweight pictures for the local photo-album demo, and residents' uploads and albums
awaiting Fotogruppen's approval (photo_album.approvals)."""

import random
from datetime import datetime, timedelta
from io import BytesIO

from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.text import slugify

from notifications import categories
from notifications.categories import Importance
from notifications.models import Notification
from notifications.services import grouped_title
from residents.models import Resident

from . import approvals
from .access import approvers
from .models import NO_YEAR_FOLDER, Album, AlbumMerge, AlbumMergeState, Media
from .services import add_tag, create_album, reject, upload_media

ALBUMS = [
    ("2026", "Sommerfest", 6),
    # A "/" makes a sub-album, so the demo has a tree to browse — one level deeper than can be made
    # by hand, as a ZIP import would.
    ("2026", "Sommerfest/Fredag", 4),
    ("2026", "Sommerfest/Lørdag", 4),
    ("2026", "Sommerfest/Lørdag/Natten", 3),
    ("2026", "Hyttetur", 6),
    ("2026", "Forårstur", 6),
    ("2025", "Julefrokost", 6),
    ("2025", "Studietur", 2),
    ("2025", "Studietur/Berlin", 5),
    ("2025", "Studietur/Berlin/Museer", 3),
    ("2025", "Studietur/Prag", 4),
    (NO_YEAR_FOLDER, "Hverdagsglimt", 6),
    (NO_YEAR_FOLDER, "Fælleskøkken", 6),
    (NO_YEAR_FOLDER, "Sport", 2),
    (NO_YEAR_FOLDER, "Sport/Fodbold", 4),
    (NO_YEAR_FOLDER, "Sport/Fodbold/Pokalfinale", 5),
    (NO_YEAR_FOLDER, "Sport/Badminton", 3),
    (NO_YEAR_FOLDER, "Paginationstest", 60),
]
# Tags on albums apply to everything below them, so these are spread over every level of the trees
# above: the timeline's tag filter then has direct, inherited and combined hits to show.
ALBUM_TAGS = {
    ("2026", "Sommerfest"): ["sommerfest", "fest"],
    ("2026", "Sommerfest/Lørdag"): ["koncert"],
    ("2026", "Sommerfest/Lørdag/Natten"): ["nattefest"],
    ("2025", "Julefrokost"): ["jul", "fest"],
    ("2025", "Studietur"): ["studietur", "rejse"],
    ("2025", "Studietur/Berlin"): ["berlin"],
    ("2025", "Studietur/Berlin/Museer"): ["museum"],
    ("2025", "Studietur/Prag"): ["prag"],
    (NO_YEAR_FOLDER, "Sport"): ["sport"],
    (NO_YEAR_FOLDER, "Sport/Fodbold"): ["fodbold"],
    (NO_YEAR_FOLDER, "Sport/Fodbold/Pokalfinale"): ["pokal.2025"],
}
# (folder, album path, image index) -> the item's own tags.
MEDIA_TAGS = {
    ("2026", "Sommerfest", 0): ["velkomst"],
    ("2026", "Sommerfest", 4): ["gruppebillede", "portræt"],
    ("2026", "Sommerfest/Fredag", 2): ["fællessang"],
    ("2026", "Sommerfest/Lørdag/Natten", 0): ["dans", "fællessang", "sent-på-aftenen"],
    ("2026", "Hyttetur", 3): ["natur", "sol"],
    ("2026", "Hyttetur", 4): ["gruppebillede"],
    ("2026", "Forårstur", 3): ["natur"],
    ("2025", "Julefrokost", 2): ["fællessang"],
    ("2025", "Julefrokost", 4): ["gruppebillede", "julefrokost_2025"],
    ("2025", "Studietur/Berlin", 1): ["mad"],
    ("2025", "Studietur/Berlin/Museer", 0): ["arkitektur", "kunst"],
    ("2025", "Studietur/Prag", 3): ["natur", "arkitektur"],
    (NO_YEAR_FOLDER, "Hverdagsglimt", 0): ["morgenkaffe"],
    (NO_YEAR_FOLDER, "Hverdagsglimt", 5): ["oprydning", "fælleskøkken"],
    (NO_YEAR_FOLDER, "Fælleskøkken", 1): ["mad", "fælleskøkken"],
    (NO_YEAR_FOLDER, "Sport/Fodbold/Pokalfinale", 4): ["gruppebillede", "sejr"],
    (NO_YEAR_FOLDER, "Sport/Badminton", 0): ["badminton"],
}
COLOURS = ["#dd6b43", "#1f6c64", "#d8a32f", "#315b8d", "#8a4e70", "#517544"]
DIMENSIONS = [(1600, 900), (900, 1600), (1200, 800), (1000, 1000), (1600, 1067), (1067, 1600)]
CAPTIONS = [
    "Ankomst og velkomst",
    "Langbordet er dækket",
    "Fællessang sent på aftenen",
    "Pause i solen",
    "Holdet samlet",
    "Oprydning dagen derpå",
]
LIFECYCLE_MEDIA = [
    (NO_YEAR_FOLDER, "Testdata: sletning", "Uploadet for 40 minutter", timedelta(minutes=40), None),
    (NO_YEAR_FOLDER, "Testdata: sletning", "Uploadet for 14 dage", timedelta(days=14), None),
    (NO_YEAR_FOLDER, "Testdata: sletning", "Uploadet for 31 dage", timedelta(days=31), None),
    (
        NO_YEAR_FOLDER,
        "Papirkurv: nylig",
        "Uploadet for 25 minutter",
        timedelta(minutes=25),
        timedelta(minutes=5),
    ),
    (NO_YEAR_FOLDER, "Papirkurv: nylig", "Uploadet for 12 dage", timedelta(days=12), timedelta(days=2)),
    (
        NO_YEAR_FOLDER,
        "Papirkurv: gammel",
        "Uploadet for 45 dage - i papirkurven i 31 dage",
        timedelta(days=45),
        timedelta(days=31),
    ),
    ("2025", "Låst demoalbum", "Sidste upload for 91 dage", timedelta(days=91), None),
]


def _album(folder: str, path: str) -> Album:
    parent: Album | None = None
    for name in path.split("/"):
        parent, _ = Album.objects.get_or_create(folder=folder, name=name, parent=parent)
    assert parent is not None  # noqa: S101 — a path has at least one name  # nosec B101
    return parent


def seed(residents: list[Resident], _now: datetime, _rng: random.Random) -> int:
    if not residents:
        return 0
    made = 0
    for album_index, (folder, album_path, count) in enumerate(ALBUMS):
        album = _album(folder, album_path)
        album_name = album.name
        for name in ALBUM_TAGS.get((folder, album_path), []):
            add_tag(album, name)
        for image_index in range(count):
            filename = f"{slugify(album_name)}-{slugify(CAPTIONS[image_index % len(CAPTIONS)])}-{image_index + 1}.jpg"
            title = filename
            media_tags = MEDIA_TAGS.get((folder, album_path, image_index), [])
            if (existing := album.media.filter(title=title).first()) is not None:
                _tag(existing, media_tags)  # so a re-seed tags what an older seed made
                continue
            media = upload_media(
                album=album,
                uploaded_file=ContentFile(
                    _image(
                        title,
                        COLOURS[(album_index + image_index) % len(COLOURS)],
                        DIMENSIONS[(album_index + image_index) % len(DIMENSIONS)],
                    ),
                    name=filename,
                ),
                resident=residents[(album_index + image_index) % len(residents)],
                title=title,
                approved=True,
            )
            media.added_at = _now - timedelta(days=album_index * 4 + image_index)
            media.save(update_fields=["added_at"])
            _tag(media, media_tags)
            made += 1
    for media_index, (folder, album_name, caption, upload_age, bin_age) in enumerate(LIFECYCLE_MEDIA):
        album = _album(folder, album_name)
        filename = f"{slugify(caption)}.jpg"
        title = filename
        if album.media.filter(title=title).exists():
            continue
        media = upload_media(
            album=album,
            uploaded_file=ContentFile(
                _image(
                    title,
                    COLOURS[media_index % len(COLOURS)],
                    DIMENSIONS[media_index % len(DIMENSIONS)],
                ),
                name=filename,
            ),
            resident=residents[media_index % len(residents)],
            title=title,
            approved=True,
        )
        media.added_at = _now - upload_age
        if bin_age is not None:
            media.deleted_at = _now - bin_age
            media.deleted_by = residents[media_index % len(residents)]
            media.save(update_fields=["added_at", "deleted_at", "deleted_by"])
        else:
            media.save(update_fields=["added_at"])
        made += 1
    return made + _seed_approvals(residents, _now)


# --- Godkend (photo_album.approvals) ------------------------------------------------------------
# Who the approval cases below involve: two documented logins, so the page can be tried from both
# sides, and five residents outside them, so the list has more than one name on it.
APPROVER = "formand@gahk.dk"  # an administrator, which counts as Fotogruppen: Godkend is theirs
UPLOADER = "beboer@gahk.dk"  # a plain resident: their pending album and uploads show only to them
LOGINS = {"admin@gahk.dk", APPROVER, "ak@gahk.dk", "oel@gahk.dk", "regnskab@gahk.dk", UPLOADER}
OTHER_UPLOADERS = 5
# Why an approval's merge failed (photo_album.merge's refusal): the album it went into was locked
# meanwhile.
FAILED_MERGE_ERROR = "Begge albummer skal være låst op, før de kan flettes."
APPROVALS_VERB = "har uploadet til fotoalbummet"  # notify.approval_requested's


def _seed_approvals(residents: list[Resident], now: datetime) -> int:
    """A Godkend page with a request of every kind on it, and what a rejection leaves behind.

    Requests are worked out from the pending rows (approvals.pending_requests), so these are pending
    uploads and albums, made through the services as a resident's own would be. A re-seed finds each
    case already there and adds nothing.
    """
    by_email = {resident.email: resident for resident in residents}
    others = [
        resident
        for resident in residents
        if resident.email not in LOGINS and not resident.is_superuser and resident.move_out_date is None
    ][:OTHER_UPLOADERS]
    approver, bente = by_email.get(APPROVER), by_email.get(UPLOADER)
    if approver is None or bente is None or len(others) < OTHER_UPLOADERS:
        return 0  # too few residents (a test of the albums above): nobody to ask, nobody to approve
    anders, carl, dorthe, erik, frida = others
    made = 0

    # Two residents' uploads to the same album are two requests, judged each on its own. Bente's
    # reach two levels of sub-albums, so her request's page groups them by sub-album.
    sommerfest = _album("2026", "Sommerfest")
    made += _upload(sommerfest, bente, 2, now - timedelta(days=2))
    made += _upload(_album("2026", "Sommerfest/Lørdag"), bente, 1, now - timedelta(days=2))
    made += _upload(_album("2026", "Sommerfest/Lørdag/Natten"), bente, 1, now - timedelta(days=2))
    # The other one in the same tree comes with a sub-album its uploader asked for, which the
    # approver keeps (under its name or another) or merges into an album that exists.
    made += _upload(sommerfest, anders, 3, now - timedelta(hours=6))
    sunday = _pending_album("2026", "Søndag", anders, now - timedelta(hours=6), parent=sommerfest)
    made += _upload(sunday, anders, 2, now - timedelta(hours=6))

    # Uploads to a sub-album only: the request is still its root album's. 28 days old, they go in two
    # unless approved, so this heads the list and makes the daily reminder Høj (URGENT_BEFORE).
    made += _upload(_album("2025", "Studietur/Prag"), carl, 3, now - timedelta(days=28))

    # Bente's own album with a sub-album, which only she and Fotogruppen see. Fotogruppen has since
    # made one of the same name — a pending name holds no place — so the page says so, starts on
    # merging hers into it and refuses keeping the name.
    halloween = _pending_album("2026", "Halloween", bente, now - timedelta(days=4))
    made += _upload(halloween, bente, 3, now - timedelta(days=4))
    costumes = _pending_album("2026", "Kostumer", bente, now - timedelta(days=4), parent=halloween)
    made += _upload(costumes, bente, 2, now - timedelta(days=4))
    if not Album.objects.filter(folder="2026", parent=None, name="Halloween", pending=False).exists():
        taken = create_album(name="Halloween", folder="2026", created_by=approver)
        made += _upload(taken, approver, 2, now - timedelta(days=1), approved=True)

    # An album asked for with nothing in it yet: a request of its own, waiting since it was made.
    _pending_album(NO_YEAR_FOLDER, "Brætspilsaften", dorthe, now - timedelta(hours=1))

    # An approval that merged the album into one locked meanwhile: the request is back, with why.
    juleklip = _pending_album("2025", "Juleklip", erik, now - timedelta(days=9))
    made += _upload(juleklip, erik, 3, now - timedelta(days=9))
    if not juleklip.merged_into.exists():
        failed = AlbumMerge.objects.create(
            target=_album("2025", "Julefrokost"),
            source=juleklip,
            source_path=juleklip.name,
            requested_by=approver,
            state=AlbumMergeState.FAILED,
            error=FAILED_MERGE_ERROR,
            completed_at=now - timedelta(hours=3),
            approve_media_ids=list(juleklip.media.values_list("pk", flat=True)),
        )
        # Within a day of it, so the album it went into tells its approver too (views.detail).
        AlbumMerge.objects.filter(pk=failed.pk).update(created_at=now - timedelta(hours=3, minutes=2))

    # What a rejection leaves: the items in the bins, where Fotogruppen can restore them, and the
    # rejected album hidden but to its maker, whose page says it was rejected. Neither is a request.
    roof = _pending_album("2026", "Tagterrassen", frida, now - timedelta(days=6))
    made += _upload(roof, frida, 2, now - timedelta(days=6), rejected_by=approver)
    made += _upload(_album("2026", "Hyttetur"), frida, 1, now - timedelta(days=6), rejected_by=approver)

    if made:
        _seed_approval_notifications(now, newest=[dorthe, anders])
    return made


def _pending_album(
    folder: str, name: str, maker: Resident, created_at: datetime, parent: Album | None = None
) -> Album:
    """An album `maker` asked for (services.create_album, pending), made at `created_at`."""
    existing = Album.objects.filter(
        folder=folder, parent=parent, name=name, pending=True, created_by=maker
    ).first()
    if existing is not None:
        return existing
    album = create_album(name=name, folder=folder, parent=parent, created_by=maker, pending=True)
    Album.objects.filter(pk=album.pk).update(created_at=created_at)
    return album


def _upload(
    album: Album,
    uploader: Resident,
    count: int,
    added_at: datetime,
    *,
    approved: bool = False,
    rejected_by: Resident | None = None,
) -> int:
    """`count` of `uploader`'s pictures in `album`, pending unless `approved`, a minute apart from
    `added_at`; `rejected_by` puts them in the bin a day later. Returns how many were new."""
    made = 0
    for index in range(count):
        filename = f"{slugify(album.name)}-{slugify(uploader.first_name)}-{index + 1}.jpg"
        if album.media.filter(title=filename).exists():
            continue
        picture = _image(
            filename, COLOURS[(album.pk + index) % len(COLOURS)], DIMENSIONS[index % len(DIMENSIONS)]
        )
        media = upload_media(
            album=album,
            uploaded_file=ContentFile(picture, name=filename),
            resident=uploader,
            title=filename,
            approved=approved,
        )
        media.added_at = added_at + timedelta(minutes=index)
        media.save(update_fields=["added_at"])
        if rejected_by is not None:
            reject(media, rejected_by)
            Media.objects.filter(pk=media.pk).update(deleted_at=added_at + timedelta(days=1))
        made += 1
    return made


def _seed_approval_notifications(now: datetime, *, newest: list[Resident]) -> None:
    """Fotogruppen's bell — the administrators', while nobody is in it (access.approvers): the grouped
    row of the latest requests, and this morning's reminder of those that have waited a day, worded
    as notify.approval_requested and notify.approvals_waiting word them. Written directly, as
    notifications.demo does, so nothing is pushed."""
    reminded_at = timezone.localtime(now).replace(hour=10, minute=20, second=0, microsecond=0)
    if reminded_at > now:
        reminded_at -= timedelta(days=1)
    waiting = [
        request
        for request in approvals.pending_requests()
        if request.first_at <= reminded_at - approvals.REMIND_AFTER
    ]
    actors = [{"id": resident.pk, "name": resident.full_name} for resident in newest]
    url = reverse("photo_album:approvals")
    latest_at = now - timedelta(hours=1)
    rows: list[Notification] = []
    for recipient in approvers():
        rows.append(
            Notification(
                recipient=recipient,
                category=categories.PHOTO_ALBUM_APPROVALS,
                group_key=categories.PHOTO_ALBUM_APPROVALS,
                title=grouped_title(actors, APPROVALS_VERB),
                body=f"Venter på godkendelse: et nyt album, Brætspilsaften, i {NO_YEAR_FOLDER}.",
                url=url,
                actors=actors,
                created_at=latest_at,
                updated_at=latest_at,
                expires_at=now + timedelta(days=7),
            )
        )
        if waiting:
            first = waiting[0]
            who = first.uploader.full_name if first.uploader is not None else "en tidligere beboer"
            urgent = first.expires_at - reminded_at <= approvals.URGENT_BEFORE
            rows.append(
                Notification(
                    recipient=recipient,
                    category=categories.PHOTO_ALBUM_APPROVALS,
                    importance=Importance.HOEJ if urgent else Importance.NORMAL,
                    title=f"{len(waiting)} upload{'s' if len(waiting) != 1 else ''} venter på godkendelse",
                    body=(
                        f"Den ældste, fra {who} til {first.album.name}, venter fra "
                        f"{date_format(timezone.localtime(first.first_at), 'j. F')} og slettes "
                        f"{date_format(timezone.localtime(first.expires_at), 'j. F')}, hvis den ikke er "
                        "godkendt."
                    ),
                    url=url,
                    created_at=reminded_at,
                    updated_at=reminded_at,
                    expires_at=reminded_at + timedelta(days=1),
                )
            )
    # Ignoring conflicts: a re-seed that adds a case finds the grouped row there already.
    Notification.objects.bulk_create(rows, ignore_conflicts=True)


def _tag(media: Media, names: list[str]) -> None:
    for name in names:
        add_tag(media, name)


def _image(title: str, colour: str, dimensions: tuple[int, int]) -> bytes:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return title.encode()
    image = Image.new("RGB", dimensions, colour)
    ImageDraw.Draw(image).text((48, 48), title, fill="white")
    output = BytesIO()
    image.save(output, format="JPEG", quality=80)
    return output.getvalue()
