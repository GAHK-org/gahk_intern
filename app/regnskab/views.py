"""Regnskab (accounting) settlement view (F-010 role family).

A REPORT ACROSS THREE DOMAINS, which is why it is a module of its own rather than a view in any of
them: who moved out is identity's (residents.api), the AK balance is AK's (ak.api) and the bar balance
is ølkælderen's (oelkaelder.api). It used to live in residents and query the AK ledger and the
shopper accounts directly, which made identity depend on two features. It owns no data.

The accounting embedsgruppe settles a moved-out resident's housing deposit. For that they need to see
what the resident still owes the dorm: AK labour (krydser; negative = behind) and their ølkælder
balance (kr; negative = owes the bar). AK has no monetary value defined in the system, so it is shown
as raw krydser and accounting converts it by their own rules.
"""

from typing import NamedTuple

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from ak.api import balance_krydser
from core.danish import MONTHS
from oelkaelder.api import balance_ore
from residents.api import (
    Resident,
    leavers,
    parse_period,
    period_options,
    prev_period,
    role_required,
    search_residents,
)


class Row(NamedTuple):
    resident: Resident
    room: int | None  # room number in the month they were last resident (None for a free lookup)
    ak_krydser: int  # negative = owes labour
    oek_ore: int  # negative = owes the ølkælder


def _row(resident: Resident, room: int | None) -> Row:
    return Row(resident, room, balance_krydser(resident.pk), balance_ore(resident.pk))


@role_required("regnskab")
def overview(request: HttpRequest) -> HttpResponse:
    # `period` is the month they are GONE from (default: the active period). They are a "leaver" if they
    # held a room the month before but not this month — Residency is per-month, so this is a set diff.
    year, month = parse_period(request)
    py, pm = prev_period((year, month))
    gone = sorted(
        (_row(entry.resident, entry.room_number) for entry in leavers((year, month))),
        key=lambda row: row.resident.full_name,
    )

    # Free lookup of any resident (current or former) by name/email.
    q = (request.GET.get("q") or "").strip()
    results = []
    if q:
        results = [_row(r, None) for r in search_residents(q)]

    return render(
        request,
        "regnskab/overview.html",
        {
            "periods": period_options((year, month)),
            "period_label": f"{MONTHS[month].capitalize()} {year}",
            "prior_label": f"{MONTHS[pm].capitalize()} {py}",
            "period_value": f"{year}-{month}",
            "leavers": gone,
            "q": q,
            "results": results,
        },
    )
