"""AK views (F-009). Members see their own balance/log; AK officers (role `ak`) see everyone and can
add/adjust crosses for anyone. Balance is the SUM of ledger entries."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from residents.models import Resident, active_period
from residents.permissions import current_resident, role_required
from residents.views import DA_MONTHS

from .models import AkEntry, AkMonthlyCharge
from .services import apply_monthly_charge, ensure_active_month_applied


@login_required
def my_ak(request: HttpRequest) -> HttpResponse:
    ensure_active_month_applied()  # backstop for the monthly cron job (DEPLOY.md §4b)
    user = current_resident(request)
    return render(
        request,
        "ak/my.html",
        {
            "balance": AkEntry.balance_for(user),
            "entries": user.ak_entries.all()[:100],
        },
    )


@require_POST
@login_required
def add_self_entry(request: HttpRequest) -> HttpResponseRedirect:
    """A resident logs their own AK labour: a positive number of krydser + a required description
    (mirrors the legacy 'addtolog' on one's own log). AK officers review/adjust via the overview."""
    resident = current_resident(request)
    reason = (request.POST.get("reason") or "").strip()
    try:
        krydser = int(request.POST.get("krydser", "0"))
    except ValueError:
        krydser = 0
    if krydser < 1:
        messages.error(request, "Antal krydser skal være et positivt heltal.")
    elif not reason:
        messages.error(request, "Skriv en beskrivelse af det udførte arbejde.")
    else:
        AkEntry.objects.create(
            resident=resident,
            delta=krydser,
            kind=AkEntry.Kind.LABOUR,
            reason=reason,
            created_by=resident,
            created_at=timezone.now(),
        )
        messages.success(request, f"{krydser} kryds registreret.")
    return redirect("ak:index")


def _schedule_rows(active_month: int) -> list[dict]:
    """The 12 calendar-month schedule rows (januar…december) for the template. Any missing row defaults
    to on at 2 (the migration seeds all 12, so this is just belt-and-braces)."""
    configs = {c.month: c for c in AkMonthlyCharge.objects.all()}
    rows = []
    for m in range(1, 13):
        cfg = configs.get(m)
        rows.append(
            {
                "month": m,
                "label": DA_MONTHS[m].capitalize(),
                "active": cfg.active if cfg else True,
                "krydser": cfg.krydser if cfg else 2,
                "is_active_month": m == active_month,
            }
        )
    return rows


@role_required("ak")
def overview(request: HttpRequest) -> HttpResponse:
    ensure_active_month_applied()  # backstop for the monthly cron job (DEPLOY.md §4b)
    year, month = active_period()
    residents = Resident.objects.filter(residencies__year=year, residencies__month=month).distinct()
    balances = dict(
        AkEntry.objects.values_list("resident").annotate(b=Sum("delta")).values_list("resident", "b")
    )
    rows = sorted(((r, balances.get(r.id, 0)) for r in residents), key=lambda t: t[1])
    return render(
        request,
        "ak/overview.html",
        {
            "rows": rows,
            "period": f"{year}-{month:02d}",
            "schedule": _schedule_rows(month),
            "active_month_label": DA_MONTHS[month].capitalize(),
        },
    )


@require_POST
@role_required("ak")
def save_monthly_charges(request: HttpRequest) -> HttpResponseRedirect:
    """Persist the per-calendar-month schedule (same amount every year) and re-book the *active* month's
    deduction now. Only the active period is (re)applied — historical months are never touched, so
    already-settled balances are safe."""
    officer = current_resident(request)
    for m in range(1, 13):
        active = request.POST.get(f"active_{m}") == "1"
        try:
            krydser = int(request.POST.get(f"krydser_{m}", "2"))
        except ValueError:
            krydser = 2
        krydser = max(1, krydser)  # a charged month deducts at least 1
        AkMonthlyCharge.objects.update_or_create(
            month=m,
            defaults={"active": active, "krydser": krydser, "updated_by": officer},
        )
    year, month = active_period()
    written, removed = apply_monthly_charge(year, month, officer=officer)
    messages.success(
        request,
        f"Skema gemt. Denne måneds afskrivning: {written} bogført, {removed} fjernet.",
    )
    return redirect("ak:overview")


@role_required("ak")
def resident_log(request: HttpRequest, pk: int) -> HttpResponse:
    resident = get_object_or_404(Resident, pk=pk)
    return render(
        request,
        "ak/log.html",
        {
            "resident": resident,
            "balance": AkEntry.balance_for(resident),
            "entries": resident.ak_entries.all()[:200],
        },
    )


@require_POST
@role_required("ak")
def add_entry(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    resident = get_object_or_404(Resident, pk=pk)
    try:
        delta = int(request.POST.get("delta", "0"))
    except ValueError:
        delta = 0
    if not delta:
        # Said out loud, like add_self_entry: a silently ignored "2 krydser" (or "+2,5") reads as
        # saved to the officer, who then has no idea the resident was never credited.
        messages.error(request, "Ændringen skal være et helt tal forskelligt fra 0, fx +2 eller -1.")
    else:
        AkEntry.objects.create(
            resident=resident,
            delta=delta,
            kind=AkEntry.Kind.LABOUR if delta > 0 else AkEntry.Kind.ADJUSTMENT,
            reason=request.POST.get("reason", "").strip(),
            created_by=current_resident(request),
            created_at=timezone.now(),
        )
        messages.success(request, f"{delta:+d} kryds registreret.")
    return redirect("ak:log", pk=pk)


@require_POST
@role_required("ak")
def delete_entry(request: HttpRequest, pk: int, entry_id: int) -> HttpResponseRedirect:
    AkEntry.objects.filter(id=entry_id, resident_id=pk).delete()
    return redirect("ak:log", pk=pk)
