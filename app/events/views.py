"""Begivenheder's views.

TWO RULES THIS MODULE MUST KEEP, both of which have their own test:

  1. **Nothing here touches `Event.objects` directly.** Every read starts from `_get_event` or
     `access.visible_to`, so a private event cannot leak out of a view somebody adds later and
     forgets to filter. `test_views_never_reach_event_objects_directly` reads this file's source and
     fails if the string appears.
  2. **Every view carries `@access_required`**, partials included. A partial that answers 200 to
     someone the page 403s hands the feature out through the back door.
"""

import calendar as calendar_module
import datetime

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core import push
from core.clock import current_date, current_datetime
from core.danish import MONTHS, WEEKDAYS_SHORT
from core.uploads import attached_image
from residents import birthdays as birthdays_mod
from residents.models import Resident
from residents.permissions import current_resident

from . import access, icalendar, services
from .forms import EventCommentForm, EventForm
from .models import Answer, CalendarFeedToken, Event, EventComment, Rsvp

PAGE_SIZE = 20


def _my_state(event: Event, resident: Resident) -> str | None:
    """ "kommer" / "venteliste" / "nej" / None, from rows already prefetched.

    Seating is positional, so a card cannot read a column to know where you stand — it has to know
    where you fall in the order. Done over the prefetch rather than by calling services.seated per
    card, which would be a query each.
    """
    committed = sorted(
        (r for r in event.rsvps.all() if r.answer == Answer.JA),
        key=lambda r: (r.answered_at, r.pk),
    )
    coming = committed if event.capacity is None else committed[: event.capacity]
    mine = next((r for r in event.rsvps.all() if r.resident_id == resident.pk), None)
    if mine is None:
        return None
    if mine.answer == Answer.NEJ:
        return "nej"
    return "kommer" if any(r.pk == mine.pk for r in coming) else "venteliste"


def _as_datetime(day: datetime.date) -> datetime.datetime:
    """Local midnight on `day`, as an aware datetime for comparing against starts_at."""
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time()))


def _requested_month(request: HttpRequest) -> tuple[int, int]:
    """`?maaned=YYYY-MM`, or this month. Junk falls back rather than erroring — a mistyped URL
    should show you a calendar, not a stack trace."""
    raw = request.GET.get("maaned", "")
    try:
        year, month = (int(part) for part in raw.split("-", 1))
        if 1 <= month <= 12 and 2000 <= year <= 2100:
            return year, month
    except (ValueError, TypeError):
        pass
    today = current_date()
    return today.year, today.month


def _month_param(day: datetime.date) -> str:
    return f"{day.year}-{day.month:02d}"


def _requested_day(request: HttpRequest, span: tuple[datetime.date, datetime.date]) -> datetime.date | None:
    """`?dag=YYYY-MM-DD`, the day whose events are spelled out under the grid. None if absent.

    This is what makes the calendar usable on a phone: seven columns wide enough to read leave no
    room for event titles, so the cells carry dots and a tap opens the day down here. A URL
    parameter rather than JavaScript, for the same reason the month is one — a day is a place, so it
    should survive a reload and be sendable to somebody.

    A day outside the visible grid is treated as absent. Otherwise `?dag=2019-04-01` would print a
    heading and a list for a date nowhere on the page, which reads as a rendering bug.
    """
    try:
        day = datetime.date.fromisoformat(request.GET.get("dag", ""))
    except ValueError:
        return None
    return day if span[0] <= day <= span[1] else None


def _weeks_of(first: datetime.date) -> list[list[datetime.date]]:
    """The whole Monday-to-Sunday weeks that a month occupies, padding included."""
    # Monday first, as a Danish calendar is printed.
    return calendar_module.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)


def _grid_span(first: datetime.date) -> tuple[datetime.date, datetime.date]:
    """First and last day the grid actually SHOWS — which is what the query has to cover."""
    weeks = _weeks_of(first)
    return weeks[0][0], weeks[-1][-1]


