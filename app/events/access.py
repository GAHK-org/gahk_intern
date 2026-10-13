"""Who may reach begivenheder, who may see which ones, and who may do what to them.

The staged rollout is over: ACCESS_ROLES is None and every resident is in.

    TO RE-GATE IT: set ACCESS_ROLES to a tuple of roles.

The gate MECHANISM is core.rollout — extracted when this became the third feature to want one, on
the schedule opslagstavle/access.py set for it. What is here is this feature's own policy, and the
policy has one part the sibling features do not: an event can be invisible to a resident who is
otherwise allowed into the whole feature.

`visible_to` IS THE CHOKEPOINT. Every view, the calendar, both .ics endpoints and anything added
later must start from it. A private event has to be *absent*, not forbidden — the detail view 404s
for a non-invitee, because a 403 confirms that an event with that id exists, which is precisely the
fact a private event is hiding.

That gives this module two different refusals, and the split is the rule rather than an
inconsistency:

    404  you may not know it exists          (a private event you were not invited to)
    403  you know it exists, but not this    (someone else's event you cannot edit)

MODERATORS DO NOT GET TO SEE PRIVATE EVENTS. Inspektionen moderate opslagstavlen and Den Hurtige,
and deliberately not this: a private event's whole promise is that a non-invitee cannot see it, and
"except Inspektionen" makes that promise false in exactly the case anyone would care about. A
reported private event is a superuser job in the Django admin, which has always seen every table.
"""

from collections.abc import Collection

from django.db.models import Q, QuerySet
from django.http import HttpRequest

from core.rollout import Gate
from residents.models import Resident
from residents.permissions import View, current_resident

from .models import Event, EventComment, EventInvite, EventQuerySet, Visibility

# None = every logged-in resident. A tuple = only those roles (administrator implies every role, so
# administrators and superusers are always in).
#
# Open to the whole kollegium. It was gated to Inspektionen and Netværksgruppen for a first pass,
# matching opslagstavlen's trial group. ("Netværk" was spelled ADMINISTRATOR: the network group is not
# an embedsgruppe with a Workgroup row, so it has never had a role of its own — see
# residents.models.WORKGROUP_ROLE, where `administrator` is deliberately absent for that reason.)
#
# WHAT THE TRIAL COULD NOT ANSWER IS NOW LIVE, and it is the thing to watch. Half a dozen testers
# could exercise creating, answering, the venteliste, the deadline, invites and both .ics paths, but
# "does a month of real events look right in Google Calendar six weeks from now" needs a whole house
# with real answers in it — which is why this module said to open it before trusting that half.
# Opening it is what makes the question askable; the calendar feed is where a problem will surface
# first, and it will surface in somebody's phone rather than in a test.
#
# TO RE-GATE IT: set ACCESS_ROLES to a tuple of roles. That one edit narrows every view, the sidebar
# entry, the "Under test" chip on the list and the push audience together.
ACCESS_ROLES: tuple[str, ...] | None = None

# Read through a lambda, never passed by value: this global is what tests rebind and what the edit
# above would flip, and a Gate holding the value would freeze at import. See core.rollout.
_GATE = Gate(lambda: ACCESS_ROLES)

is_limited = _GATE.is_limited


def roles_allowed(roles: Collection[str]) -> bool:
    """Whether a role set may use begivenheder. Takes roles rather than a request so the sidebar,
    which only has the effective role set to hand, can ask the same question as the views."""
    return _GATE.roles_allowed(roles)


def request_allowed(request: HttpRequest) -> bool:
    """Same question for a request."""
    return _GATE.request_allowed(request)


def access_required(view: View) -> View:
    """@login_required plus the rollout gate. Every view gets this, including the htmx partials: a
    partial that answers 200 to someone the page 403s hands the feature out through the back."""
    return _GATE.required(view)


def allowed_subscribers(qs: QuerySet) -> QuerySet:
    """Narrow a push audience to devices whose owner can actually open the feature."""
    return _GATE.allowed_subscribers(qs)


