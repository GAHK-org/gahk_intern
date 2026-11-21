"""Send one mail straight through the real mail backend, reporting exactly what the server said.

Diagnostic counterpart to the outbox. `send_mail` (and Django's own `sendtestemail`) only queues a mail
(core.mail), so it succeeds whatever the server thinks of it; this sends through MAIL_DELIVERY_BACKEND
there and then, past the queue, and fails with the server's answer:

    manage.py check_mail_server you@example.com
    manage.py check_mail_server you@example.com --from "Test <alias@gahk.dk>"

The usual failure is a sender the SMTP account may not use: one.com refuses any From that SMTP_USER is
not itself or an alias of, so MAIL_SENDER must be the account. No error means the server accepted the mail — not that it reached the inbox.
"""

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.core.management.base import BaseCommand, CommandError, CommandParser


class Command(BaseCommand):
    help = "Send a test mail through the real mail backend, past the outbox, and report the server's answer."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("recipient", help="Where to send the test mail.")
        parser.add_argument(
            "--from",
            dest="from_email",
            default=settings.DEFAULT_FROM_EMAIL,
            help="Sender (DEFAULT_FROM_EMAIL, which is MAIL_SENDER).",
        )

    def handle(self, *args: object, **opts: object) -> None:
        recipient, from_email = str(opts["recipient"]), str(opts["from_email"])
        message = EmailMessage(
            "Testmail fra GAHK Intern",
            f"Sendt med manage.py check_mail_server fra {from_email}.",
            from_email,
            [recipient],
            connection=get_connection(settings.MAIL_DELIVERY_BACKEND, fail_silently=False),
        )
        try:
            message.send()
        except Exception as exc:
            raise CommandError(f"Not sent through {settings.MAIL_DELIVERY_BACKEND}: {exc!r}") from exc
        self.stdout.write(
            self.style.SUCCESS(f"Accepted by {settings.MAIL_DELIVERY_BACKEND}: {from_email} → {recipient}")
        )