def _month_grid(
    first: datetime.date,
    by_day: dict[datetime.date, list[Event]],
    chosen: datetime.date | None = None,
    birthdays: dict[datetime.date, list[birthdays_mod.Birthday]] | None = None,
) -> list[list[dict[str, object]]]:
    """The month as whole Monday-to-Sunday weeks, padded into the neighbouring months.

    Padding rather than blanks, because a grid that starts mid-row reads as broken, and because an
    event on the 1st of next month is worth seeing from the 30th of this one. The padding days are
    marked so the template can grey them.

    Birthdays ride in the same cell as the events and stay a separate key, never merged into
    `events`. They are notes rather than begivenheder — see residents.birthdays — and everything
    the template does with an event (a link to a detail page, a state colour, an "Aflyst" chip)
    would be a lie about one.
    """
    marked = birthdays or {}
    weeks: list[list[dict[str, object]]] = []
    for week in _weeks_of(first):
        weeks.append(
            [
                {
                    "date": day,
                    "outside": day.month != first.month,
                    "events": by_day.get(day, []),
                    "birthdays": marked.get(day, []),
                    "chosen": day == chosen,
                }
                for day in week
            ]
        )
    return weeks


def _get_event(request: HttpRequest, pk: int) -> Event:
    """The only way this module reaches an event by primary key.

    404 — never 403 — for one this resident may not see. A 403 confirms that event #17 exists, which
    is precisely the fact an invite-only event is hiding. The other refusal (403) is for things you
    know exist but may not do, like editing somebody else's event; see access.py on the split.
    """
    return get_object_or_404(access.visible_to(current_resident(request)).select_related("organiser"), pk=pk)


def _rsvp_context(request: HttpRequest, event: Event) -> dict[str, object]:
    """Everything the answer panel needs, shared by the page and the htmx partial."""
    resident = current_resident(request)
    mine = event.rsvps.filter(resident=resident).first()
    coming = services.seated(event)
    queue = services.waitlist(event)
    return {
        "event": event,
        "mine": mine,
        "seated": coming,
        "seated_count": len(coming),
        "declined_count": event.rsvps.filter(answer=Answer.NEJ).count(),
        "waitlist": queue,
        "waitlist_count": len(queue),
        "my_position": next((i + 1 for i, r in enumerate(queue) if r.resident_id == resident.pk), None),
        "locked": services.answers_locked(event),
        "is_host": access.is_host(event, resident),
        "is_full": event.capacity is not None and len(coming) >= event.capacity,
    }


@access.access_required
def index(request: HttpRequest) -> HttpResponse:
    """Everything coming up. There is no past — see the retention note in models.py.

    Purges on the way past, the den_hurtige idiom: traffic does the cleanup and the cron covers the
    quiet weeks. Cheap here, and it is the backstop for a cron that has silently stopped.
    """
    Event.objects.purge_expired()
    services.ensure_due_reminders_sent()
    resident = current_resident(request)
    upcoming = access.visible_to(resident).upcoming().select_related("organiser").prefetch_related("rsvps")
    page = Paginator(upcoming, PAGE_SIZE).get_page(request.GET.get("page"))

    # Attached here rather than resolved in the template, because a Django template cannot look a
    # dict up by a variable key and "did *I* answer?" depends on the current user. Same move as
    # den_hurtige.views.posts_for attaching reaction_rows.
    #
    # Counted in Python over the prefetch, not with an annotate: an event has at most one row per
    # resident (uniq_rsvp_per_resident), so this is sixty rows already in memory. A Count() would be
    # a second query and a join that the next annotate would silently multiply.
    answers = {r.event_id: r for r in Rsvp.objects.filter(resident=resident, event__in=page.object_list)}
    for event in page:
        # Seating is positional (services.seated), so the card cannot just read a column to know
        # whether YOU are in or on the venteliste — it has to know where you fall in the order.
        # Done here over the prefetch rather than by calling services.seated per card, which would
        # be a query each. Sixty rows, already in memory, sorted the same way services does it.
        committed = sorted(
            (r for r in event.rsvps.all() if r.answer == Answer.JA),
            key=lambda r: (r.answered_at, r.pk),
        )
        coming = committed if event.capacity is None else committed[: event.capacity]
        mine = answers.get(event.pk)
        event.seated_count = len(coming)  # type: ignore[attr-defined]
        event.my_state = (  # type: ignore[attr-defined]
            None
            if mine is None
            else "nej"
            if mine.answer == Answer.NEJ
            else "kommer"
            if any(r.pk == mine.pk for r in coming)
            else "venteliste"
        )

    return render(
        request,
        "events/index.html",
        {
            "page_obj": page,
            "push_configured": push.is_configured(),
            "vapid_public_key": push.vapid_public_key(),
            "push_subscribed": push.subscribers(services.TOPIC).filter(user=resident).exists(),
            "limited_rollout": access.is_limited(),
        },
    )


