"""GAHK-konto's pages: one's own accounts and statements, sending money, a book's page for its
managers, and making books (administrators). Every rule about money is the ledger's; a view only
picks what to show and passes the request's login on as the actor."""

import uuid

from django import forms
from django.contrib import messages
from django.db.models import Q, QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from access.api import permission_required
from residents.models import Account

from . import closing, holders, ledger, statements
from .forms import BookForm, ChargeForm, LimitForm, OpenAccountForm, TransferForm, parse_kr
from .ledger import LedgerError
from .models import Book, CreditAccount, Transfer


def _errors(form: forms.Form) -> str:
    return " ".join(str(error) for errors in form.errors.values() for error in errors)


def _user(request: HttpRequest) -> Account:
    return request.user  # type: ignore[return-value]


def _accounts_of(account: Account) -> QuerySet[CreditAccount]:
    """The open accounts this login may send from: its person's and its embedsgrupper's."""
    ids = [h.pk for h in holders.holders_of(account)]
    return CreditAccount.objects.filter(holder_id__in=ids, closed_at__isnull=True).select_related(
        "book", "holder__person", "holder__workgroup"
    )


def index(request: HttpRequest) -> HttpResponse:
    user = _user(request)
    if user.person_id is not None:
        ledger.main_account(user.person)
    mine = []
    for holder in holders.holders_of(user):
        accounts = ledger.with_balances(
            CreditAccount.objects.filter(holder=holder, counterpart_book__isnull=True, bank_reference="")
            .exclude(book__holder=holder)
            .select_related("book")
        )
        if accounts:
            mine.append((holder, list(accounts)))
    return render(
        request,
        "credit/index.html",
        {"mine": mine, "books": holders.managed_books(user)},
    )


def account(request: HttpRequest, pk: int) -> HttpResponse:
    credit_account = get_object_or_404(CreditAccount.objects.select_related("book", "holder"), pk=pk)
    user = _user(request)
    if not holders.may_see(user, credit_account):
        raise Http404
    manages = holders.manages(user, credit_account.book)
    lines = ledger.statement(credit_account, limit=500)
    reversible = set()
    managed_books = {b.pk for b in holders.managed_books(user)}
    for line in lines:
        made = line.transfer
        if made.kind not in (Transfer.Kind.TRANSFER, Transfer.Kind.CHARGE) or hasattr(made, "reversed_by"):
            continue
        receiving = credit_account if line.posting.amount_ore > 0 else line.counterparty
        if receiving.book_id in managed_books:
            reversible.add(made.pk)
    return render(
        request,
        "credit/account.html",
        {
            "account": credit_account,
            "balance": ledger.balance_of(credit_account),
            "lines": lines,
            "manages": manages,
            "own": holders.acts_for(user, credit_account.holder),
            "reversible": reversible,
            "limit_form": LimitForm(initial={"credit_limit": _kr_input(credit_account.credit_limit_ore)}),
            "statement_months": _ended_months(credit_account),
        },
    )


def _ended_months(credit_account: CreditAccount, count: int = 12) -> list[tuple[int, int]]:
    """The last `count` months that have ended since the account was opened, newest first."""
    opened = timezone.localtime(credit_account.created_at).date().replace(day=1)
    year, month = statements.previous_month(timezone.localdate())
    out: list[tuple[int, int]] = []
    while len(out) < count and (year, month) >= (opened.year, opened.month):
        out.append((year, month))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return out


def statement(request: HttpRequest, pk: int, year: int, month: int) -> HttpResponse:
    """One account's signed statement for an ended month: for a person without a login, their
    book's managers fetch it here and hand it on."""
    credit_account = get_object_or_404(CreditAccount.objects.select_related("book", "holder"), pk=pk)
    if not holders.may_see(_user(request), credit_account) or (year, month) not in _ended_months(
        credit_account, 1200
    ):
        raise Http404
    item = statements.account_month(credit_account, year, month)
    pdf = statements.pdf_for(credit_account.holder.name, [item], year, month)
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="gahk-konto-{credit_account.pk}-{year}-{month:02d}.pdf"'
    )
    return response


