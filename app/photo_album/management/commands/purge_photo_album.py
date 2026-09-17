from django.core.management.base import BaseCommand

from photo_album.services import purge_expired


class Command(BaseCommand):
    help = "Permanently remove expired pending media and media held in the bin for more than 30 days."

    def handle(self, *args: object, **options: object) -> None:
        self.stdout.write(f"Removed {purge_expired()} media item(s).")
