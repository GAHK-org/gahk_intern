from django.core.management.base import BaseCommand

from photo_album.approvals import purge_expired_albums
from photo_album.services import purge_expired


class Command(BaseCommand):
    help = (
        "Permanently remove expired pending media, media held in the bin for more than 30 days, "
        "and albums awaiting approval left empty for 30 days."
    )

    def handle(self, *args: object, **options: object) -> None:
        media = purge_expired()
        albums = purge_expired_albums()
        self.stdout.write(f"Removed {media} media item(s) and {albums} album(s) awaiting approval.")
