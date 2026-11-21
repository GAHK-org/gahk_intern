"""What the photo album tells people, through the notifications app. Wording and audiences only."""

from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from notifications import categories
from notifications.categories import Importance
from notifications.services import notify, notify_grouped
from residents.models import Resident

from . import access
from .models import Album, AlbumImport

if TYPE_CHECKING:
    from .approvals import Decision, Request

# Why an import failed, as its notification says it. Only the start of it fits; the import page,
# which the notification links to, lists every reason.
REFUSED = "ZIP-filen blev afvist, så intet er importeret: {first}"
REFUSED_MORE = " Og {count} andre ting — se dem alle på importsiden."
INTERRUPTED = "Importen blev afbrudt undervejs, så intet er importeret. Prøv igen."
NEVER_STARTED = (
    "Importen er ikke kommet i gang efter 12 timer og er opgivet, så intet er importeret. "
    "Kommer den alligevel i gang, bliver den gennemført."
)
CRASHED = (
    "Der skete en fejl på serveren, så intet er importeret. "
    "Prøv igen, og kontakt en administrator, hvis det sker igen."
)


def refused(errors: list[str]) -> str:
    return REFUSED.format(first=errors[0]) + (
        REFUSED_MORE.format(count=len(errors) - 1) if len(errors) > 1 else ""
    )


def import_failed(album_import: AlbumImport, reason: str) -> None:
    """Tell whoever started an import that it failed: they may have closed the page that was waiting
    for it. Not the import script's System resident, whom nobody reads the notifications of."""
    resident = album_import.requested_by
    if resident.email == settings.PHOTO_ALBUM_SYSTEM_IMPORT_EMAIL:
        return
    notify(
        [resident.pk],
        category=categories.SYSTEM,
        title=f"Importen af {album_import.archive_name} mislykkedes",
        body=reason,
        url=f"{reverse('photo_album:import_zip')}?job={album_import.token}",
    )


# --- Approvals (photo_album.approvals) -----------------------------------------------------------


def _approvals_url() -> str:
    return reverse("photo_album:approvals")


def _album_url(album: Album) -> str:
    return reverse("photo_album:detail", args=[album.pk])


def approval_requested(uploader: Resident, album: Album) -> None:
    """A request has started: a new album, or a first upload awaiting approval to an album tree.

    `album` is the one made or uploaded to. One row per member of Fotogruppen, which the next
    request folds into ("Anders og Bente har uploadet …"), so a day of requests is one notification
    each rather than one per request.
    """
    if album.pending and album.parent_id is None:
        where = f"et nyt album, {album.name}, i {album.folder}"
    elif album.pending:
        where = f"et nyt underalbum, {album.name}, i {album.parent.name} ({album.folder})"  # type: ignore[union-attr]
    else:
        where = f"{' › '.join(album.display_path())} i {album.folder}"
    for recipient in access.approvers().values_list("pk", flat=True):
        notify_grouped(
            recipient,
            category=categories.PHOTO_ALBUM_APPROVALS,
            group_key=categories.PHOTO_ALBUM_APPROVALS,
            actor=uploader,
            verb="har uploadet til fotoalbummet",
            body=f"Venter på godkendelse: {where}.",
            url=_approvals_url(),
        )


def approvals_waiting(requests: "list[Request]", *, urgent: bool) -> None:
    """The daily reminder of what still waits, oldest first. It lasts until the next one."""
    first = requests[0]
    count = len(requests)
    who = first.uploader.full_name if first.uploader is not None else "en tidligere beboer"
    notify(
        access.approvers(),
        category=categories.PHOTO_ALBUM_APPROVALS,
        title=f"{count} upload{'s' if count != 1 else ''} venter på godkendelse",
        body=(
            f"Den ældste, fra {who} til {first.album.name}, venter fra "
            f"{date_format(timezone.localtime(first.first_at), 'j. F')} og slettes "
            f"{date_format(timezone.localtime(first.expires_at), 'j. F')}, hvis den ikke er godkendt."
        ),
        url=_approvals_url(),
        importance=Importance.HOEJ if urgent else None,
        expires_at=timezone.now() + timedelta(days=1),
    )


def approval_decided(
    uploader: Resident, request: "Request", decision: "Decision", *, note: str, rejected: bool = False
) -> None:
    """Tell the uploader how their request was decided, with the approver's note if there is one."""
    root = decision.root
    root_name = root.name if root is not None else request.album.name
    url = _album_url(root) if root is not None else reverse("photo_album:index")
    body: list[str] = []
    tops = request.tops
    if rejected:
        if len(tops) == 1:
            title = f"Dit album {tops[0].name} blev afvist"
        elif tops:
            title = f"Dine nye albummer i {root_name} blev afvist"
        else:
            title = f"Dine uploads til {root_name} blev afvist"
        if decision.rejected:
            body.append("Fotogruppen kan fortryde det i 30 dage.")
    else:
        for old_name, target, _ in decision.merges:
            body.append(f"Det fra {old_name} lægges i albummet {target.name} i {target.folder}.")
        for old_name, old_folder, album in decision.kept:
            if album.name != old_name or album.folder != old_folder:
                where = f" i {album.folder}" if album.parent_id is None else ""
                body.append(f"{old_name} hedder nu {album.name}{where}.")
        if len(decision.merges) == 1 and not decision.kept:
            old_name, target, _ = decision.merges[0]
            title = f"Dine uploads til {old_name} er godkendt"
            url = _album_url(target)
        elif len(decision.kept) == 1 and not decision.merges:
            title = f"Dit album {decision.kept[0][2].name} er godkendt"
            url = _album_url(decision.kept[0][2])
        elif decision.kept or decision.merges:
            title = f"Dine nye albummer og uploads til {root_name} er godkendt"
        elif decision.rejected:
            title = f"{decision.approved} af dine uploads til {root_name} er godkendt"
        else:
            title = f"Dine uploads til {root_name} er godkendt"
        if decision.rejected:
            body.append(f"{decision.rejected} blev afvist.")
    if note:
        body.append(f"Fotogruppen skriver: {note}")
    notify([uploader.pk], category=categories.GENERAL, title=title, body=" ".join(body), url=url)
