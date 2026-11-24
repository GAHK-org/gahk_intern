"""Apply every active mailbox's time rules ("sager in <column> with no activity for N days move to <column>").
Idempotent: a moved sag counts as active again, so a second run changes nothing. A mailbox a sync or a move
is working on is skipped; the next run gets it.

manage.py apply_mail_time_rules             # all active mailboxes
manage.py apply_mail_time_rules --dry-run   # only say what would move
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from postkasser import rules


class Command(BaseCommand):
    help = "Flyt sager uden aktivitet efter postkassernes tidsregler."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Vis hvad der ville blive flyttet.")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        dry_run = options["dry_run"]
        for move in rules.apply_time_rules(dry_run=dry_run):
            verb = "ville flytte" if dry_run else "flyttede"
            target = move.rule.to_column.name if move.rule.to_column is not None else ""
            self.stdout.write(
                f"{move.thread.mailbox.address}: {verb} «{move.thread.subject}» til «{target}» "
                f"({move.rule.name})"
            )