@access.access_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    event = _get_event(request, pk)
    resident = current_resident(request)
    context = _rsvp_context(request, event)
    context.update(
        {
            "can_edit": access.can_edit(event, resident),
            "can_delete": access.can_delete(event, resident),
            "can_cancel": access.can_cancel(event, resident),
            # The other half of Notice.event: the posts announcing this thing. Gated on the READER's
            # access to opslagstavlen, which is still a role trial — linking somebody to a board
            # that will 403 them is worse than not mentioning it. Imported lazily so the two apps
            # stay acyclic at import time (opslagstavle.forms reaches the other way).
            "notices": _related_notices(request, event),
            "comments": _comments(request, event),
            "comment_form": EventCommentForm(),
        }
    )
    return render(request, "events/detail.html", context)


def _comments(request: HttpRequest, event: Event) -> list:
    """The thread, each row carrying its own `can_delete`.

    Resolved here rather than in the template because a Django template cannot call
    access.can_delete_comment with arguments — the same reason opslagstavlen and reparationer both
    do this in the view.

    `host` is read ONCE for the event and handed to every row. Without it each comment would ask
    access.is_host for itself, which filters co_organisers, so a thread of twenty comments would
    cost twenty queries to render a ✕ that is either on all of them or none.
    """
    resident = current_resident(request)
    host = access.is_host(event, resident)
    comments = list(event.comments.select_related("author"))
    for comment in comments:
        comment.can_delete = access.can_delete_comment(comment, resident, host=host)  # type: ignore[attr-defined]
    return comments


def _comment_anchor(event_pk: int) -> str:
    """Back to the thread, not to the top of the page. The event page is long — image, facts,
    description, the answer panel, the posts announcing it — so a bare redirect after commenting
    lands the reader above the fold and hides the thing they just wrote."""
    return f"{reverse('events:detail', args=[event_pk])}#kommentarer"


def _related_notices(request: HttpRequest, event: Event) -> list:
    from opslagstavle.access import request_allowed as board_allowed

    if not board_allowed(request):
        return []
    return list(event.notices.select_related("author").order_by("-created_at")[:5])


def _visible_comment(request: HttpRequest, pk: int) -> EventComment:
    """The only way this module reaches a comment by primary key.

    Filtered through access.visible_to on the way in, exactly like _get_event, and for the same
    reason one step removed: a COMMENT id is a side channel onto the event it hangs off. Without
    this filter, POSTing to kommentar/<id>/slet tells a non-invitee whether that comment exists
    (403) or not (404) — which answers "is there a private event with a thread on it" — and a
    resident who guessed their own comment id on an event they were since uninvited from would
    still be able to reach into it.

    Not `Event.objects`, per rule 1 in the module docstring: the filter is a subquery on
    access.visible_to, so the chokepoint is still the only thing that decides visibility.
    """
    return get_object_or_404(
        EventComment.objects.select_related("event", "event__organiser", "author").filter(
            event__in=access.visible_to(current_resident(request))
        ),
        pk=pk,
    )


