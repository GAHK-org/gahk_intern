from datetime import date, datetime, time, timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Sum
from django.utils import timezone

from .models import Shopper


def _previous_month(reference: date | None = None) -> tuple[date, date]:
    first_this_month = (reference or timezone.localdate()).replace(day=1)
    last_month = first_this_month - timedelta(days=1)
    return last_month.replace(day=1), first_this_month


def _kr(amount_ore: int) -> str:
    return f"{amount_ore / 100:.2f}".replace(".", ",")


@shared_task
def send_monthly_statements() -> int:
    """Email every active ølkælder account holder their prior calendar-month ledger summary."""
    start, end = _previous_month()
    lower = timezone.make_aware(datetime.combine(start, time.min))
    upper = timezone.make_aware(datetime.combine(end, time.min))
    sent = 0
    for shopper in Shopper.objects.filter(active=True, resident__is_active=True).select_related("resident"):
        deposits = shopper.deposits.filter(is_valid=True, created_at__gte=lower, created_at__lt=upper)
        purchases = shopper.purchase_shares.filter(
            transaction__is_valid=True, transaction__created_at__gte=lower, transaction__created_at__lt=upper
        )
        adjustments = shopper.adjustments.filter(is_valid=True, created_at__gte=lower, created_at__lt=upper)
        deposited = deposits.aggregate(total=Sum("amount_ore"))["total"] or 0
        spent = purchases.aggregate(total=Sum("share_ore"))["total"] or 0
        adjusted = adjustments.aggregate(total=Sum("amount_ore"))["total"] or 0
        body = "\n".join(
            [
                f"Hej {shopper.resident.first_name},",
                "",
                f"Her er din ølkælderoversigt for {start:%B %Y}.",
                f"Indbetalinger: {_kr(deposited)} kr",
                f"Køb: -{_kr(spent)} kr",
                f"Justeringer: {_kr(adjusted)} kr",
                f"Saldo: {_kr(shopper.balance_ore)} kr",
            ]
        )
        sent += send_mail(
            f"Ølkælderoversigt for {start:%B %Y}",
            body,
            settings.OELKAELDER_FROM_EMAIL,
            [shopper.resident.email],
        )
    return sent
