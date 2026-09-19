from datetime import date, datetime, time, timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Sum
from django.utils import timezone
from django.utils.formats import date_format

from .models import Shopper


def _previous_month(reference: date | None = None) -> tuple[date, date]:
    first_this_month = (reference or timezone.localdate()).replace(day=1)
    last_month = first_this_month - timedelta(days=1)
    return last_month.replace(day=1), first_this_month


def _kr(amount_ore: int) -> str:
    return f"{amount_ore / 100:.2f}".replace(".", ",")


def _month_name(month: date) -> str:
    """ "august 2026", not "August 2026".

    `f"{month:%B %Y}"` is strftime, which reads the process's C locale — and nothing sets one, so
    every Danish statement went out with an English month in its subject line and its first
    sentence. Django's own formatter uses the active locale, which is `da` via LANGUAGE_CODE.
    """
    return date_format(month, "F Y")


def _balance_at(shopper: Shopper, upper: datetime) -> int:
    """The shopper's balance as of `upper`, which is the figure a statement is supposed to close on.

    `Shopper.balance_ore` is the balance RIGHT NOW. Reporting it beside a named month meant a
    statement for August, sent on 1 September, quoted a balance that already included September's
    purchases — so the four numbers in the mail did not add up, and the one people act on was the
    wrong one. Same three ledgers as `balance_ore`, bounded by the end of the period.
    """
    deposits = shopper.deposits.filter(is_valid=True, created_at__lt=upper).aggregate(s=Sum("amount_ore"))
    spent = shopper.purchase_shares.filter(
        transaction__is_valid=True, transaction__created_at__lt=upper
    ).aggregate(s=Sum("share_ore"))
    adjustments = shopper.adjustments.filter(is_valid=True, created_at__lt=upper).aggregate(
        s=Sum("amount_ore")
    )
    return (deposits["s"] or 0) - (spent["s"] or 0) + (adjustments["s"] or 0)


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
                f"Her er din ølkælderoversigt for {_month_name(start)}.",
                f"Indbetalinger: {_kr(deposited)} kr",
                f"Køb: -{_kr(spent)} kr",
                f"Justeringer: {_kr(adjusted)} kr",
                f"Saldo ved månedens udgang: {_kr(_balance_at(shopper, upper))} kr",
            ]
        )
        sent += send_mail(
            f"Ølkælderoversigt for {_month_name(start)}",
            body,
            settings.OELKAELDER_FROM_EMAIL,
            [shopper.resident.email],
        )
    return sent