def _kr_input(ore: int | None) -> str:
    return "" if ore is None else f"{ore / 100:.2f}".replace(".", ",")


def _receivers(user: Account) -> QuerySet[CreditAccount]:
    """Every open account, but a mellemregning only for its own book's managers."""
    managed = [b.pk for b in holders.managed_books(user)]
    return (
        CreditAccount.objects.filter(closed_at__isnull=True)
        .filter(Q(counterpart_book__isnull=True) | Q(book_id__in=managed))
        .select_related("book", "holder__person", "holder__workgroup")
        .order_by("holder__person__first_name", "holder__workgroup__name", "name")
    )


def transfer(request: HttpRequest) -> HttpResponse:
    user = _user(request)
    senders, receivers = _accounts_of(user), _receivers(user)
    form = TransferForm(
        request.POST or None,
        senders=senders,
        receivers=receivers,
        initial={"sender": request.GET.get("from"), "receiver": request.GET.get("to")},
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            made = ledger.send(
                actor=user,
                sender=data["sender"],
                receiver=data["receiver"],
                amount_ore=data["amount"],
                sender_text=data["sender_text"],
                receiver_text=data["receiver_text"],
                dedupe_token=data["token"],
                source="credit.transfer_form",
            )
        except LedgerError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Overførslen er gennemført (#{made.pk}).")
            return redirect("credit:account", pk=data["sender"].pk)
    balances = {a.pk: ledger.annotated_balance(a) for a in ledger.with_balances(senders)}
    return render(request, "credit/transfer.html", {"form": form, "senders": senders, "balances": balances})


@require_POST
def reverse(request: HttpRequest, pk: int) -> HttpResponse:
    made = get_object_or_404(Transfer, pk=pk)
    back = request.POST.get("back")
    try:
        token = uuid.UUID(request.POST.get("token", ""))
        ledger.reverse(actor=_user(request), transfer=made, dedupe_token=token)
    except (LedgerError, ValueError) as exc:
        messages.error(request, str(exc) or "Tilbageførslen kunne ikke gennemføres.")
    else:
        messages.success(request, f"Overførsel #{made.pk} er tilbageført.")
    return redirect("credit:account", pk=int(back)) if back and back.isdigit() else redirect("credit:index")


@require_POST
def set_limit(request: HttpRequest, pk: int) -> HttpResponse:
    credit_account = get_object_or_404(CreditAccount, pk=pk)
    form = LimitForm(request.POST)
    if not form.is_valid():
        messages.error(request, _errors(form))
        return redirect("credit:account", pk=pk)
    try:
        ledger.set_credit_limit(
            actor=_user(request), account=credit_account, credit_limit_ore=form.cleaned_data["credit_limit"]
        )
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Kreditgrænsen er ændret.")
    return redirect("credit:account", pk=pk)


@require_POST
def close(request: HttpRequest, pk: int) -> HttpResponse:
    credit_account = get_object_or_404(CreditAccount, pk=pk)
    try:
        ledger.close_account(actor=_user(request), account=credit_account)
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Kontoen er lukket.")
    return redirect("credit:account", pk=pk)


def _managed_book(request: HttpRequest, pk: int) -> Book:
    book = get_object_or_404(Book.objects.select_related("holder"), pk=pk)
    if not holders.manages(_user(request), book):
        raise Http404
    return book


def book(request: HttpRequest, pk: int) -> HttpResponse:
    book = _managed_book(request, pk)
    accounts = list(
        ledger.with_balances(
            CreditAccount.objects.filter(book=book).select_related(
                "holder__person", "holder__workgroup", "counterpart_book"
            )
        )
    )
    own = [a for a in accounts if a.holder_id == book.holder_id]
    others = [a for a in accounts if a.holder_id != book.holder_id]
    mirrors = {
        a.counterpart_book_id: ledger.annotated_balance(a)
        for a in ledger.with_balances(CreditAccount.objects.filter(counterpart_book=book))
    }
    settlements = [(a, mirrors.get(a.counterpart_book_id, 0)) for a in own if a.is_mellemregning]
    return render(
        request,
        "credit/book.html",
        {
            "book": book,
            "own": [a for a in own if not a.is_mellemregning],
            "others": others,
            "settlements": settlements,
            "total": sum(ledger.annotated_balance(a) for a in accounts),
            "charge_form": _charge_form(book),
            "closings": book.closings.select_related("anchor", "closed_by")[:24],
            "next_period": closing.next_period(book),
            "may_close": closing.may_close(book),
            "banks": [a for a in own if a.bank_reference],
            "open_form": OpenAccountForm(initial={"credit_limit": _kr_input(book.default_credit_limit_ore)}),
        },
    )


def _charge_form(book: Book, data: object = None) -> ChargeForm:
    accounts = CreditAccount.objects.filter(book=book, closed_at__isnull=True).select_related(
        "book", "holder__person", "holder__workgroup"
    )
    return ChargeForm(
        data,
        accounts=accounts.exclude(holder_id=book.holder_id),
        into=accounts.filter(holder_id=book.holder_id, counterpart_book__isnull=True),
    )


@require_POST
def charge(request: HttpRequest, pk: int) -> HttpResponse:
    book = _managed_book(request, pk)
    form = _charge_form(book, request.POST)
    if form.is_valid():
        data = form.cleaned_data
        try:
            ledger.charge(
                actor=_user(request),
                account=data["account"],
                into=data["into"],
                amount_ore=data["amount"],
                text=data["text"],
                dedupe_token=data["token"],
                source="credit.book_charge",
            )
        except LedgerError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"{data['account']} er opkrævet.")
    else:
        messages.error(
            request,
            "Opkrævningen mangler noget: " + _errors(form),
        )
    return redirect("credit:book", pk=pk)


@require_POST
def open_account(request: HttpRequest, pk: int) -> HttpResponse:
    book = _managed_book(request, pk)
    form = OpenAccountForm(request.POST)
    if not form.is_valid():
        messages.error(request, _errors(form))
        return redirect("credit:book", pk=pk)
    data = form.cleaned_data
    holder = (
        holders.holder_for_person(data["person"])
        if data.get("person")
        else holders.holder_for_workgroup(data["workgroup"])
    )
    try:
        made = ledger.open_account(
            actor=_user(request),
            book=book,
            holder=holder,
            name=data["name"],
            credit_limit_ore=data.get("credit_limit"),
        )
    except LedgerError as exc:
        messages.error(request, str(exc))
        return redirect("credit:book", pk=pk)
    messages.success(request, f"{made} er oprettet.")
    return redirect("credit:account", pk=made.pk)


@permission_required("access.credit_administer")
def books(request: HttpRequest) -> HttpResponse:
    form = BookForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            made = ledger.create_book(
                actor=_user(request),
                name=data["name"],
                holder=holders.holder_for_workgroup(data["workgroup"]),
                bank_name=data["bank_name"],
                bank_reference=data["bank_reference"],
                default_credit_limit_ore=data["default_credit_limit"],
                is_main=data["is_main"],
            )
        except LedgerError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Kontoføreren {made.name} er oprettet.")
            return redirect("credit:books")
    listed = Book.objects.select_related("holder__workgroup", "holder__person").prefetch_related("accounts")
    return render(request, "credit/books.html", {"form": form, "books": listed})


@require_POST
def close_period(request: HttpRequest, pk: int) -> HttpResponse:
    book = _managed_book(request, pk)
    typed: dict[int, int] = {}
    for account in CreditAccount.objects.filter(book=book).exclude(bank_reference=""):
        raw = request.POST.get(f"bank_{account.pk}", "").strip()
        if raw:
            try:
                typed[account.pk] = parse_kr(raw)
            except ValueError:
                messages.error(request, f"Skriv bankens saldo for {account.name} i kroner.")
                return redirect("credit:book", pk=pk)
    try:
        made = closing.close(
            actor=_user(request), book=book, bank_statements=typed, note=request.POST.get("note", "")
        )
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, f"Perioden {made.period_start:%d.%m.%Y}–{made.period_end:%d.%m.%Y} er lukket."
        )
    return redirect("credit:book", pk=pk)