@require_POST
@access.access_required
def create_comment(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Anyone who can SEE the event may comment on it.

    No narrower than that, and two candidate restrictions were considered and dropped:

      * **Not "only people who answered ja".** "Kan man tage børn med?" and "hvad skal jeg tage
        med?" are questions you ask BEFORE you commit, so a thread visible only to people who
        already committed would be shut to exactly the readers who need it.
      * **Not "not on a cancelled event".** access.can_edit freezes an aflyst event because an
        edit bumps SEQUENCE and re-notifies every subscribed calendar; a comment does neither, and
        "hvorfor?" / "vi finder en ny dato" is the most predictable thread on the feature. The
        answer panel locks; the thread does not.

    The audience is already correct without any check of its own: _get_event has applied
    access.visible_to, so on a private event the only people who can reach this are its invitees
    and hosts.
    """
    event = _get_event(request, pk)
    form = EventCommentForm(request.POST)
    # EVENT_IMAGE_MAX_MB, so a comment photo and the event's own hero image share one ceiling.
    image = attached_image(request, settings.EVENT_IMAGE_MAX_MB)

    if not form.is_valid():
        # The message rather than re-rendering the page: the form lives at the bottom of a long
        # read-only page, and a re-render would throw away the RSVP panel's state to say
        # "write something".
        messages.error(request, "Kommentaren kunne ikke gemmes.")
        return redirect(_comment_anchor(event.pk))

    comment = form.save(commit=False)
    if not comment.body and image is None:
        # Either nothing was submitted, or the only thing submitted was a picture that
        # core.uploads.attached_image refused - in which case its warning is already queued and
        # arrives beside this, so the reader learns both halves.
        messages.error(request, "Skriv en kommentar, eller vedhæft et billede.")
        return redirect(_comment_anchor(event.pk))

    comment.event = event
    comment.author = current_resident(request)
    if image is not None:
        comment.image = image
    comment.save()
    services.notify_new_comment(comment)
    return redirect(_comment_anchor(event.pk))


@require_POST
@access.access_required
def delete_comment(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """403, not 404, for a comment you can see but may not remove — the split the access module
    documents. The 404 case is handled a step earlier, by _visible_comment."""
    comment = _visible_comment(request, pk)
    if not access.can_delete_comment(comment, current_resident(request)):
        raise PermissionDenied
    event_pk = comment.event_id
    comment.delete()
    messages.success(request, "Kommentaren er slettet.")
    return redirect(_comment_anchor(event_pk))


@access.access_required
def create(request: HttpRequest) -> HttpResponse:
    resident = current_resident(request)
    if request.method != "POST":
        return render(request, "events/form.html", {"form": EventForm(organiser=resident)})

    form = EventForm(request.POST, request.FILES, organiser=resident)
    if not form.is_valid():
        return render(request, "events/form.html", {"form": form})

    event = form.save(commit=False)
    event.organiser = resident
    event.save()
    # After the event exists, because an invite needs something to point at. The notification then
    # reads the finished guest list, which is what decides who hears about a private event at all.
    services.sync_invites(event, list(form.cleaned_data["invitees"]), invited_by=resident)
    services.notify_new_event(event)
    messages.success(request, "Begivenheden er oprettet.")
    return redirect("events:detail", pk=event.pk)


@access.access_required
def edit(request: HttpRequest, pk: int) -> HttpResponse:
    event = _get_event(request, pk)
    if not access.can_edit(event, current_resident(request)):
        raise PermissionDenied

    if request.method != "POST":
        return render(
            request,
            "events/form.html",
            {"form": EventForm(instance=event, organiser=event.organiser), "event": event},
        )

    # Snapshot BEFORE binding: a ModelForm mutates its instance in place on validation, so "the
    # event as it was" cannot be read off `event` afterwards. A plain tuple rather than a re-read,
    # because this module never touches Event.objects (see the docstring).
    before = services.significant_fields(event)
    form = EventForm(request.POST, request.FILES, instance=event, organiser=event.organiser)
    if not form.is_valid():
        return render(request, "events/form.html", {"form": form, "event": event})

    updated = form.save(commit=False)
    updated.edited_at = current_datetime()
    if services.significant_change(before, updated):
        # One predicate for both the SEQUENCE bump and the push, so a calendar client and a phone
        # can never disagree about whether something changed.
        updated.sequence += 1
        updated.save()
        services.notify_changed(updated)
    else:
        updated.save()
    services.sync_invites(updated, list(form.cleaned_data["invitees"]), invited_by=current_resident(request))
    messages.success(request, "Begivenheden er opdateret.")
    return redirect("events:detail", pk=event.pk)


@require_POST
@access.access_required
def delete(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Only while nobody has committed. Otherwise the control is Aflys — see access.can_delete."""
    event = _get_event(request, pk)
    if not access.can_delete(event, current_resident(request)):
        raise PermissionDenied
    event.delete()
    messages.success(request, "Begivenheden er slettet.")
    return redirect("events:index")


@require_POST
@access.access_required
def cancel(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    event = _get_event(request, pk)
    if not access.can_cancel(event, current_resident(request)):
        raise PermissionDenied
    services.cancel(event)
    messages.success(request, "Begivenheden er aflyst, og alle tilmeldte har fået besked.")
    return redirect("events:detail", pk=event.pk)


@require_POST
@access.access_required
def answer(request: HttpRequest, pk: int) -> HttpResponse:
    """Record ja or nej, returning just the answer panel.

    The deadline is checked here so the happy path never raises, and caught anyway: `set_answer` is
    the authority, and between rendering the page and pressing the button the deadline may have
    passed. A closed event falls through to a plain re-render of the panel in its locked state,
    following opslagstavle.views.toggle_reaction — an htmx swap has nowhere to show a redirect.
    """
    event = _get_event(request, pk)
    choice = request.POST.get("svar")
    if choice in {Answer.JA, Answer.NEJ}:
        try:
            services.set_answer(event.pk, current_resident(request), choice)
        except services.RsvpClosed:
            pass
    event.refresh_from_db()
    return render(request, "events/_rsvp.html", _rsvp_context(request, event))


@require_GET
@access.access_required
def event_ics(request: HttpRequest, pk: int) -> HttpResponse:
    """One event as a download. `attachment`, because this one genuinely is a file."""
    event = _get_event(request, pk)
    response = HttpResponse(icalendar.one_event(event), content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="begivenhed-{event.pk}.ics"'
    return response


@access.access_required
def feed_settings(request: HttpRequest) -> HttpResponse:
    """Where a resident finds — and can replace — their personal calendar address."""
    token = CalendarFeedToken.for_resident(current_resident(request))
    return render(
        request,
        "events/feed.html",
        {"feed_url": request.build_absolute_uri(reverse("events_feed", args=[token.token]))},
    )


@require_POST
@access.access_required
def rotate_token(request: HttpRequest) -> HttpResponseRedirect:
    CalendarFeedToken.for_resident(current_resident(request)).rotate()
    messages.success(
        request,
        "Din kalenderadresse er skiftet. Husk at tilføje den nye i din kalender — "
        "den gamle holder op med at virke med det samme.",
    )
    return redirect("events:feed_settings")


@access.access_required
def calendar(request: HttpRequest) -> HttpResponse:
    """A month at a time. Server-rendered; the arrows are ordinary links.

    Not htmx: a month is a place you can be, so it should be a URL you can send somebody, reload,
    and press back out of. `?maaned=YYYY-MM`.
    """
    Event.objects.purge_expired()
    resident = current_resident(request)
    year, month = _requested_month(request)
    first = datetime.date(year, month, 1)
    last = datetime.date(year + (month == 12), month % 12 + 1, 1)

    # The GRID's span, not the month's. The grid pads out to whole weeks, so late August shows the
    # first days of September — and querying the month would render those cells empty while the
    # events they should carry sit one click away. A calendar that draws a day and then hides what
    # is on it is worse than one that does not draw the day at all.
    grid_first, grid_last = _grid_span(first)

    events = (
        access.visible_to(resident)
        .filter(
            starts_at__gte=_as_datetime(grid_first),
            starts_at__lt=_as_datetime(grid_last + datetime.timedelta(days=1)),
        )
        .select_related("organiser")
        .prefetch_related("rsvps")
        .order_by("starts_at")
    )
    by_day: dict[datetime.date, list[Event]] = {}
    for event in events:
        event.my_state = _my_state(event, resident)  # type: ignore[attr-defined]
        by_day.setdefault(timezone.localtime(event.starts_at).date(), []).append(event)

    # Notes on the days, not rows in `events` — see residents.birthdays for why a birthday is not
    # an Event. Queried over the GRID's span for the same reason the events are: the cell for
    # 1 September is drawn on the August page, and a name that appears only when you click through
    # to the next month is a name the calendar failed to tell you about.
    birthdays = birthdays_mod.in_span(grid_first, grid_last)

    chosen = _requested_day(request, (grid_first, grid_last))

    return render(
        request,
        "events/calendar.html",
        {
            "weeks": _month_grid(first, by_day, chosen, birthdays),
            "month_label": f"{MONTHS[month].capitalize()} {year}",
            "this_month": _month_param(first),
            "prev": _month_param(first - datetime.timedelta(days=1)),
            "next": _month_param(last),
            "today": current_date(),
            "weekdays": WEEKDAYS_SHORT,
            "chosen_day": chosen,
            # Empty is a real answer here and the template says so in Danish: "ingen begivenheder
            # den 12." is what a tap on a quiet day should produce, not a panel that fails to appear.
            "chosen_events": by_day.get(chosen, []) if chosen else [],
            "chosen_birthdays": birthdays.get(chosen, []) if chosen else [],
        },
    )


@require_GET
def calendar_feed(request: HttpRequest, token: str) -> HttpResponse:
    """One resident's subscribable calendar. NO AUTH DECORATOR — the token IS the credential.

    That is not a shortcut. Google Calendar's "Fra URL" fetch is made by Google's servers, not the
    subscriber's browser: no cookies, no login form, no redirect to follow. Behind @login_required
    this would return the login page's HTML with a 200, and Google would show an empty calendar with
    no error anywhere. Apple's fetcher behaves the same. HTTP Basic is the other option and is worse
    — it means handing a resident's site password to Apple and Google.

    What follows from the token being a bearer credential, and all of it matters:

      * It is a PATH SEGMENT, never a query string. Query strings reach Referer headers and
        third-party analytics far more readily, and calendar clients want a URL ending in .ics.
      * An unknown token 404s — not 403, which would confirm that a token once existed.
      * `no-store` and `noindex`, so it is not cached by a proxy or found by a crawler.
      * The feed contains only THIS person's own ja-events, so a leak exposes one person's social
        calendar rather than the kollegium's.
      * It is rotatable, and the page that shows it says so in Danish (see events/feed.html).

    Deliberately outside the rollout gate: somebody not in a trial group simply has no answers, so
    their feed is a valid empty VCALENDAR rather than a 403 a calendar client cannot explain.
    """
    feed_token = CalendarFeedToken.objects.filter(token=token).select_related("resident").first()
    if feed_token is None:
        raise Http404("Ingen kalender med den adresse.")

    resident = feed_token.resident
    since = current_datetime() - icalendar.FEED_LOOKBACK
    mine = (
        access.visible_to(resident)
        .filter(rsvps__resident=resident, rsvps__answer=Answer.JA, starts_at__gte=since)
        .prefetch_related("rsvps")
        .order_by("starts_at")
        .distinct()
    )
    entries = [(event, _my_state(event, resident) == "venteliste") for event in mine]

    body = icalendar.feed(entries, name="GAHK – mine begivenheder")
    response = HttpResponse(body, content_type="text/calendar; charset=utf-8")
    # inline, NOT attachment: a subscribing client just GETs this, and `attachment` would make a
    # resident who pastes the URL into a browser download a file instead of seeing it work.
    response["Content-Disposition"] = 'inline; filename="gahk-begivenheder.ics"'
    response["Cache-Control"] = "private, max-age=0, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Referrer-Policy"] = "no-referrer"

    _touch(feed_token)
    return response


def _touch(feed_token: CalendarFeedToken) -> None:
    """Record that the feed was fetched, at most once an hour.

    A write on every GET would turn a read endpoint into a write storm: sixty phones polling hourly,
    forever. Once an hour is enough to answer "is my calendar actually pulling?" and to make a leak
    that is being *used* visible in the data.
    """
    now = current_datetime()
    if feed_token.last_used_at and now - feed_token.last_used_at < datetime.timedelta(hours=1):
        return
    CalendarFeedToken.objects.filter(pk=feed_token.pk).update(last_used_at=now)


@access.access_required
def save_subscription(request: HttpRequest) -> HttpResponse:
    """Per-topic push opt-in. The body is shared with the other two features — see core.push."""
    return push.handle_subscription_request(request, services.TOPIC)
