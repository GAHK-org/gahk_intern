"""Nudge the people who have not answered, shortly before a deadline closes.

Scheduled daily (DEPLOY.md §4b). Idempotent, as everything on that schedule must be: the claim is a
compare-and-swap on Event.reminder_sent_at, so a double run or two overlapping runs send one
reminder between them.

THE CLAIM HAPPENS BEFORE THE SEND, and the order is deliberate. A crash between the two loses one
reminder; the other order would push the whole house twice. Losing one is recoverable — somebody
opens the page — and double-notifying sixty people is not.

Delivery is INLINE (`background=False`). core.push hands the fan-out to a daemon thread by default,
which is right in a request and fatal here: handle() returns, the interpreter shuts down, and Python
kills the thread mid-send. That would deliver to however many devices it happened to reach, a
different number every night, with no error anywhere. See core.push.send_many.
"""

from typing import Any

from django.core.management.base import BaseCommand

from events import services


class Command(BaseCommand):
    help = "Send påmindelser om begivenheder hvis svarfrist nærmer sig."

    def add_arguments(self, parser: Any) -> None:  # noqa: ANN401 — ArgumentParser
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Vis hvad der ville blive sendt, uden at sende eller markere noget.",
        )

    def handle(self, *args: object, **options: object) -> None:
        dry_run = bool(options.get("dry_run"))
        due = services.events_needing_reminder()

        if not due:
            self.stdout.write("Ingen svarfrister inden for vinduet.")
            return

        for event in due:
            recipients = services.deadline_reminder_audience(event)
            if dry_run:
                self.stdout.write(
                    f"[tør] {event.title} ({event.starts_at:%d.%m}) → {len(recipients)} beboer(e)"
                )
                continue
            sent = services.send_deadline_reminder(event)
            self.stdout.write(f"{event.title}: påmindelse sendt til {sent} beboer(e).")
