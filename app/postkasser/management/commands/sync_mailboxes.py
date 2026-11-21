"""Sync every active mailbox (all its folders but drafts, trash and junk) from one.com. Idempotent — see
services.sync. A mailbox another sync or a move is working on is skipped quietly.

manage.py sync_mailboxes              # all active mailboxes
manage.py sync_mailboxes ak           # one, by slug
manage.py sync_mailboxes ak --folders # list the server's folders (to set Mailbox.sent_folder)
"""

import logging
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from postkasser import imap, services, utf7
from postkasser.models import Mailbox

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Synkronisér postkassernes indbakker fra one.com."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("slugs", nargs="*")
        parser.add_argument("--folders", action="store_true", help="Vis serverens mapper i stedet.")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        mailboxes = Mailbox.objects.filter(is_active=True)
        if options["slugs"]:
            mailboxes = mailboxes.filter(slug__in=options["slugs"])
        for mailbox in mailboxes:
            try:
                if options["folders"]:
                    with imap.connect(mailbox) as conn:
                        self.stdout.write(f"{mailbox.address}:")
                        for info in conn.list_folders():
                            marks = " ".join(sorted(info.attributes))
                            self.stdout.write(f"  {utf7.decode(info.name)}  [{info.name}] {marks}".rstrip())
                    continue
                # The only caller (with the Celery task that runs it) that may push in the foreground.
                new = services.sync(mailbox, background=False)
                self.stdout.write(f"{mailbox.address}: {new} nye")
                if mailbox.last_sync_error:
                    self.stderr.write(f"{mailbox.address}: nogle mapper fejlede: {mailbox.last_sync_error}")
            except services.SyncBusy:
                # A sync or a move is already running on it (another worker, the refresh button):
                # not an error, the next run picks it up.
                continue
            except imap.MailboxError as exc:
                # One mailbox with a changed password must not stop the others syncing.
                self.stderr.write(f"{mailbox.address}: FEJL {exc}")
            except Exception as exc:
                logger.exception("postkasser: sync of %s crashed", mailbox.address)
                self.stderr.write(f"{mailbox.address}: FEJL {exc!r}")
                mailbox.last_sync_error = f"Uventet fejl: {exc!r}"[:500]
                mailbox.save(update_fields=["last_sync_error"])
