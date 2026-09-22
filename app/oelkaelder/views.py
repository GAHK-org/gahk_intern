"""Ølkælder views (F-003). Kiosk (LAN-IP gated, no login) for the till; member balance view;
ølkælder-admin screens for deposits/balances."""

import json
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import TypedDict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Prefetch, Q, QuerySet, Sum, Value
from django.db.models.functions import Concat
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.exports import csv_or_xlsx_response
from residents.models import Resident
from residents.permissions import current_resident, role_required

from .forms import InterestPolicyForm, PurchasePolicyForm, WarningForm
from .models import (
    Adjustment,
    Deposit,
    InterestPolicy,
    Product,
    PurchasePolicy,
    PurchaseShare,
    Shopper,
    Transaction,
    TransactionItem,
    Warning,
)
from .services import (
    apply_interest,
    bulk_balances,
    record_adjustment,
    record_deposit,
    record_purchase,
    void_adjustment,
    void_purchase,
)


class _Entry(TypedDict):
    created_at: datetime
    text: str
    amount_ore: int


def _client_ip(request: HttpRequest) -> str:
    """The till's real IP. Behind Coolify/Traefik, REMOTE_ADDR is the proxy, so trust the last hop of
    X-Forwarded-For (the IP Traefik observed — the rightmost entry is the one it appended, not a value
    a client could spoof). Safe only because the app server is reachable *only* via the proxy."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")


def _is_kiosk(request: HttpRequest) -> bool:
    return settings.DEBUG or _client_ip(request) in settings.OELKAELDER_KIOSK_IPS


def shop(request: HttpRequest) -> HttpResponse:
    if not _is_kiosk(request):
        raise PermissionDenied("Tillen er kun tilgængelig fra kollegiets netværk.")
    products = Product.objects.filter(active=True).order_by("-highlighted", "name")
    shoppers = list(
        Shopper.objects.filter(active=True).select_related("resident").order_by("resident__first_name")
    )
    # Balances in three queries rather than three per shopper per read — the template shows each
    # balance and also needs to know whether the shopper is over the credit limit.
    policy = PurchasePolicy.get()
    balances = bulk_balances(shoppers)
    rows = [(s, balances[s.pk], policy.blocks(balances[s.pk])) for s in shoppers]
    # Tiles are server-rendered (the till's iPad runs iOS 10.3 and cannot use the Alpine/Tailwind
    # bundle); the template's inline ES5 only enhances them. See app/templates/oelkaelder/shop.html.
    return render(
        request,
        "oelkaelder/shop.html",
        {
            "products": products,
            "shopper_rows": rows,
            "block_limit_kr": policy.block_below_ore // 100,
            "block_below_ore": policy.block_below_ore,
        },
    )


@require_POST
def purchase(request: HttpRequest) -> HttpResponseRedirect:
    if not _is_kiosk(request):
        raise PermissionDenied
    shopper_ids = [int(x) for x in request.POST.getlist("shopper")]
    try:
        lines = json.loads(request.POST.get("basket", "[]"))
        if not isinstance(lines, list):
            raise ValueError("Ugyldig kurv.")
        txn = record_purchase(shopper_ids, lines)
        messages.success(request, f"Køb registreret (#{txn.id}).")
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, Product.DoesNotExist) as e:
        messages.error(request, str(e) or "Ugyldigt køb.")
    return redirect("oelkaelder:shop")


def _account_entries(accounts: list[Shopper]) -> list[_Entry]:
    """Combined account statement for one or more shopper accounts: purchase shares as debits,
    deposits and adjustments as credits, newest first. Shared by the resident's own `min-saldo` page
    and the ØK per-person history so the two can never disagree about what someone owes."""
    shares = (
        PurchaseShare.objects.filter(shopper__in=accounts, transaction__is_valid=True)
        .select_related("transaction")
        .prefetch_related("transaction__items__product")
    )
    deposits = Deposit.objects.filter(shopper__in=accounts, is_valid=True)
    adjustments = Adjustment.objects.filter(shopper__in=accounts, is_valid=True)
    entries: list[_Entry] = [
        _Entry(
            created_at=s.transaction.created_at,
            text=", ".join(f"{i.quantity}× {i.product.name}" for i in s.transaction.items.all()) or "Køb",
            amount_ore=-s.share_ore,  # debit
        )
        for s in shares
    ] + [
        _Entry(created_at=d.created_at, text="Indbetaling", amount_ore=d.amount_ore)  # credit
        for d in deposits
    ]
    entries += [
        _Entry(created_at=a.created_at, text=a.reason or a.get_kind_display(), amount_ore=a.amount_ore)
        for a in adjustments
    ]
    entries.sort(key=lambda e: e["created_at"], reverse=True)
    return entries


@login_required
def my_balance(request: HttpRequest) -> HttpResponse:
    """Balance + a combined account statement (deposits as credits, purchase shares as debits)."""
    accounts = list(current_resident(request).shopper_accounts.all())
    return render(
        request,
        "oelkaelder/my.html",
        {
            "accounts": [(a, a.balance_ore) for a in accounts],
            "entries": _account_entries(accounts)[:100],
            "bank_reg": settings.OELKAELDER_BANK_REG,
            "bank_account": settings.OELKAELDER_BANK_ACCOUNT,
        },
    )


@role_required("oelkaelder")
def admin(request: HttpRequest) -> HttpResponse:
    active = Shopper.objects.filter(active=True).select_related("resident")
    rows = sorted(((s, s.balance_ore) for s in active), key=lambda t: t[1])  # debtors first
    inactive = [
        (s, s.balance_ore)
        for s in Shopper.objects.filter(active=False)
        .select_related("resident")
        .order_by("resident__first_name")
    ]
    residents_without_account = Resident.objects.filter(shopper_accounts__isnull=True).order_by(
        "first_name", "last_name"
    )
    deposits = (
        Deposit.objects.filter(is_valid=True).select_related("shopper__resident").order_by("-created_at")[:15]
    )
    warning_forms = [(WarningForm(instance=Warning.objects.filter(pk=num).first()), num) for num in (1, 2)]
    report_links = [
        ("Indbetalingsrapport", "oelkaelder:report_deposits"),
        ("Salgsrapport", "oelkaelder:report_sales"),
        ("Antal", "oelkaelder:report_quantity"),
    ]
    return render(
        request,
        "oelkaelder/admin.html",
        {
            "rows": rows,
            "inactive": inactive,
            "residents_without_account": residents_without_account,
            "deposits": deposits,
            "warning_forms": warning_forms,
            "interest_form": InterestPolicyForm(instance=InterestPolicy.get()),
            "purchase_policy_form": PurchasePolicyForm(instance=PurchasePolicy.get()),
            "report_links": report_links,
        },
    )


@require_POST
@role_required("oelkaelder")
def deactivate_shopper(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    Shopper.objects.filter(pk=pk).update(active=False)
    messages.success(request, "Alumne deaktiveret.")
    return redirect("oelkaelder:admin")


@require_POST
@role_required("oelkaelder")
def activate_shopper(request: HttpRequest) -> HttpResponseRedirect:
    shopper = get_object_or_404(Shopper, pk=request.POST.get("shopper") or 0)
    shopper.active = True
    shopper.save(update_fields=["active"])
    messages.success(request, f"{shopper.resident.full_name} genaktiveret.")
    return redirect("oelkaelder:admin")


@require_POST
@role_required("oelkaelder")
def add_shopper(request: HttpRequest) -> HttpResponseRedirect:
    resident = get_object_or_404(Resident, pk=request.POST.get("resident") or 0)
    _, created = Shopper.objects.get_or_create(resident=resident, defaults={"active": True})
    if created:
        messages.success(request, f"{resident.full_name} tilføjet som alumne.")
    else:
        messages.error(request, f"{resident.full_name} har allerede en konto.")
    return redirect("oelkaelder:admin")


@require_POST
@role_required("oelkaelder")
def update_warning(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    warning = get_object_or_404(Warning, pk=pk)
    form = WarningForm(request.POST, instance=warning)
    if form.is_valid():
        form.save()
        messages.success(request, f"Advarselsmail {pk} gemt.")
    else:
        messages.error(request, "Kunne ikke gemme advarselsmailen.")
    return redirect("oelkaelder:admin")


@require_POST
@role_required("oelkaelder")
def update_interest(request: HttpRequest) -> HttpResponseRedirect:
    form = InterestPolicyForm(request.POST, instance=InterestPolicy.get())
    if form.is_valid():
        form.save()
        messages.success(request, "Renteindstillinger gemt.")
    else:
        messages.error(request, "Kunne ikke gemme renteindstillingerne.")
    return redirect("oelkaelder:admin")


@require_POST
@role_required("oelkaelder")
def update_purchase_policy(request: HttpRequest) -> HttpResponseRedirect:
    form = PurchasePolicyForm(request.POST, instance=PurchasePolicy.get())
    if form.is_valid():
        form.save()
        messages.success(request, "Købsgrænsen er gemt.")
    else:
        messages.error(request, "Kunne ikke gemme købsgrænsen.")
    return redirect("oelkaelder:admin")


@require_POST
@role_required("oelkaelder")
def apply_interest_view(request: HttpRequest) -> HttpResponseRedirect:
    count = apply_interest()
    if count:
        messages.success(request, f"Rente anvendt: {count} postering(er).")
    else:
        messages.info(request, "Ingen rente anvendt (renten er slået fra, eller ingen skylder over grænsen).")
    return redirect("oelkaelder:admin")


# ---- reports (ØK role): on-screen table + CSV/Excel download ----
def _report_range(request: HttpRequest) -> tuple[date, date]:
    """Parse ?start=&end= (YYYY-MM-DD), defaulting to a wide-open range."""

    def parse(name: str, fallback: date) -> date:
        try:
            return date.fromisoformat(request.GET[name])
        except (KeyError, ValueError):
            return fallback

    return parse("start", date(2000, 1, 1)), parse("end", timezone.localdate())


def _report_response(
    request: HttpRequest, title: str, headers: list[str], data: list[list[str]], filename: str
) -> HttpResponse:
    """Render an on-screen report table, or stream CSV/Excel when ?format=csv|xlsx."""
    download = csv_or_xlsx_response(request.GET.get("format"), title, headers, data, filename)
    if download is not None:
        return download
    start, end = _report_range(request)
    return render(
        request,
        "oelkaelder/report.html",
        {"title": title, "headers": headers, "data": data, "start": start, "end": end, "filename": filename},
    )


@role_required("oelkaelder")
def report_deposits(request: HttpRequest) -> HttpResponse:
    start, end = _report_range(request)
    lo = timezone.make_aware(datetime.combine(start, datetime.min.time()))
    hi = timezone.make_aware(datetime.combine(end, datetime.max.time()))
    data = []
    for s in Shopper.objects.filter(active=True).select_related("resident").order_by("resident__first_name"):
        total = s.deposits.filter(is_valid=True, created_at__range=(lo, hi)).aggregate(x=Sum("amount_ore"))[
            "x"
        ]
        data.append([s.resident.full_name, _kr(total or 0), _kr(s.balance_ore)])
    return _report_response(
        request, "Indbetalinger", ["Navn", "Indbetalinger", "Saldo"], data, f"indbetalinger-{start}-{end}"
    )


@role_required("oelkaelder")
def report_sales(request: HttpRequest) -> HttpResponse:
    start, end = _report_range(request)
    lo = timezone.make_aware(datetime.combine(start, datetime.min.time()))
    hi = timezone.make_aware(datetime.combine(end, datetime.max.time()))
    rows = (
        TransactionItem.objects.filter(transaction__is_valid=True, transaction__created_at__range=(lo, hi))
        .values("product__name")
        .annotate(qty=Sum("quantity"), total=Sum("price_ore"))
        .order_by("product__name")
    )
    data = [[r["product__name"], str(r["qty"] or 0), _kr(r["total"] or 0)] for r in rows]
    return _report_response(request, "Salg", ["Vare", "Antal", "Beløb"], data, f"salg-{start}-{end}")


@role_required("oelkaelder")
def report_quantity(request: HttpRequest) -> HttpResponse:
    start, end = _report_range(request)
    lo = timezone.make_aware(datetime.combine(start, datetime.min.time()))
    hi = timezone.make_aware(datetime.combine(end, datetime.max.time()))
    rows = (
        TransactionItem.objects.filter(transaction__is_valid=True, transaction__created_at__range=(lo, hi))
        .values("product__name")
        .annotate(qty=Sum("quantity"))
        .order_by("product__name")
    )
    data = [[r["product__name"], str(r["qty"] or 0)] for r in rows]
    return _report_response(request, "Antal", ["Vare", "Antal"], data, f"antal-{start}-{end}")


def _kr(ore: int) -> str:
    return f"{ore / 100:.2f}".replace(".", ",")


# ---- Salgsoverblik (ØK role): every sale, one row per transaction (F-003, legacy `allsales`) ----
PAGE_SIZE = 50
EXPORT_MAX_ROWS = 5000
DEFAULT_RANGE_DAYS = 90
SALES_HEADERS = ["Dato", "Personer", "I alt", "Pr. person", "Varer", "Status"]
_FILTER_KEYS = ("start", "end", "q", "buyers")
NO_BUYERS = "(ukendt — fraflytter)"


class _SaleRow(TypedDict):
    txn_id: int
    created_at: datetime
    is_valid: bool
    has_buyers: bool
    people: str
    total_ore: int
    per_person: str
    items: str


def _sales_range(request: HttpRequest) -> tuple[date, date]:
    """Like _report_range, but defaults to the last 90 days instead of the year 2000. This page is
    reachable with no query string at all, and a 2000-01-01 default would make the *default* render —
    and the export button sitting next to it — cover every transaction ever recorded."""
    today = timezone.localdate()
    start, end = _report_range(request)
    if "start" not in request.GET:
        start = today - timedelta(days=DEFAULT_RANGE_DAYS)
    return start, end


def _filter_query(source: QueryDict, *, extra_keys: tuple[str, ...] = ()) -> str:
    """Re-encode only this page's own filter params, so pagers and post-action redirects preserve the
    user's filters. Whitelisted on purpose: never echoes arbitrary input into a redirect, and never
    carries `format` (which would turn a "næste side" link into a CSV download)."""
    params = QueryDict(mutable=True)
    for key in (*_FILTER_KEYS, *extra_keys):
        value = source.get(key)
        if value:
            params[key] = value
    return params.urlencode()


def _sales_queryset(start: date, end: date, query: str, buyers: str) -> QuerySet[Transaction]:
    """Transactions in the range, newest first, with items and buyers prefetched (3 queries per page).

    The explicit order_by inside each Prefetch is required: neither child model has Meta.ordering, so
    the buyer and item lists would otherwise render in whatever order the planner returned.
    """
    lo = timezone.make_aware(datetime.combine(start, datetime.min.time()))
    hi = timezone.make_aware(datetime.combine(end + timedelta(days=1), datetime.min.time()))
    qs = Transaction.objects.filter(created_at__gte=lo, created_at__lt=hi).prefetch_related(
        Prefetch("items", queryset=TransactionItem.objects.select_related("product").order_by("id")),
        Prefetch(
            "shares",
            queryset=PurchaseShare.objects.select_related("shopper__resident").order_by("id"),
        ),
    )
    if query:
        # Exists(), not filter(shares__shopper__resident__…): the join spelling returns a transaction
        # once per *matching buyer*, which duplicates rows in the table, in Paginator.count and in the
        # export. .distinct() would mask that at the cost of the index, and only works by coincidence
        # of the current select list. Concat so "Anders Andersen" matches; per-field icontains cannot.
        people = (
            PurchaseShare.objects.filter(transaction=OuterRef("pk"))
            .annotate(
                full=Concat("shopper__resident__first_name", Value(" "), "shopper__resident__last_name")
            )
            .filter(Q(full__icontains=query) | Q(shopper__resident__email__icontains=query))
        )
        qs = qs.filter(Exists(people))
    if buyers in ("none", "any"):
        has_share = PurchaseShare.objects.filter(transaction=OuterRef("pk"))
        qs = qs.filter(~Exists(has_share) if buyers == "none" else Exists(has_share))
    return qs


def _per_person(shares: list[PurchaseShare]) -> str:
    """The per-buyer amount read off the actual shares — never total/len(shares). Two reasons: the ETL
    left many historic transactions with no shares at all (that would be a ZeroDivisionError), and the
    largest-remainder split genuinely differs by an øre between buyers, so a division would lie."""
    if not shares:
        return "—"
    low = min(s.share_ore for s in shares)
    high = max(s.share_ore for s in shares)
    return _kr(low) if low == high else f"{_kr(low)}–{_kr(high)}"


def _sale_rows(transactions: Iterable[Transaction]) -> list[_SaleRow]:
    """Build display rows from prefetched data — no further queries. Used by both the HTML page and
    the export so the two can never disagree."""
    rows = []
    for txn in transactions:
        items = list(txn.items.all())
        shares = list(txn.shares.all())
        rows.append(
            _SaleRow(
                txn_id=txn.pk,
                created_at=txn.created_at,
                is_valid=txn.is_valid,
                has_buyers=bool(shares),
                people=", ".join(s.shopper.resident.full_name for s in shares) or NO_BUYERS,
                # Sum the items, not the shares: for a transaction whose buyers were never migrated the
                # share sum is 0, and reporting 0 kr would make the cellar look like it sold nothing.
                total_ore=sum(i.price_ore for i in items),
                per_person=_per_person(shares),
                items=", ".join(f"{i.quantity}× {i.product.name}" for i in items) or "—",
            )
        )
    return rows


@role_required("oelkaelder")
def all_sales(request: HttpRequest) -> HttpResponse:
    """Salgsoverblik: every sale, one row per transaction, with the buyers who split it."""
    start, end = _sales_range(request)
    query = (request.GET.get("q") or "").strip()
    buyers = request.GET.get("buyers", "")
    qs = _sales_queryset(start, end, query, buyers)
    base_query = _filter_query(request.GET)

    if request.GET.get("format") in ("csv", "xlsx"):
        # Export the whole filtered set, not the current page. Capped: _report_response materialises
        # every row, and openpyxl holds each cell as an object — the full ~99k-row history would be
        # hundreds of MB and outlive the 60s request timeout. Streaming would not help; the timeout
        # counts time since the worker last checked in per request, not per chunk.
        count = qs.count()
        if count > EXPORT_MAX_ROWS:
            messages.error(
                request,
                f"{count} handler er for mange at eksportere (grænsen er {EXPORT_MAX_ROWS}). "
                "Vælg en kortere periode.",
            )
            return redirect(f"{reverse('oelkaelder:all_sales')}?{base_query}")
        data = [
            [
                timezone.localtime(r["created_at"]).strftime("%Y-%m-%d %H:%M"),
                r["people"],
                _kr(r["total_ore"]),
                r["per_person"],
                r["items"],
                "" if r["is_valid"] else "Annulleret",
            ]
            for r in _sale_rows(qs)
        ]
        return _report_response(request, "Salgsoverblik", SALES_HEADERS, data, f"salgsoverblik-{start}-{end}")

    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "oelkaelder/salgsoverblik.html",
        {
            "page_obj": page,
            "rows": _sale_rows(page.object_list),
            "start": start,
            "end": end,
            "query": query,
            "buyers": buyers,
            "base_query": base_query,
        },
    )


@require_POST
@role_required("oelkaelder")
def void_sale(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Annullér a mistaken sale. POST-only: the legacy `deleteTransaction` moved money on GET."""
    if void_purchase(pk, current_resident(request).email):
        messages.success(request, f"Køb #{pk} annulleret. Købernes saldi er rettet.")
    else:
        messages.info(request, f"Køb #{pk} var allerede annulleret.")
    query = _filter_query(request.POST, extra_keys=("page",))
    return redirect(f"{reverse('oelkaelder:all_sales')}?{query}")


