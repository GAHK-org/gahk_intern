"""The Adgang page (/access/): the kitchen staff's accounts, and the roles given by hand.

Køkkengruppen and the Indstilling add and end the kitchen staff (access.kitchen_staff); only the
Indstilling gives other roles by hand (access.manage), from a resident's edit page or here.
"""

import datetime
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_encode
from django.views.decorators.http import require_POST

from audit import trail
from core.clock import current_date
from core.danish import genitive
from residents.forms import password_link_mail
from residents.models import Person, Resident
from residents.permissions import current_resident

from .api import KITCHEN, ROLE_LABELS, can, can_preview, permission_required, real_roles
from .explain import day, explain
from .forms import GrantForm, KitchenStaffForm
from .models import Grant

logger = logging.getLogger(__name__)


def _back(request: HttpRequest) -> HttpResponseRedirect:
    """To the page the form was on (a resident's edit page), or the Adgang page."""
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect("access:index")


def _label(grant: Grant) -> str:
    return ROLE_LABELS.get(grant.role, grant.role)


def index(request: HttpRequest) -> HttpResponse:
    today = current_date()
    kitchen_staff = can(request, "access.kitchen_staff")
    manage = can(request, "access.manage")
    current = Grant.objects.filter(ends_on__isnull=True) | Grant.objects.filter(ends_on__gte=today)
    current = current.select_related("resident", "granted_by").order_by("resident__first_name", "starts_on")
    kitchen = [g for g in current if g.role == KITCHEN] if kitchen_staff else []
    others = [(g, _label(g)) for g in current if g.role != KITCHEN] if manage else []
    return render(
        request,
        "access/index.html",
        {
            "kitchen": kitchen,
            "others": others,
            "kitchen_form": KitchenStaffForm() if kitchen_staff else None,
            "can_manage": manage,
            "today": today,
        },
    )


@require_POST
@permission_required("access.manage")
def grant_add(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    resident = get_object_or_404(Resident, pk=pk)
    form = GrantForm(request.POST)
    if not form.is_valid():
        messages.error(
            request,
            "Adgangen blev ikke givet: " + "; ".join(str(e) for es in form.errors.values() for e in es),
        )
        return _back(request)
    data = form.cleaned_data
    with trail.event("access.grant.added", subject=resident):
        Grant.objects.create(
            resident=resident,
            role=data["role"],
            reason=data["reason"],
            starts_on=data["starts_on"],
            ends_on=data["ends_on"],
            granted_by=current_resident(request),
        )
    messages.success(request, f"{resident.full_name} har fået {ROLE_LABELS[data['role']]}.")
    return _back(request)


@require_POST
def grant_end(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """End a grant today: it held through yesterday. One that has not begun is removed instead."""
    grant = get_object_or_404(Grant.objects.select_related("resident"), pk=pk)
    needed = "access.kitchen_staff" if grant.role == KITCHEN else "access.manage"
    if not can(request, needed):
        raise PermissionDenied
    yesterday = current_date() - datetime.timedelta(days=1)
    with trail.event("access.grant.ended", subject=grant.resident):
        if grant.starts_on > yesterday:
            grant.delete()
        elif grant.ends_on is None or grant.ends_on > yesterday:
            grant.ends_on = yesterday
            grant.save(update_fields=["ends_on"])
    messages.success(request, f"{grant.resident.full_name} har ikke længere {_label(grant)}.")
    return _back(request)


@require_POST
@permission_required("access.kitchen_staff")
def kitchen_staff_add(request: HttpRequest) -> HttpResponseRedirect:
    """An account of their own for one of the kitchen staff, with the kitchen role, and a mail to
    set its password."""
    form = KitchenStaffForm(request.POST)
    if not form.is_valid():
        messages.error(
            request,
            "Kontoen blev ikke oprettet: " + "; ".join(str(e) for es in form.errors.values() for e in es),
        )
        return redirect("access:index")
    data = form.cleaned_data
    account = Resident(email=data["email"], first_name=data["first_name"], last_name=data["last_name"])
    with transaction.atomic(), trail.event("access.kitchen_staff.added", subject=account):
        # A person as well as a login: they are people the house knows, never on its list.
        account.person = Person.objects.create(first_name=data["first_name"], last_name=data["last_name"])
        account.set_unusable_password()  # they set one from the welcome mail's link
        account.save()
        Grant.objects.create(
            resident=account,
            role=KITCHEN,
            reason="Køkkenpersonale",
            starts_on=current_date(),
            granted_by=current_resident(request),
        )
    if _send_welcome(request, account):
        messages.success(
            request, f"{account.full_name} er oprettet og har fået en mail med et link til at sætte kodeord."
        )
    else:
        messages.warning(request, f"{account.full_name} er oprettet, men mailen kunne ikke sendes.")
    return redirect("access:index")


def _send_welcome(request: HttpRequest, account: Resident) -> bool:
    uid = urlsafe_base64_encode(force_bytes(account.pk))
    token = default_token_generator.make_token(account)
    set_link = request.build_absolute_uri(
        reverse("password_reset_confirm", kwargs={"uidb64": uid, "token": token})
    )
    reset_link = request.build_absolute_uri(reverse("password_reset"))
    try:
        with password_link_mail():
            send_mail(
                "Velkommen til GAHK Intern",
                (
                    f"Kære {account.first_name}\n\n"
                    f"Du har fået en konto på GAHKs interne netværk med e-mailen {account.email}, "
                    "så du kan skrive i køkkenets kanal på Den Hurtige og melde reparationer.\n\n"
                    f"Sæt dit kodeord her:\n{set_link}\n\n"
                    f"Hvis linket er udløbet, kan du anmode om et nyt på:\n{reset_link}\n\n"
                    "Mvh. GAHK"
                ),
                settings.DEFAULT_FROM_EMAIL,
                [account.email],
            )
    except Exception:
        logger.exception("Failed sending the welcome mail to kitchen account %s", account.pk)
        return False
    return True


@permission_required("access.manage")
def person(request: HttpRequest, pk: int) -> HttpResponse:
    """One account's access, and why: its roles with where each comes from, what each feature is
    open for, and what is about to change."""
    resident = get_object_or_404(Resident, pk=pk)
    return render(request, "access/person.html", _person_page(request, resident, own=False))


def mine(request: HttpRequest) -> HttpResponse:
    """The same page about oneself."""
    resident = current_resident(request)
    return render(request, "access/person.html", _person_page(request, resident, own=True))


def _person_page(request: HttpRequest, resident: Resident, *, own: bool) -> dict[str, object]:
    explanation = explain(resident)
    return {
        "resident": resident,
        "own": own,
        "explanation": explanation,
        "changes": [(day(change.on), change.text) for change in explanation.changes],
        "may_preview": not own and can_preview(request.user),
        "whose": genitive(resident.first_name),
    }


def person_context(request: HttpRequest, resident: Resident) -> dict[str, object]:
    """What a resident's edit page shows of their access: their roles now, and their grants."""
    today = current_date()
    grants = Grant.objects.filter(resident=resident).select_related("granted_by").order_by("-starts_on")
    return {
        "access_roles": sorted(ROLE_LABELS.get(role, role) for role in real_roles(resident)),
        "access_grants": [(g, _label(g), g.ends_on is None or g.ends_on >= today) for g in grants],
        "grant_form": GrantForm() if can(request, "access.manage") else None,
    }
