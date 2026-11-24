"""Delete the kladder (half-written mails, intranet only) that nobody has touched for 30 days.
Saving a draft counts as touching it, so one that is still being worked on never goes.

manage.py purge_mail_drafts             # delete them
manage.py purge_mail_drafts --dry-run   # only say how many would go
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from postkasser.models import DRAFT_TTL, MailDraft


class Command(BaseCommand):
    help = "Slet kladder der ikke er rørt i 30 dage."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Vis hvor mange der ville blive slettet.")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        old = MailDraft.objects.filter(updated_at__lt=timezone.now() - DRAFT_TTL)
        if options["dry_run"]:
            self.stdout.write(f"Ville slette {old.count()} kladde(r).")
            return
        deleted, _ = old.delete()  # nothing cascades from a draft, so this is the number of drafts
        self.stdout.write(f"Slettede {deleted} kladde(r).")
