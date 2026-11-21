"""The mail page (Administration → Mails): the outbox every mail goes through (core.mail) — what
waits, what went out, what did not and why. Mounted with the other site-admin pages (residents.urls_admin).

Only what a mail is, never what it says: a queued password reset carries a working login link, and an
alumneregning someone's statement, so neither body nor attachment is shown, here or anywhere else.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from residents.models import Role
from residents.permissions import effective_roles, role_required

from . import mail as outbox
from .models import OutgoingMail

Status = OutgoingMail.Status
Kind = OutgoingMail.Kind

MAIL_PAGE_SIZE = 25
# A status's name on the page and its chip, in the order the filter lists them.
MAIL_STATES = {
    Status.QUEUED: ("I kø", "chip-quiet"),
    Status.SENDING: ("Sendes", "chip-brass"),
    Status.SENT: ("Sendt", "chip"),
    Status.FAILED: ("Fejlet", "chip-neg"),
    Status.EXPIRED: ("Udløbet", "chip-neg"),
    Status.UNKNOWN: ("Ukendt", "chip-brass"),
}
# Every way a mail can end without having gone out: one filter for them all, behind the "Ikke sendt" tile.
NOT_SENT = "not_sent"
NOT_SENT_STATES = (Status.FAILED, Status.EXPIRED, Status.UNKNOWN)
MAIL_PERIODS = {
    "": ("Alt gemt", None),
    "24h": ("Seneste 24 timer", datetime.timedelta(hours=24)),
    "7d": ("Seneste 7 dage", datetime.timedelta(days=7)),
    "30d": ("Seneste 30 dage", datetime.timedelta(days=30)),
}
# Mails waiting again after a failed try, listed at the top of the page.
RETRYING_SHOWN = 10


@dataclass(frozen=True)
class MailFilters:
    status: str = ""
    kind: str = ""
    period: str = ""
    query: str = ""

    @classmethod
    def from_query(cls, query: QueryDict) -> MailFilters:
        status = query.get("status", "")
        kind = query.get("kind", "")
        period = query.get("period", "")
        return cls(
            status=status if status in Status.values or status == NOT_SENT else "",
            kind=kind if kind in Kind.values else "",
            period=period if period in MAIL_PERIODS else "",
            query=query.get("q", "").strip()[:200],
        )

    def apply(
        self, mails: QuerySet[OutgoingMail], now: datetime.datetime, *, status: bool = True
    ) -> QuerySet[OutgoingMail]:
        """The mails these filters select — leaving out the status filter for counting by status."""
        if self.kind:
            mails = mails.filter(kind=self.kind)
        since = MAIL_PERIODS[self.period][1]
        if since is not None:
            mails = mails.filter(created_at__gte=now - since)
        if self.query:
            mails = mails.filter(Q(recipients__icontains=self.query) | Q(subject__icontains=self.query))
        if status and self.status == NOT_SENT:
            mails = mails.filter(status__in=NOT_SENT_STATES)
        elif status and self.status:
            mails = mails.filter(status=self.status)
        return mails


@dataclass(frozen=True)
class MailRow:
    mail: OutgoingMail
    state_label: str
    state_class: str
    kind_label: str


def _row(mail: OutgoingMail) -> MailRow:
    label, chip = MAIL_STATES.get(mail.status, (mail.status, "chip-quiet"))  # type: ignore[call-overload]
    return MailRow(mail, label, chip, mail.get_kind_display())


def _live_context(now: datetime.datetime) -> dict[str, Any]:
    waiting = OutgoingMail.objects.filter(status__in=[Status.QUEUED, Status.SENDING])
    day = now - datetime.timedelta(hours=24)
    held, held_for = outbox.bulk_held_back(now)
    retrying = waiting.filter(status=Status.QUEUED).exclude(error="").order_by("next_attempt_at")
    return {
        "now": now,
        "waiting": waiting.count(),
        "oldest_waiting": waiting.order_by("created_at").values_list("created_at", flat=True).first(),
        "window_used": outbox.rate_window(now),
        "rate_limit": settings.MAIL_RATE_LIMIT,
        "rate_window_minutes": int(settings.MAIL_RATE_WINDOW.total_seconds() // 60),
        "bulk_held": held,
        "bulk_held_for": now + held_for if held_for is not None else None,
        "sent_today": OutgoingMail.objects.filter(sent_at__gte=day).count(),
        "not_sent_today": OutgoingMail.objects.filter(
            status__in=NOT_SENT_STATES, created_at__gte=day
        ).count(),
        "retrying": [_row(mail) for mail in retrying[:RETRYING_SHOWN]],
        "retrying_total": retrying.count(),
    }


@role_required("administrator")
def mails(request: HttpRequest) -> HttpResponse:
    now = timezone.now()
    filters = MailFilters.from_query(request.GET)
    counts = dict(
        filters.apply(OutgoingMail.objects.all(), now, status=False)
        .values_list("status")
        .annotate(n=Count("id"))
        .values_list("status", "n")
    )
    listed = filters.apply(OutgoingMail.objects.all(), now).order_by("-created_at", "-pk")
    page = Paginator(listed, MAIL_PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "siteadmin/mails.html",
        {
            **_live_context(now),
            "filters": filters,
            "page": page,
            "rows": [_row(mail) for mail in page.object_list],
            "total": sum(counts.values()),
            "not_sent_count": sum(counts.get(status, 0) for status in NOT_SENT_STATES),
            "status_counts": [
                (status, label, counts.get(status, 0))
                for status, (label, _chip) in MAIL_STATES.items()
                if counts.get(status) or status == filters.status
            ],
            "kinds": Kind.choices,
            "periods": [(key, label) for key, (label, _since) in MAIL_PERIODS.items()],
            "not_sent": NOT_SENT,
        },
    )


def mails_live(request: HttpRequest) -> HttpResponse:
    """The top of the mail page, for its own poll. Not decorated, for worker_jobs_live's reason: a 204
    leaves the page as it is, where role_required would swap a login page or a 403 into it."""
    if not request.user.is_authenticated or Role.ADMINISTRATOR not in effective_roles(request):
        return HttpResponse(status=204)
    return render(request, "siteadmin/_mails_live.html", _live_context(timezone.now()))


@role_required("administrator")
def mail_detail(request: HttpRequest, pk: int) -> HttpResponse:
    mail = get_object_or_404(OutgoingMail, pk=pk)
    now = timezone.now()
    return render(
        request,
        "siteadmin/mail_detail.html",
        {
            "row": _row(mail),
            "mail": mail,
            "now": now,
            "can_send_again": outbox.can_send_again(mail, now),
            "has_content": bool(mail.payload),
        },
    )


@role_required("administrator")
def mail_send_again(request: HttpRequest, pk: int) -> HttpResponse:
    """Send a failed or cut-off mail again, as it was (core.mail.send_again), once its cause is fixed."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    mail = get_object_or_404(OutgoingMail, pk=pk)
    if outbox.send_again([mail]):
        messages.success(request, f"Mailen til {mail.recipients} er sat i kø igen.")
    else:
        messages.error(
            request, "Mailen kan ikke sendes igen: den er sendt, eller dens indhold og levetid er udløbet."
        )
    return redirect("siteadmin:mail_detail", pk=mail.pk)


def not_sent_since(since: datetime.datetime) -> int:
    """Mails queued since `since` that did not go out — for the administration front page."""
    return OutgoingMail.objects.filter(status__in=NOT_SENT_STATES, created_at__gte=since).count()