def visible_to(resident: Resident) -> EventQuerySet:
    """The ONLY queryset any view, partial, calendar or feed may start from.

    SUBQUERIES RATHER THAN JOINS, and that is correctness, not taste. `Q(invites__resident=r)` is a
    multi-valued join, so an event you are BOTH a co-organiser of and invited to comes back twice —
    a duplicate card in the list and, worse, two VEVENTs sharing one UID in a calendar file, which
    some clients resolve by dropping both. `.distinct()` patches that, and then silently stops being
    enough the moment anyone adds `annotate(Count("rsvps"))`, because the join multiplies the rows
    before the aggregate reaches them. `pk__in=<subquery>` has neither problem and needs no
    incantation a later reader has to know to keep.
    """
    return Event.objects.filter(
        Q(visibility=Visibility.AABENT)
        | Q(organiser=resident)
        | Q(pk__in=Event.co_organisers.through.objects.filter(resident_id=resident.pk).values("event_id"))
        | Q(pk__in=EventInvite.objects.filter(resident=resident).values("event_id"))
    )


def is_host(event: Event, resident: Resident) -> bool:
    """Whether this resident runs the event — the organiser or a co-organiser.

    Hosts may edit, cancel, invite, uninvite and promote. Everything except handing over the
    event itself, which stays with the organiser (see can_manage_hosts).
    """
    if event.organiser_id == resident.pk:
        return True
    return event.co_organisers.filter(pk=resident.pk).exists()


def can_edit(event: Event, resident: Resident) -> bool:
    """Hosts edit; nobody else does, moderators included.

    A cancelled event is frozen: there is nothing useful to change about something that is not
    happening, and an edit would bump SEQUENCE and re-notify sixty phones about it.
    """
    return not event.is_cancelled and is_host(event, resident)


def can_manage_hosts(event: Event, resident: Resident) -> bool:
    """Only the ORIGINAL organiser adds or removes co-organisers.

    If co-organisers could, one of them could remove the organiser and the event would have no
    owner — and no way back to having one short of the admin.
    """
    return event.organiser_id == resident.pk


def can_delete(event: Event, resident: Resident) -> bool:
    """Delete is for events nobody has committed to yet.

    Once anyone has said ja the control becomes Aflys instead (see services.cancel). A hard delete
    vanishes from every subscribed calendar with no explanation, and the per-event .ics already
    imported into other people's calendars stays there forever, because once the row is gone there
    is nothing left to emit STATUS:CANCELLED from.
    """
    if not is_host(event, resident):
        return False
    return not event.rsvps.filter(answer="ja").exists()


def can_cancel(event: Event, resident: Resident) -> bool:
    """The other half of can_delete: hosts may aflys anything not already cancelled."""
    return not event.is_cancelled and is_host(event, resident)


def can_delete_comment(comment: EventComment, resident: Resident, *, host: bool | None = None) -> bool:
    """Its author, or a HOST of the event it is on.

    NOT "a moderator", which is where the two sibling features land (opslagstavle.access and
    reparationer.views both say "the author, or a manager"). It cannot be that here, and the reason
    is the paragraph at the top of this module: Inspektionen deliberately cannot SEE a private
    event, so "Inspektionen may delete its comments" would be a permission that either does
    nothing or quietly reintroduces the read access the 404 exists to deny. The host is the right
    analogue anyway — they run the event, they are who a comment thread on it is aimed at, and on
    an open event they are as reachable as Inspektionen would be.

    A superuser still has the Django admin, which is where a reported private event is handled
    (see admin.py). That is the escape hatch, and it is deliberately the only one.

    `host` is an optimisation for asking this about a WHOLE THREAD, and the rule stays here rather
    than being re-expressed in the view. `is_host` costs a query — it filters co_organisers — and
    its answer is identical for every comment on one event, so views._comments resolves it once and
    passes it in; left None, this asks for itself. Authorship is checked first either way, so a
    resident reading their own comments needs no query at all.
    """
    if comment.author_id == resident.pk:
        return True
    return is_host(comment.event, resident) if host is None else host


def request_host(request: HttpRequest, event: Event) -> bool:
    """`is_host` for a request, using the *effective* resident."""
    return is_host(event, current_resident(request))
