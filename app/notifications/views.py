"""The resident's notification pages: the bell's dropdown, the list, the settings, and the one
device-subscribe endpoint.

Every view is @login_required and nothing more. Notifications are not a feature behind a rollout
gate — they are how the gated features reach people — and every query here is scoped to the
requesting resident's own rows, so there is nothing of anyone else's to gate.
"""

import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import push
from residents.permissions import current_resident

from . import categories, services
from .categories import IMPORTANCE_CHOICES, Importance
from .models import Notification, NotificationPreference, NotificationSettings

# How many the bell's dropdown shows. The page shows the rest.
DROPDOWN_SIZE = 10

VIEWS = (("kronologisk", "Kronologisk"), ("kategori", "Efter kategori"))


def _safe(url: str) -> str:
    """Redirect target for a stored row. Rows are written by our own code with site-relative paths;
    this refuses anything else anyway, so a bad row can never become an open redirect."""
    return url if services.is_site_path(url) else "/intern/notifikationer/"


@login_required
def index(request: HttpRequest) -> HttpResponse:
    """Every active notification, most important first, then newest. `?visning=kategori` groups
    them under their category's group instead."""
    resident = current_resident(request)
    view = request.GET.get("visning", "kronologisk")
    if view not in dict(VIEWS):
        view = "kronologisk"
    rows = list(Notification.objects.filter(recipient=resident).visible().ranked())
    groups: list[tuple[str, list[Notification]]] = []
    if view == "kategori":
        by_group: dict[str, list[Notification]] = {}
        for row in rows:
            by_group.setdefault(row.group, []).append(row)
        groups = [(name, by_group[key]) for key, name in categories.GROUPS if key in by_group]
    return render(
        request,
        "notifications/index.html",
        {
            "rows": rows,
            "groups": groups,
            "view": view,
            "views": VIEWS,
            "unseen": sum(1 for row in rows if not row.is_seen),
            "category_names": {c.key: c.name for c in categories.CATEGORIES},
        },
    )


@login_required
def dropdown(request: HttpRequest) -> HttpResponse:
    """The bell's panel, fetched by htmx the first time it opens — so a page render costs one COUNT
    for the badge, not ten rows nobody looked at."""
    resident = current_resident(request)
    rows = Notification.objects.filter(recipient=resident).visible().ranked()[:DROPDOWN_SIZE]
    return render(request, "notifications/_dropdown.html", {"rows": rows})


@login_required
def open_item(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Clicking a notification in the app: mark it seen, go where it points."""
    row = get_object_or_404(Notification, pk=pk, recipient=current_resident(request))
    services.mark_seen(Notification.objects.filter(pk=row.pk))
    return redirect(_safe(row.url))


@login_required
def open_batch(request: HttpRequest, batch: uuid.UUID) -> HttpResponseRedirect:
    """Tapping a push: the payload is shared by every recipient of one fan-out, so it names the
    batch and this finds the tapping resident's own row in it. No row (purged, or the push was for
    someone else who used to own this browser) lands on the list rather than erroring."""
    row = Notification.objects.filter(recipient=current_resident(request), batch=batch).first()
    if row is None:
        return redirect("notifications:index")
    services.mark_seen(Notification.objects.filter(pk=row.pk))
    return redirect(_safe(row.url))


@require_POST
@login_required
def mark_all_seen(request: HttpRequest) -> HttpResponseRedirect:
    services.mark_seen(Notification.objects.filter(recipient=current_resident(request)).visible())
    return redirect("notifications:index")


@login_required
def preferences(request: HttpRequest) -> HttpResponse:
    """The resident's settings: per category in-app / push / vibration, the vibration switches,
    and this device's push subscription.

    Saved as exceptions: a category back at the default (everything on) has its row deleted, so the
    table only ever holds what somebody changed — see models' module docstring.
    """
    resident = current_resident(request)
    settings_row = NotificationSettings.objects.filter(resident=resident).first() or NotificationSettings(
        resident=resident
    )
    saved = {p.category: p for p in NotificationPreference.objects.filter(resident=resident)}

    if request.method == "POST":
        with transaction.atomic():
            for category in categories.CATEGORIES:
                # A category without an optional in-app row is stored as on whatever is posted.
                optional = category.in_app and category.in_app_optional
                in_app = (f"{category.key}__in_app" in request.POST) if optional else True
                push_on = f"{category.key}__push" in request.POST
                vibrate = f"{category.key}__vibrate" in request.POST
                if in_app and push_on and vibrate:
                    NotificationPreference.objects.filter(resident=resident, category=category.key).delete()
                else:
                    NotificationPreference.objects.update_or_create(
                        resident=resident,
                        category=category.key,
                        defaults={"in_app": in_app, "push": push_on, "vibrate": vibrate},
                    )
            settings_row.vibrate = "vibrate" in request.POST
            try:
                level = Importance(int(request.POST.get("vibrate_min_importance", Importance.LAV)))
            except ValueError:
                level = Importance.LAV
            settings_row.vibrate_min_importance = level
            settings_row.save()
        messages.success(request, "Dine notifikationsindstillinger er gemt.")
        return redirect("notifications:preferences")

    groups = []
    for key, name, leaves in categories.grouped():
        rows = []
        for category in leaves:
            pref = saved.get(category.key)
            rows.append(
                {
                    "category": category,
                    "in_app": (pref.in_app if pref else True) or not category.in_app_optional,
                    "push": pref.push if pref else True,
                    "vibrate": pref.vibrate if pref else True,
                    "anchor": (
                        "rum-" + category.key.removeprefix(categories.CHAT_ROOM_PREFIX)
                        if category.key.startswith(categories.CHAT_ROOM_PREFIX)
                        else ""
                    ),
                }
            )
        groups.append({"key": key, "name": name, "rows": rows})
    return render(
        request,
        "notifications/preferences.html",
        {
            "groups": groups,
            "settings": settings_row,
            "importance_choices": IMPORTANCE_CHOICES,
            "push_configured": push.is_configured(),
            "vapid_public_key": push.vapid_public_key(),
        },
    )


@require_POST
@login_required
def subscription(request: HttpRequest) -> HttpResponse:
    """This device's push subscription: subscribe / unsubscribe / status. See
    core.push.handle_subscription_request."""
    return push.handle_subscription_request(request)
