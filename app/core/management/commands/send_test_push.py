"""Send a test push notification to one resident, reporting exactly what happened.

Diagnostic counterpart to the normal flow. `core.push._dispatch` deliberately swallows per-device
failures so one dead phone cannot cost the dorm its notification — which is right in production and
useless when you are trying to find out why nothing arrives. This command does the opposite: it
prints the configuration state, every endpoint it tries, and the full push-service response.

    manage.py send_test_push anton@gahk.dk

Lives in core, with the transport it exercises: it tests the keys, the subscription and the service
worker, none of which belong to a single feature. It ignores the resident's notification settings on
purpose — it tests the device, and "I turned it off for this category" is not what is being asked.

It separates the two halves of "no notification": if this delivers, the keys, the subscription and
the service worker are all fine and the problem is in the posting flow; if it fails, the reason is
on screen instead of in a swallowed log line.
"""

import json

from django.core.management.base import BaseCommand, CommandError, CommandParser
from pywebpush import WebPushException

from core import push
from core.models import PushSubscription
from residents.models import Resident


class Command(BaseCommand):
    help = "Send a test push notification to one resident's subscribed devices."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("email", help="Email of the resident to notify.")

    def handle(self, *args: object, **opts: object) -> None:
        email = str(opts["email"])

        if not push.is_configured():
            raise CommandError(
                "VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY are not both set — push is disabled. "
                "See app/.env.example."
            )

        try:
            resident = Resident.objects.get(email__iexact=email)
        except Resident.DoesNotExist as exc:
            raise CommandError(f"No resident with email {email!r}.") from exc

        subscriptions = list(PushSubscription.objects.filter(user=resident))
        total = PushSubscription.objects.count()
        self.stdout.write(f"{resident.full_name}: {len(subscriptions)} device(s) — {total} in total.")
        if not subscriptions:
            raise CommandError(
                "This resident has no subscribed devices. Open /intern/notifikationer/indstillinger "
                "as them and press 'Slå notifikationer til' first."
            )

        # Deep-links somewhere real: this is a delivery test, and a notification that opens nothing
        # when tapped only half-tests it.
        payload = push._payload(
            "Test fra GAHK", "Hvis du kan se denne, virker push.", "/intern/notifikationer/"
        )
        body = json.dumps(payload)

        failures = 0
        for subscription in subscriptions:
            # Deliberately NOT going through _dispatch: its whole job is to hide these errors.
            self.stdout.write(f"  → {subscription.endpoint[:72]}…")
            try:
                push._send(subscription, body)
            except WebPushException as exc:
                failures += 1
                status = getattr(exc.response, "status_code", None)
                detail = getattr(exc.response, "text", "") or str(exc)
                self.stdout.write(self.style.ERROR(f"    FAILED ({status}): {detail.strip()[:400]}"))
            except Exception as exc:
                failures += 1
                self.stdout.write(self.style.ERROR(f"    FAILED ({type(exc).__name__}): {exc}"))
            else:
                self.stdout.write(self.style.SUCCESS("    accepted by the push service"))

        if failures:
            raise CommandError(f"{failures} of {len(subscriptions)} device(s) failed.")
        self.stdout.write(
            self.style.SUCCESS(
                "All devices accepted. If no notification appears, the push service took it but the "
                "browser did not show it — check OS notification settings / Do Not Disturb, and that "
                "the service worker at /sw.js is the running one."
            )
        )
