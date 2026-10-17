"""Monthly AK assessment (F-009): charge every current resident the configured krydser for the active
month. The amount and whether a month is charged is set by AK officers per calendar month via the AK
schedule (`AkMonthlyCharge`, same amount every year); this command just applies the active period
through the same idempotent service. If that calendar month has no schedule row yet, a default (active,
2 krydser) one is created — the historical −2 behaviour. Idempotent; schedule monthly if desired."""

from django.core.management.base import BaseCommand

from ak.models import AkMonthlyCharge
from ak.services import apply_monthly_charge
from residents.api import active_period


class Command(BaseCommand):
    help = "Apply the AK monthly kryds deduction for the active month (idempotent)."

    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        year, month = active_period()
        AkMonthlyCharge.objects.get_or_create(month=month, defaults={"active": True, "krydser": 2})
        written, removed = apply_monthly_charge(year, month)
        self.stdout.write(
            self.style.SUCCESS(
                f"AK monthly assessment {year}-{month:02d}: {written} charged, {removed} removed."
            )
        )
