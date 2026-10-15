"""Regnskab (accounting) settlement view.

The accounting embedsgruppe settles a moved-out resident's housing deposit. For that they need to see
what the resident still owes the dorm: AK labour (krydser; negative = behind) and their ølkælder
balance (kr; negative = owes the bar). AK has no monetary value defined in the system, so it is shown
as raw krydser and accounting converts it by their own rules.
"""

from typing import NamedTuple

from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from ak.models import AkEntry

from .models import Residency, Resident, prev_period
from .permissions import role_required
from .views import DA_MONTHS, _parse_period, _period_options


class Row(NamedTuple):
    resident: Resident
    room: int | None  # room number in the month they were last resident (None for a free lookup)
    ak_krydser: int  # negative = owes labour
    oek_ore: int  # negative = owes the ølkælder


def _row(resident: Resident, room: int | None) -> Row:
    oek_ore = sum((a.balance_ore for a in resident.shopper_accounts.all()), 0)
    return Row(resident, room, AkEntry.balance_for(resident), oek_ore)


@role_required("regnskab")
def overview(request: HttpRequest) -> HttpResponse:
    # `period` is the month they are GONE from (default: the active period). They are a "leaver" if they
    # held a room the month before but not this month — Residency is per-month, so this is a set diff.
    year, month = _parse_period(request)
    py, pm = prev_period((year, month))

    prior = Residency.objects.filter(year=py, month=pm).select_related("resident", "room")
    still_here = set(Residency.objects.filter(year=year, month=month).values_list("resident_id", flat=True))
    leavers = sorted(
        (_row(r.resident, r.room.number) for r in prior if r.resident_id not in still_here),
        key=lambda row: row.resident.full_name,
    )

    # Free lookup of any resident (current or former) by name/email.
    q = (request.GET.get("q") or "").strip()
    results = []
    if q:
        found = Resident.objects.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q)
        ).order_by("first_name", "last_name")[:50]
        results = [_row(r, None) for r in found]

    return render(
        request,
        "regnskab/overview.html",
        {
            "periods": _period_options((year, month)),
            "period_label": f"{DA_MONTHS[month].capitalize()} {year}",
            "prior_label": f"{DA_MONTHS[pm].capitalize()} {py}",
            "period_value": f"{year}-{month}",
            "leavers": leavers,
            "q": q,
            "results": results,
        },
    )
