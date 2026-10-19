"""Monthly AK assessment: charge every resident on last month's alumneliste the configured
krydser for that month. Run on the night of the 1st, once the month is over, so the list it reads is
the month's final one. The amount and whether a month is charged is set by AK officers per calendar
month via the AK schedule (`AkMonthlyCharge`, same amount every year); this command applies it through
the same idempotent service. If that calendar month has no schedule row yet, a default (active,
2 krydser) one is created — the historical −2 behaviour. Idempotent."""

from django.core.management.base import BaseCommand

from ak.models import AkMonthlyCharge
from ak.services import charge_period, charge_previous_month


class Command(BaseCommand):
    help = "Apply the AK monthly kryds deduction for the month that just ended (idempotent)."

    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        AkMonthlyCharge.objects.get_or_create(
            month=charge_period()[1], defaults={"active": True, "krydser": 2}
        )
        year, month, written, removed = charge_previous_month()
        self.stdout.write(
            self.style.SUCCESS(
                f"AK monthly assessment {year}-{month:02d}: {written} charged, {removed} removed."
            )
        )
