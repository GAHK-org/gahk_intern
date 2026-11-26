"""The Adgang page (/access/): the kitchen staff's accounts, and the roles given by hand.

Køkkengruppen and the Indstilling add and end the kitchen staff (access.access_kitchen_staff); the
Indstilling also sees and ends the other roles given by hand (access.access_manage), all but
administrator, which only administrators give and end (the role page, with its two-person rule).
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
from django.utils.http import urlsafe_base64_encode
from django.views.decorators.http import require_POST

from audit import trail
from core.clock import current_date
from residents.forms import password_link_mail
from residents.models import Account, Person
from residents.permissions import current_resident

from .api import ADMINISTRATOR, KITCHEN, ROLE_LABELS, permission_required
from .forms import KitchenStaffForm
from .models import Grant

logger = logging.getLogger(__name__)

KITCHEN_STAFF = "access.access_kitchen_staff"
MANAGE = "access.access_manage"


def _label(grant: Grant) -> str:
    return ROLE_LABELS.get(grant.role, grant.role)


def index(request: HttpRequest) -> HttpResponse:
    kitchen_staff = request.user.has_perm(KITCHEN_STAFF)
    manage = request.user.has_perm(MANAGE)
    current = (
        Grant.objects.active()
        .select_related("account", "account__person__resident", "granted_by")
        .order_by("account__first_name", "starts_on")
    )
    current = current.exclude(role=ADMINISTRATOR) if manage else current.filter(role=KITCHEN)
    kitchen = [g for g in current if g.role == KITCHEN] if kitchen_staff else []
    others = [(g, _label(g)) for g in current if g.role != KITCHEN] if manage else []
    return render(
        request,
        "access/index.html",
        {"kitchen": kitchen, "others": others, "kitchen_form": KitchenStaffForm() if kitchen_staff else None},
    )


@require_POST
def grant_end(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """End a grant today: it held through yesterday. One that has not begun yet is removed."""
    grant = get_object_or_404(Grant.objects.select_related("account"), pk=pk)
    needed = KITCHEN_STAFF if grant.role == KITCHEN else MANAGE
    if grant.role == ADMINISTRATOR or not request.user.has_perm(needed):
        raise PermissionDenied
    yesterday = current_date() - datetime.timedelta(days=1)
    with trail.event("access.grant.ended", subject=grant.account, details={"role": grant.role}):
        if grant.starts_on > yesterday:
            grant.delete()
        elif grant.ends_on is None or grant.ends_on > yesterday:
            grant.ends_on = yesterday
            grant.save(update_fields=["ends_on"])
    messages.success(request, f"{grant.account.full_name} har ikke længere {_label(grant)}.")
    return redirect("access:index")


@require_POST
@permission_required(KITCHEN_STAFF)
def kitchen_staff_add(request: HttpRequest) -> HttpResponseRedirect:
    """An account of their own for one of the kitchen staff: a person (no resident: they do not
    live here), their login, the kitchen role from today, and a mail to set the password."""
    form = KitchenStaffForm(request.POST)
    if not form.is_valid():
        messages.error(
            request,
            "Kontoen blev ikke oprettet: " + "; ".join(str(e) for es in form.errors.values() for e in es),
        )
        return redirect("access:index")
    data = form.cleaned_data
    person = Person(first_name=data["first_name"], last_name=data["last_name"])
    account = Account(email=data["email"], first_name=data["first_name"], last_name=data["last_name"])
    with transaction.atomic(), trail.event("access.kitchen_staff.added", subject=account):
        person.save()
        account.person = person
        account.set_unusable_password()  # they set one from the welcome mail's link
        account.save()
        Grant.objects.create(
            account=account,
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


def _send_welcome(request: HttpRequest, account: Account) -> bool:
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