@role_required("oelkaelder")
def person_history(request: HttpRequest) -> HttpResponse:
    """One resident's full ølkælder history — purchases, deposits and adjustments — so the ØK can see
    not just what somebody bought but why their balance is what it is."""
    residents = (
        Resident.objects.filter(shopper_accounts__isnull=False).distinct().order_by("first_name", "last_name")
    )
    chosen = None
    accounts: list[Shopper] = []
    raw_id = request.GET.get("resident", "")
    if raw_id.isdigit():
        chosen = residents.filter(pk=int(raw_id)).first()
        if chosen:
            accounts = list(chosen.shopper_accounts.all())
    return render(
        request,
        "oelkaelder/person.html",
        {
            "residents": residents,
            "chosen": chosen,
            "accounts": [(a, a.balance_ore) for a in accounts],
            "entries": _account_entries(accounts)[:200] if accounts else [],
            "adjustments": (
                Adjustment.objects.filter(shopper__in=accounts, kind=Adjustment.Kind.MANUAL)
                .select_related("shopper")
                .order_by("-created_at")
                if accounts
                else []
            ),
        },
    )


def _signed_ore(amount_kr: str, direction: str) -> int:
    """Parse a positive kroner amount plus a direction into signed øre. The form asks for a direction
    rather than a minus sign because "-50" vs "50" in a free-text field is far too easy to get wrong
    when the mistake silently moves someone's money the wrong way."""
    try:
        ore = round(float((amount_kr or "0").replace(",", ".")) * 100)
    except ValueError:
        raise ValueError("Ugyldigt beløb.") from None
    if ore <= 0:
        raise ValueError("Beløbet skal være positivt — vælg i stedet retning ovenfor.")
    return -ore if direction == "subtract" else ore


