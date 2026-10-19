"""Site-admin page for notifications: who has which devices, cancelling one, delivery statistics,
and the broadcast form.

Mounted under /admin/ in the siteadmin namespace (residents/urls_admin.py) next to the rest of the
administrator tools, and gated the same way: @role_required("administrator").
"""

import datetime

from django import forms
from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.clock import current_datetime
from core.models import PushDeliveryDay, PushSubscription
from residents.permissions import role_required

from . import categories, services
from .categories import IMPORTANCE_CHOICES, Importance
from .models import Notification

# The windows the statistics are counted over. 30 is also the retention (services.purge_expired),
# so a longer window would quietly undercount.
WINDOWS = (7, 30)


class BroadcastForm(forms.Form):
    """A message to every resident, as a system or general notification."""

    category = forms.ChoiceField(
        label="Kategori",
        choices=[(categories.GENERAL, "Generelt"), (categories.SYSTEM, "System")],
    )
    importance = forms.TypedChoiceField(
        label="Vigtighed", choices=IMPORTANCE_CHOICES, coerce=int, initial=int(Importance.NORMAL)
    )
    title = forms.CharField(label="Titel", max_length=200)
    body = forms.CharField(
        label="Tekst", max_length=300, required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    url = forms.CharField(
        label="Link",
        max_length=500,
        initial="/intern/",
        help_text="En side på intern, fx /intern/opslagstavle/.",
    )

    def clean_url(self) -> str:
        url = str(self.cleaned_data["url"]).strip()
        if not services.is_site_path(url):
            raise forms.ValidationError("Linket skal være en side på sitet og starte med /.")
        return url


def _stats() -> dict[str, object]:
    now = current_datetime()
    windows = []
    for days in WINDOWS:
        rows = Notification.objects.filter(created_at__gte=now - datetime.timedelta(days=days))
        totals = rows.aggregate(
            total=Count("pk"),
            seen=Count("pk", filter=Q(seen_at__isnull=False)),
            pushed=Count("pk", filter=Q(pushed=True)),
        )
        by_category = [
            {
                **row,
                "name": categories.BY_KEY[row["category"]].name
                if row["category"] in categories.BY_KEY
                else row["category"],
            }
            for row in rows.values("category")
            .annotate(
                total=Count("pk"),
                seen=Count("pk", filter=Q(seen_at__isnull=False)),
                pushed=Count("pk", filter=Q(pushed=True)),
            )
            .order_by("-total")
        ]
        by_importance = [
            {**row, "label": Importance(row["importance"]).label}
            for row in rows.values("importance")
            .annotate(total=Count("pk"), seen=Count("pk", filter=Q(seen_at__isnull=False)))
            .order_by("-importance")
        ]
        windows.append({"days": days, **totals, "by_category": by_category, "by_importance": by_importance})
    return {
        "windows": windows,
        "devices": PushSubscription.objects.count(),
        "device_users": PushSubscription.objects.values("user_id").distinct().count(),
        "delivery_days": list(PushDeliveryDay.objects.all()[:30]),
    }


@role_required("administrator")
def notifications(request: HttpRequest) -> HttpResponse:
    form = BroadcastForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        reached = services.notify(
            services.everyone(),
            category=form.cleaned_data["category"],
            importance=Importance(form.cleaned_data["importance"]),
            title=form.cleaned_data["title"],
            body=form.cleaned_data["body"],
            url=form.cleaned_data["url"],
        )
        messages.success(request, f"Sendt til {reached} beboer(e).")
        return redirect("siteadmin:notifications")

    devices = PushSubscription.objects.select_related("user").order_by(
        "user__first_name", "user__last_name", "-created_at"
    )
    by_user: dict[int, dict[str, object]] = {}
    for device in devices:
        entry = by_user.setdefault(device.user_id, {"user": device.user, "devices": []})
        entry["devices"].append(device)  # type: ignore[attr-defined]
    return render(
        request,
        "notifications/admin.html",
        {"form": form, "users": list(by_user.values()), **_stats()},
    )


@require_POST
@role_required("administrator")
def cancel_device(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Delete one push subscription. The device stops getting pushes at once; its owner can
    subscribe it again from the settings page, which will show it as not subscribed."""
    device = get_object_or_404(PushSubscription, pk=pk)
    owner = device.user.full_name
    device.delete()
    messages.success(request, f"Abonnementet for {owner} er annulleret.")
    return redirect("siteadmin:notifications")
