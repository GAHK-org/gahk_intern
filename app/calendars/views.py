"""The calendar-subscription settings page, and the feed it hands out."""

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from audit import trail
from core.clock import current_datetime
from residents.permissions import current_resident

from . import feed
from .forms import SubscriptionForm
from .models import CalendarSubscription

# Every calendar app fetches far more often than this (Apple hourly, Outlook every few hours, Google
# at least daily), so a week of silence means the subscription is not set up — or was removed.
STALE_AFTER = datetime.timedelta(days=7)


@login_required
def settings_page(request: HttpRequest) -> HttpResponse:
    """The resident's address, what goes into it, how to add it, and whether it is being fetched."""
    subscription = CalendarSubscription.for_resident(current_resident(request))
    if request.method == "POST":
        form = SubscriptionForm(request.POST, instance=subscription)
        if form.is_valid():
            with trail.event("calendars.subscription.updated", subject=subscription, only_if_changed=True):
                form.save()
            messages.success(request, "Gemt. Din kalender henter ændringen næste gang den opdaterer.")
            return redirect("calendars:settings")
    else:
        form = SubscriptionForm(instance=subscription)

    feed_url = request.build_absolute_uri(reverse("calendars:feed", args=[subscription.token]))
    webcal_url = "webcal://" + feed_url.split("://", 1)[1]
    last_used = subscription.last_used_at
    return render(
        request,
        "calendars/settings.html",
        {
            "form": form,
            "subscription": subscription,
            "feed_url": feed_url,
            "webcal_url": webcal_url,
            "stale": last_used is not None and current_datetime() - last_used > STALE_AFTER,
        },
    )


@require_POST
@login_required
def rotate_token(request: HttpRequest) -> HttpResponseRedirect:
    subscription = CalendarSubscription.for_resident(current_resident(request))
    with trail.event("calendars.subscription.rotated", subject=subscription):
        subscription.rotate()
    messages.success(
        request,
        "Din kalenderadresse er skiftet. Husk at tilføje den nye i din kalender — "
        "den gamle holder op med at virke med det samme.",
    )
    return redirect("calendars:settings")


@require_GET
def calendar_feed(request: HttpRequest, token: str) -> HttpResponse:
    """One resident's subscribable calendar. NO AUTH DECORATOR — the token IS the credential.

    That is not a shortcut. Google Calendar's "Fra webadresse" fetch is made by Google's servers, not
    the subscriber's browser: no cookies, no login form. Behind @login_required this would return
    the login page's HTML, and Google would show an empty calendar with no error anywhere. Apple's
    fetcher behaves the same. HTTP Basic is the other option and is worse — it means handing a
    resident's site password to Apple and Google.

    What follows from the token being a bearer credential, and all of it matters:

      * It is a PATH SEGMENT, never a query string. Query strings reach Referer headers and
        third-party analytics far more readily, and calendar clients want a URL ending in .ics.
      * An unknown token 404s — not 403, which would confirm that a token once existed.
      * `no-store` and `noindex`, so it is not cached by a proxy or found by a crawler.
      * It is rotatable, and the page that shows it says so.
    """
    subscription = CalendarSubscription.objects.filter(token=token).select_related("resident").first()
    if subscription is None:
        raise Http404("Ingen kalender med den adresse.")

    response = HttpResponse(feed.render(subscription), content_type="text/calendar; charset=utf-8")
    # inline, NOT attachment: a subscribing client just GETs this, and `attachment` would make a
    # resident who pastes the URL into a browser download a file instead of seeing it work.
    response["Content-Disposition"] = 'inline; filename="gahk-kalender.ics"'
    response["Cache-Control"] = "private, max-age=0, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Referrer-Policy"] = "no-referrer"

    subscription.record_fetch()
    return response


def old_feed_address(request: HttpRequest, token: str) -> HttpResponseRedirect:
    """/kalender/<token>.ics, the address before this moved under /calendar/feed/. Calendar apps and
    gahk.dk's pass-through follow a permanent redirect."""
    return HttpResponseRedirect(reverse("calendars:feed", args=[token]), status=301)
