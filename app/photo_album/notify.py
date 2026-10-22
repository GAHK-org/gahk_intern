"""What the photo album tells people, through the notifications app. Wording and audiences only."""

from django.conf import settings
from django.urls import reverse

from notifications import categories
from notifications.services import notify

from .models import AlbumImport

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