@require_POST
@role_required("oelkaelder")
def add_adjustment(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Manually correct one shopper's balance, with a mandatory written explanation."""
    shopper = get_object_or_404(Shopper, pk=pk)
    try:
        amount = _signed_ore(request.POST.get("amount_kr", ""), request.POST.get("direction", "subtract"))
        record_adjustment(shopper, amount, request.POST.get("reason", ""), current_resident(request).email)
        messages.success(
            request, f"Justering på {_kr(amount)} kr registreret for {shopper.resident.full_name}."
        )
    except ValueError as e:
        messages.error(request, str(e))
    return redirect(f"{reverse('oelkaelder:person_history')}?resident={shopper.resident_id}")


@require_POST
@role_required("oelkaelder")
def void_adjustment_view(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    adjustment = get_object_or_404(Adjustment, pk=pk)
    if void_adjustment(pk, current_resident(request).email):
        messages.success(request, "Justering annulleret.")
    else:
        messages.info(request, "Justeringen var allerede annulleret.")
    return redirect(f"{reverse('oelkaelder:person_history')}?resident={adjustment.shopper.resident_id}")


@require_POST
@role_required("oelkaelder")
def add_deposit(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    shopper = get_object_or_404(Shopper, pk=pk)
    try:
        kr = float(request.POST.get("amount_kr", "0").replace(",", "."))
        record_deposit(shopper, round(kr * 100))
        messages.success(request, f"Indbetaling registreret for {shopper.resident.full_name}.")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("oelkaelder:admin")


@require_POST
@role_required("oelkaelder")
def void_deposit(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Soft-delete a mistaken deposit (is_valid=False) — the balance is derived, so it just drops out."""
    deposit = get_object_or_404(Deposit, pk=pk, is_valid=True)
    deposit.is_valid = False
    deposit.save(update_fields=["is_valid"])
    messages.success(request, "Indbetaling annulleret.")
    return redirect("oelkaelder:admin")


# ---- product & price management (ØK role) ----
def _kr_to_ore(value: str) -> int:
    ore = round(float((value or "0").replace(",", ".")) * 100)
    if ore <= 0:
        raise ValueError("Prisen skal være positiv.")
    return ore


@role_required("oelkaelder")
def products(request: HttpRequest) -> HttpResponse:
    """Manage the kiosk assortment: name, price, in/out of the till, featured, and image."""
    return render(
        request,
        "oelkaelder/products.html",
        {"products": Product.objects.order_by("-active", "-highlighted", "name")},
    )


@require_POST
@role_required("oelkaelder")
def product_create(request: HttpRequest) -> HttpResponseRedirect:
    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, "Produktet skal have et navn.")
        return redirect("oelkaelder:products")
    try:
        price_ore = _kr_to_ore(request.POST.get("price_kr", ""))
    except ValueError as e:
        messages.error(request, str(e))
        return redirect("oelkaelder:products")
    Product.objects.create(
        name=name,
        price_ore=price_ore,
        active="active" in request.POST,
        highlighted="highlighted" in request.POST,
        image=request.FILES.get("image") or "",
    )
    messages.success(request, f"«{name}» oprettet.")
    return redirect("oelkaelder:products")


@require_POST
@role_required("oelkaelder")
def product_update(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    product = get_object_or_404(Product, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, "Produktet skal have et navn.")
        return redirect("oelkaelder:products")
    try:
        product.price_ore = _kr_to_ore(request.POST.get("price_kr", ""))
    except ValueError as e:
        messages.error(request, str(e))
        return redirect("oelkaelder:products")
    product.name = name
    product.active = "active" in request.POST
    product.highlighted = "highlighted" in request.POST
    if request.FILES.get("image"):
        product.image = request.FILES["image"]
    product.save()
    messages.success(request, f"«{name}» opdateret.")
    return redirect("oelkaelder:products")
